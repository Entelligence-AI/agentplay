"""The local map store: ``~/.agentplay/maps`` plus a JSON index.

Layout (override the root with ``AGENTPLAY_HOME``)::

    ~/.agentplay/
      index.json              session_id -> index row (summary, hash, source mtime)
      maps/<key>.json         one agentplay/v1 document per session
      cache/<agent>/...       decoded transcripts for database-backed agents

Builds are incremental: a session is rebuilt only when its transcript's mtime
changed since the stored build (or ``--rebuild`` is passed).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

INDEX_VERSION = 1


def root() -> Path:
    return Path(os.environ.get("AGENTPLAY_HOME") or Path.home() / ".agentplay")


class OutsideRoot(OSError):
    """A write resolved to somewhere outside the directory it was confined to."""

def _atomic_write(path: Path, data: bytes, *, private: bool = True, within: Path | None = None) -> None:
    """Replace ``path`` with ``data`` atomically, keeping an existing file's mode.

    A new file is 0600 when ``private`` (agentplay's own store), else the umask
    default like any editor would create (files in a person's repo). ``within`` refuses a
    write whose final, symlink-resolved path left that directory.
    """
    # A person's file may be a dotfiles-managed symlink: write its target, never replace the link.
    # agentplay's own store never follows one, so a link planted there cannot redirect a write.
    if not private:
        path = Path(os.path.realpath(path))
        # Checked on the same resolved path the write uses, not on an earlier resolution of it.
        if within is not None and not path.is_relative_to(within):
            raise OutsideRoot(f"{path} is outside {within}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # lstat for the store: a link planted there must not lend its target's mode
        # (a 0644 target would make the replacement readable by other users).
        st = os.lstat(path) if private else path.stat()
        mode: int | None = stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else None
    except FileNotFoundError:
        mode = None
    tmp = path.parent / f"{path.name}{uuid.uuid4().hex[:8]}.tmp"
    # The kernel applies the umask to the create mode, so no process-wide umask juggling between threads.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600 if private or mode is not None else 0o666)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# What `map_key` produces. A key read back from the index is checked against it,
# so a hand-edited or planted index can't point a read outside `maps/`.
_KEY_RE = re.compile(r"[A-Za-z0-9._-]{1,80}-[0-9a-f]{10}")


def map_key(session_id: str) -> str:
    """Filesystem-safe, collision-free file stem for a session id."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", session_id)[:80]
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:10]
    return f"{safe}-{digest}"


class Store:
    """Read/write access to the local index and map files."""

    def __init__(self, base: Path | None = None) -> None:
        self.base = base or root()
        self.maps_dir = self.base / "maps"
        self.index_path = self.base / "index.json"
        self._index: dict[str, dict[str, Any]] | None = None
        self._index_stamp: tuple[int, int] | None = None

    # ------------------------------------------------------------------ index
    @property
    def index(self) -> dict[str, dict[str, Any]]:
        index = self._index
        if index is None:
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8"))
                index = raw.get("sessions", {}) if raw.get("version") == INDEX_VERSION else {}
            except (OSError, ValueError, AttributeError):
                index = {}
            self._index = index
            self._index_stamp = self._disk_stamp()
        return index

    def _disk_stamp(self) -> tuple[int, int] | None:
        # Size as well as mtime: Windows file times tick about every 15 ms, so two
        # writes in a row can share an mtime.
        try:
            st = self.index_path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def reload_if_changed(self) -> bool:
        """Drop the cached index when index.json changed underneath us.

        The server holds one Store for its lifetime, and `agentplay view` now scans
        in a background thread, so the index it serves would otherwise be whatever
        existed at startup.

        Explicit rather than a check inside `index`: `build_all` does many `put`s
        before a single `save_index`, and reloading between them would discard
        every pending row.
        """
        if self._index is None:
            return False
        stamp = self._disk_stamp()
        if stamp is None or stamp == self._index_stamp:
            return False
        self._index = None
        return True

    def save_index(self) -> None:
        body = json.dumps({"version": INDEX_VERSION, "sessions": self.index}, separators=(",", ":"))
        _atomic_write(self.index_path, body.encode("utf-8"))
        self._index_stamp = self._disk_stamp()

    def is_fresh(
        self, session_id: str, source_mtime: float, built_with: str | None = None, path_mode: str | None = None
    ) -> bool:
        row = self.index.get(session_id)
        if not row:
            return False
        if row.get("imported"):
            return True  # an imported map has no local transcript to rebuild from
        if abs(float(row.get("sourceMtime") or 0) - source_mtime) > 0.5:
            return False
        # A map built by another version is rebuilt once: redaction fixes (and any
        # other change to what a map holds) must reach maps already on disk.
        if built_with is not None and row.get("builtWith") != built_with:
            return False
        # Asking for another path mode (say dirs_only after a relative build) must
        # re-redact maps already on disk, not only new sessions.
        if path_mode is not None and (row.get("pathMode") or "relative") != path_mode:
            return False
        return (self.maps_dir / f"{row['key']}.json").is_file()

    # ------------------------------------------------------------------ maps
    def put(self, doc: dict[str, Any], row: dict[str, Any]) -> None:
        """Store one map document and its index row."""
        sid = doc["session"]["id"]
        key = map_key(sid)
        _atomic_write(self.maps_dir / f"{key}.json", _canonical(doc))
        self.index[sid] = {**row, "key": key}

    def get(self, session_id: str) -> dict[str, Any] | None:
        row = self.index.get(session_id)
        key = row.get("key") if row else None
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            return None
        try:
            maps = self.maps_dir.resolve()
            target = (maps / f"{key}.json").resolve()
            if not target.is_relative_to(maps):  # e.g. a symlink planted in maps/
                return None
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def rows(self) -> list[dict[str, Any]]:
        """Index rows, newest first."""
        return sorted(self.index.values(), key=lambda r: r.get("startedAt") or "", reverse=True)

    def docs(self, *, since: str | None = None, main_only: bool = True) -> Iterator[dict[str, Any]]:
        """Stored map documents, newest first.

        Args:
            since: ISO date (YYYY-MM-DD); skip sessions that started earlier.
            main_only: skip subagent and helper sessions (their work is already
                inside the parent map).
        """
        for row in self.rows():
            if since and (row.get("startedAt") or "")[:10] < since:
                continue
            if main_only and row.get("role") in ("subagent", "helper"):
                continue
            doc = self.get(row["sessionId"])
            if doc is not None:
                yield doc


def _canonical(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
