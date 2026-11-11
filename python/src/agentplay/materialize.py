"""Decode agents that keep sessions in a database into NDJSON transcripts.

OpenCode (``opencode.db``), Cursor CLI/agent (``store.db``), Cursor IDE composer
chats (``state.vscdb``), Goose (``sessions.db``) and Kiro (session directories) do not write one plain
file per session. Each ``find_*_candidates`` generator below decodes those stores
and writes one NDJSON transcript per session into a local cache under
``~/.agentplay/cache/<agent>/``, which the parsers in ``agentplay._core`` then
read like any other transcript. Consuming the generator performs the writes.

Everything here is read-only against the agent's own files: SQLite stores are
opened ``mode=ro&immutable=1`` or copied to a temp dir first. Nothing here makes
a network call.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import store

logger = logging.getLogger("agentplay.materialize")

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    """Log ``message`` at WARNING the first time ``key`` is seen in this process.

    A transcript source that cannot be read hides every session in it, so the user
    should hear about it once, not once per session or per rescan.
    """
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _warn_unreadable_db(agent: str, db_path: Path, exc: Exception) -> None:
    """One WARNING per broken ``agent`` database; the path is shown home-relative."""
    try:
        shown = "~/" + db_path.relative_to(Path.home()).as_posix()
    except ValueError:
        shown = db_path.name
    reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
    _warn_once(
        f"{agent}:{db_path}",
        "%s: cannot read %s (%s); its sessions are skipped",
        agent,
        shown,
        reason,
    )


def cache_root() -> Path:
    """Where decoded transcripts are cached, resolved at call time.

    Resolved per call rather than at import, so ``AGENTPLAY_HOME`` set after this
    module is imported (a test fixture, an embedding process) is still honoured.
    """
    return store.root() / "cache"


class AgentKind(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    OPENCODE = "opencode"
    CURSOR = "cursor"
    KIRO = "kiro"
    COPILOT = "copilot"
    GOOSE = "goose"


@dataclass(frozen=True)
class HomeSpec:
    """One agent home directory to read from."""

    kind: AgentKind
    path: Path

    @property
    def label(self) -> str:
        """Cache sub-directory for this home: its name, plus a short digest when
        the home is not directly under ``$HOME`` so two homes never collide."""
        name = self.path.name or self.path.anchor.strip("/") or "root"
        parent = self.path.parent
        try:
            home = Path.home()
            if parent == home or parent == home.resolve():
                return name
        except (OSError, RuntimeError):
            pass
        digest = hashlib.blake2s(str(parent).encode("utf-8"), digest_size=3).hexdigest()
        return f"{name}-{digest}"


@dataclass
class Session:
    """A materialized session file."""

    agent: AgentKind
    home_label: str
    abs_path: Path
    rel_path: Path
    size_bytes: int
    mtime: datetime
    turns: int = 0
    cwd: Path | None = None
    secret_findings: list = field(default_factory=list)


def _default_state_db_path() -> Path:
    """Cursor's global ``state.vscdb`` for the current OS."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "Cursor" / "User" / "globalStorage" / "state.vscdb"


#
# opencode (sst/opencode, v1.17+) stores everything in a single SQLite db at
# ``<data-dir>/opencode.db`` — no per-session file. A session is one ``session`` row (which
# carries ``directory`` = cwd) plus rows in ``message`` / ``part``. To ride the same file-based
# pipeline as Claude Code / Codex, we synthesize one NDJSON transcript per session (one line per
# message, its parts merged in) and materialize it under this cache root; the rest of the pipeline
# (secret + PII scrub, manifest, dedup) then treats it like any other transcript file.

def opencode_cache_root() -> Path:
    return cache_root() / "opencode"


def _opencode_db(home: Path) -> Path | None:
    """The opencode SQLite db under ``home`` (``opencode.db``, or an ``opencode-<channel>.db``)."""
    db = home / "opencode.db"
    if db.is_file():
        return db
    others = sorted(home.glob("opencode*.db"))
    return others[0] if others else None


def _sqlite_ro_uri(db: Path) -> str:
    """Read-only, immutable SQLite URI. Percent-encode the path so a home dir containing
    ``?`` or ``#`` (URI delimiters) doesn't make SQLite open the wrong file."""
    from urllib.parse import quote

    return f"file:{quote(str(db))}?mode=ro&immutable=1"


def _safe_component(value: object, fallback: str) -> str:
    """A single, filesystem-safe path component: no separators or ``.``/``..`` traversal, so an
    absolute or dotted opencode id can't escape the cache root when joined with ``/``."""
    text = str(value or fallback)
    for sep in ("/", "\\", "\x00"):
        text = text.replace(sep, "_")
    return text.strip(". ") or fallback


def _opencode_json(raw: str) -> dict:
    # TypeError guards a SQL NULL data column (json.loads(None) raises TypeError, not ValueError).
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {"value": obj}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _opencode_synthesize(con: sqlite3.Connection, session_id: str) -> bytes:
    """One NDJSON line per message (parts merged), ordered by creation time. b'' if empty."""
    lines: list[str] = []
    is_v2 = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='session_message'").fetchone()[0] > 0
    if is_v2:
        cur_dir = con.execute("SELECT directory FROM session_v2 WHERE id = ?", (session_id,)).fetchone()
        session_cwd = cur_dir[0] if cur_dir and cur_dir[0] else ""

        messages = con.execute(
            "SELECT id, type, time_created, data FROM session_message WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
        for msg_id, msg_type, time_created, msg_data in messages:
            msg_obj = _opencode_json(msg_data)
            if not isinstance(msg_obj, dict):
                msg_obj = {}

            if "role" not in msg_obj:
                msg_obj["role"] = msg_type if msg_type in ("user", "assistant") else "user"

            if "modelID" not in msg_obj and isinstance(msg_obj.get("model"), dict):
                msg_obj["modelID"] = msg_obj["model"].get("modelID")

            if session_cwd and "path" not in msg_obj:
                msg_obj["path"] = {"cwd": session_cwd}

            if time_created:
                if "time" not in msg_obj or not isinstance(msg_obj.get("time"), dict):
                    msg_obj["time"] = {"created": time_created}
                elif "created" not in msg_obj["time"]:
                    msg_obj["time"]["created"] = time_created

            parts_list = []
            if msg_obj["role"] == "user":
                user_text = msg_obj.get("text") or ""
                if user_text:
                    parts_list.append({"type": "text", "text": str(user_text)})
                elif isinstance(msg_obj.get("content"), list):
                    for c in msg_obj["content"]:
                        if isinstance(c, dict):
                            parts_list.append(dict(c))
            else:
                content = msg_obj.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        p = dict(item)
                        if p.get("type") == "tool":
                            st = dict(p.get("state") or {})
                            if "output" not in st and "content" in st:
                                cnt = st["content"]
                                if isinstance(cnt, list):
                                    st["output"] = "\n".join(
                                        str(c.get("text", ""))
                                        for c in cnt
                                        if isinstance(c, dict) and "text" in c
                                    )
                                elif isinstance(cnt, str):
                                    st["output"] = str(cnt)
                            p["state"] = st
                        parts_list.append(p)
                elif isinstance(content, str):
                    parts_list.append({"type": "text", "text": content})

            record = {
                "id": msg_id,
                "sessionID": session_id,
                "message": msg_obj,
                "parts": parts_list,
            }
            lines.append(json.dumps(record, separators=(",", ":")))
    else:
        messages = con.execute(
            "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
        for msg_id, msg_data in messages:
            parts = con.execute(
                "SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id",
                (msg_id,),
            ).fetchall()
            record = {
                "id": msg_id,
                "sessionID": session_id,
                "message": _opencode_json(msg_data),
                "parts": [_opencode_json(p[0]) for p in parts],
            }
            lines.append(json.dumps(record, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode() if lines else b""


def _pin_cache_mtime(path: Path, epoch_ms: float) -> bool:
    """Set a materialized cache file's mtime, and CONFIRM it took.

    Other readers use this file's filesystem mtime as the session's timestamp, so a
    failed pin is not cosmetic: the file keeps the moment it was written, the
    session re-qualifies for the look-back window, and the unchanged-content guard
    then prevents any later run from correcting it.

    The result is checked rather than assumed because some filesystems accept
    `utime` and quietly ignore it — a silent no-op would look identical to
    success and reintroduce the bug it exists to prevent.

    Args:
        path: The materialized cache file.
        epoch_ms: Target modification time, epoch milliseconds.

    Returns:
        True when the file's mtime now matches the target within a second.
        False on any failure, so the caller can report it instead of skipping
        the next write for a file whose timestamp is wrong.
    """
    try:
        when = float(epoch_ms) / 1000
        # `< 0`, not `<= 0`. Epoch zero is a real — if bizarre — modification
        # time, and the RESTORE path can legitimately carry it; rejecting it
        # would leave that file with its write time, which is the one outcome
        # this helper exists to prevent. "No timestamp at all" is the caller's
        # question, and every caller already tests it before calling.
        if when < 0:
            return False
        # Upper bound too: the timestamps come from a hand-rolled protobuf walk
        # of a possibly hot/corrupt db, and `os.utime` happily ACCEPTS an
        # insane value (year 316-million). Once pinned, the file's real mtime
        # is insane as well, and the discovery loop's `st.st_mtime` fallback,
        # the guard for exactly this corruption, would raise instead of
        # saving the session. A year of clock skew is the generous ceiling;
        # beyond it the value is corruption, not a date, and the caller's
        # warning path is the right outcome.
        if when > time.time() + 365 * 86400:
            return False
        os.utime(path, (when, when))
        return abs(path.stat().st_mtime - when) <= 1
    except (OSError, OverflowError, TypeError, ValueError):
        return False


def find_opencode_candidates(
    home: HomeSpec, *, cache_root: Path | None = None
) -> Iterator[Session]:
    """Yield a Session per opencode session, materializing each into an NDJSON transcript.

    ``home.path`` is the opencode data dir (holds ``opencode.db``). Each session is synthesized
    to ``<cache_root>/<home_label>/<project_id>/<session_id>.jsonl`` and that file becomes the
    Session's ``abs_path`` - so scanning, redaction, and dedup are all unchanged.
    """
    db = _opencode_db(home.path)
    if db is None:
        logger.debug("Skipping %s (no opencode db)", home.path)
        return
    root = (cache_root or opencode_cache_root()) / home.label
    try:
        con = sqlite3.connect(_sqlite_ro_uri(db), uri=True)
    except sqlite3.Error as exc:
        _warn_unreadable_db("opencode", db, exc)
        return
    try:
        con.row_factory = sqlite3.Row
        try:
            is_v2 = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='session_v2'").fetchone()[0] > 0
            session_tbl = "session_v2" if is_v2 else "session"
            rows = con.execute(
                f"SELECT id, project_id, directory, time_updated FROM {session_tbl}"
            ).fetchall()
        except sqlite3.Error as exc:
            _warn_unreadable_db("opencode", db, exc)
            return
        for s in rows:
            # A schema mismatch (missing message/part table) raises sqlite3.Error mid-generator;
            # contain it per-session so one bad db doesn't abort the whole run.
            try:
                data = _opencode_synthesize(con, s["id"])
            except sqlite3.Error as exc:
                logger.debug("opencode synthesize failed for session %s: %s", s["id"], exc)
                continue
            if not data:
                continue
            # Sanitize both components: an absolute or ``..``-laden project_id/session id would
            # otherwise let ``root / rel`` write outside the cache (Path drops the left side on an
            # absolute right side).
            rel = Path(_safe_component(s["project_id"], "unknown")) / (
                f"{_safe_component(s['id'], 'session')}.jsonl"
            )
            abs_path = root / rel
            # Same contract as the Cursor composer cache: other readers use
            # these files' mtimes as the session's timestamp, so a file is only
            # rewritten when its bytes change. An unconditional rewrite would
            # stamp every OpenCode session "now" and re-qualify all of them for
            # the look-back window on every run.
            updated_ms = s["time_updated"] or 0
            # Same contract as the composer materializer: an undatable session
            # must keep the timestamp it already had rather than gaining the
            # write time, which would re-qualify it for the look-back window.
            try:
                prior_mtime = abs_path.stat().st_mtime if abs_path.is_file() else None
            except OSError:
                prior_mtime = None
            try:
                unchanged = (
                    abs_path.is_file()
                    and abs_path.stat().st_size == len(data)
                    and abs_path.read_bytes() == data
                )
            except OSError:
                unchanged = False
            try:
                if not unchanged:
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = abs_path.with_name(abs_path.name + ".tmp")
                    tmp_path.write_bytes(data)
                    os.replace(tmp_path, abs_path)
            except OSError as exc:
                logger.debug("opencode materialize failed %s: %s", abs_path, exc)
                with contextlib.suppress(OSError, UnboundLocalError):
                    tmp_path.unlink()
                continue
            if not updated_ms:
                if prior_mtime is not None and not _pin_cache_mtime(abs_path, prior_mtime * 1000):
                    logger.debug(
                        "opencode: could not restore the previous cache mtime "
                        "for %s; this undated session will be re-read on the "
                        "next run until it can",
                        abs_path,
                    )
            elif not _pin_cache_mtime(abs_path, updated_ms):
                logger.debug(
                    "opencode: could not pin cache mtime for %s; this session "
                    "will be re-read on the next run until it can",
                    abs_path,
                )
            directory = s["directory"]
            try:
                mtime = datetime.fromtimestamp(updated_ms / 1000, tz=UTC)
            except (OverflowError, OSError, TypeError, ValueError):
                # A corrupt time_updated skips the timestamp, not the session or the run.
                try:
                    mtime = datetime.fromtimestamp(abs_path.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
            yield Session(
                agent=AgentKind.OPENCODE,
                home_label=home.label,
                abs_path=abs_path,
                rel_path=rel,
                size_bytes=len(data),
                mtime=mtime,
                cwd=Path(directory) if directory else None,
                secret_findings=[],
            )
    finally:
        con.close()


# ----- Discovery: Cursor store.db -------------------------------------------
#
# Cursor's CLI/agent surfaces persist each session as its own SQLite file, under one of
# two routes in the Cursor home: ``chats/<workspace-hash>/<session-uuid>/store.db`` or
# ``acp-sessions/<session-uuid>/store.db`` (+ sibling ``meta.json``). Format
# (reverse-engineered):
#
#   meta table   key '0' -> HEX-encoded JSON {agentId, latestRootBlobId, name, mode, createdAt(ms)}
#   blobs table  content-addressed (id = sha256): root blob = protobuf; repeated field 1 =
#                32-byte child blob ids IN CONVERSATION ORDER; field 9 = workspace file://
#                URI; field 22 = surface ("cli"); field 26 = updatedAt ms; field 27 = IANA
#                timezone. Child blobs = JSON messages {role, content, providerOptions, id}.
#
# The dbs are WAL-mode and often hot: recent data lives in store.db-wal, and an
# immutable=1 read of the bare file can see EMPTY tables. So each read copies
# db(+wal/+shm) to a temp dir first — never the live files. As with opencode, each
# session is synthesized into an NDJSON transcript (header line + one line per message)
# and materialized under a cache root so the rest of the pipeline treats it like any
# other transcript file.

def cursor_store_cache_root() -> Path:
    return cache_root() / "cursor-store"

# Root-blob protobuf field numbers (see module docstring above).
_CURSOR_F_CHILD_ID = 1
_CURSOR_F_CONTEXT = 5
_CURSOR_F_WORKSPACE_URI = 9
_CURSOR_F_SURFACE = 22
_CURSOR_F_UPDATED_AT = 26
_CURSOR_F_TIMEZONE = 27
# Context-stats sub-fields (nested in field 5).
_CURSOR_F_CTX_USED = 1
_CURSOR_F_CTX_WINDOW = 2


def _cursor_read_varint(data: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, i


def _cursor_parse_protobuf(data: bytes) -> list[tuple[int, int, object]]:
    """Minimal protobuf wire decode: [(field, wire_type, value), ...].

    Only wire types 0/1/2/5 (all the root blob uses). Stops at the first malformed
    byte and returns what parsed so far — never raises.
    """
    out: list[tuple[int, int, object]] = []
    i, n = 0, len(data)
    try:
        while i < n:
            tag, i = _cursor_read_varint(data, i)
            fnum, wire = tag >> 3, tag & 7
            if wire == 0:
                value, i = _cursor_read_varint(data, i)
            elif wire == 1:
                value, i = data[i : i + 8], i + 8
            elif wire == 2:
                length, i = _cursor_read_varint(data, i)
                if i + length > n:
                    break
                value, i = data[i : i + length], i + length
            elif wire == 5:
                value, i = data[i : i + 4], i + 4
            else:  # groups (3/4) / invalid — can't know the length, stop here
                break
            out.append((fnum, wire, value))
    except IndexError:
        pass  # truncated varint at end of buffer
    return out


def _cursor_decode_meta(raw: object) -> dict | None:
    """meta.value is hex-encoded JSON (text or bytes). None if undecodable."""
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(bytes.fromhex(raw.strip()))
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _cursor_utf8(value: bytes) -> str | None:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _cursor_uri_to_path(uri: str) -> str | None:
    """``file:///Users/x/proj`` -> ``/Users/x/proj``; None for a non-``file`` URI."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    if parsed.scheme != "file" or not parsed.path:
        return None
    return unquote(parsed.path)


def _cursor_read_copied(db: Path) -> tuple[dict, list[dict]] | None:
    """Decode a (temp-copied) store.db -> (header, ordered messages). Raises sqlite3.Error."""
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='0'").fetchone()
        if row is None:
            return None
        meta = _cursor_decode_meta(row[0])
        if meta is None:
            return None

        header: dict = {
            "agentId": meta.get("agentId"),
            "name": meta.get("name") if isinstance(meta.get("name"), str) else None,
            "createdAt": meta.get("createdAt") if isinstance(meta.get("createdAt"), int) else None,
            "updatedAt": None,
            "surface": None,
            "timezone": None,
            "cwd": None,
            "contextUsed": None,
            "contextWindow": None,
        }

        root_id = meta.get("latestRootBlobId")
        root = None
        if isinstance(root_id, str):
            hit = con.execute("SELECT data FROM blobs WHERE id=?", (root_id,)).fetchone()
            root = hit[0] if hit else None
        if not isinstance(root, bytes):
            return header, []  # meta-only session; caller skips it (zero messages)

        child_ids: list[str] = []
        for fnum, wire, value in _cursor_parse_protobuf(root):
            if fnum == _CURSOR_F_CHILD_ID and wire == 2 and len(value) == 32:
                child_ids.append(value.hex())
            elif fnum == _CURSOR_F_CONTEXT and wire == 2:
                for sub_f, sub_w, sub_v in _cursor_parse_protobuf(value):
                    if sub_w != 0:
                        continue
                    if sub_f == _CURSOR_F_CTX_USED:
                        header["contextUsed"] = sub_v
                    elif sub_f == _CURSOR_F_CTX_WINDOW:
                        header["contextWindow"] = sub_v
            elif fnum == _CURSOR_F_WORKSPACE_URI and wire == 2:
                path = _cursor_uri_to_path(_cursor_utf8(value) or "")
                if path:
                    header["cwd"] = path
            elif fnum == _CURSOR_F_SURFACE and wire == 2:
                header["surface"] = _cursor_utf8(value)
            elif fnum == _CURSOR_F_UPDATED_AT and wire == 0:
                header["updatedAt"] = value
            elif fnum == _CURSOR_F_TIMEZONE and wire == 2:
                header["timezone"] = _cursor_utf8(value)

        messages: list[dict] = []
        for cid in child_ids:
            hit = con.execute("SELECT data FROM blobs WHERE id=?", (cid,)).fetchone()
            if hit is None:
                continue  # child blob missing — skip it, keep the rest of the conversation
            try:
                msg = json.loads(hit[0])
            except (TypeError, ValueError):
                continue
            if isinstance(msg, dict) and "role" in msg:
                messages.append(msg)
        return header, messages
    finally:
        con.close()


def _cursor_read_store_db(db_path: Path) -> tuple[dict, list[dict]] | None:
    """WAL-safe decode of one store.db: copies db(+wal/+shm) to a temp dir first.

    Never raises — returns None on any I/O or decode failure. ``header["cwd"]`` is
    overridden from the sibling ``meta.json`` (acp-sessions route) when present.
    """
    if not db_path.exists():
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="cursor_store_") as tmp:
            copy = Path(tmp) / "store.db"
            shutil.copy2(db_path, copy)
            for suffix in ("-wal", "-shm"):
                side = db_path.with_name(db_path.name + suffix)
                if side.exists():
                    shutil.copy2(side, copy.with_name(copy.name + suffix))
            result = _cursor_read_copied(copy)
    except (OSError, sqlite3.Error) as exc:
        _warn_unreadable_db("cursor_store", db_path, exc)
        return None
    if result is None:
        return None
    header, messages = result

    # Root-blob field 26 (updatedAt) goes stale mid-session — Cursor bumps it on
    # meta events, not per message (seen live: field frozen ~3h behind a WAL that
    # kept appending messages). The db/-wal mtimes track the last real write, so
    # session end = newest of the three; flows into the NDJSON header and the
    # Session.mtime recency guard.
    disk_ms = 0
    for f in (db_path, db_path.with_name(db_path.name + "-wal")):
        with contextlib.suppress(OSError):
            disk_ms = max(disk_ms, f.stat().st_mtime_ns // 1_000_000)
    header["updatedAt"] = max(header.get("updatedAt") or 0, disk_ms) or None

    # acp-sessions keeps cwd in a sibling meta.json (no trailing newline); it wins over
    # the field-9 workspace URI when present.
    meta_json = db_path.parent / "meta.json"
    if meta_json.exists():
        try:
            sidecar = json.loads(meta_json.read_bytes())
            if isinstance(sidecar, dict) and isinstance(sidecar.get("cwd"), str):
                header["cwd"] = sidecar["cwd"]
        except (OSError, ValueError):
            logger.debug("cursor_store: bad meta.json next to %s", db_path)
    return header, messages


def _find_cursor_store_dbs(home: Path) -> list[Path]:
    """All ``store.db`` files under a Cursor home's ``chats/`` + ``acp-sessions/``."""
    return sorted([*home.glob("chats/*/*/store.db"), *home.glob("acp-sessions/*/store.db")])


def _cursor_last_message_model(messages: list[dict]) -> str | None:
    """Last ``providerOptions.cursor.modelName`` in conversation order (message or part).

    Stamped on the header when the store records no model of its own; the session's
    model is the last one it used.
    """
    last: str | None = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        po = msg.get("providerOptions")
        if isinstance(po, dict):
            cur = po.get("cursor")
            if isinstance(cur, dict) and isinstance(cur.get("modelName"), str) and cur["modelName"]:
                last = cur["modelName"]
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ppo = part.get("providerOptions")
                if isinstance(ppo, dict):
                    cur = ppo.get("cursor")
                    if (
                        isinstance(cur, dict)
                        and isinstance(cur.get("modelName"), str)
                        and cur["modelName"]
                    ):
                        last = cur["modelName"]
    return last


def find_cursor_store_candidates(
    home: HomeSpec, *, cache_root: Path | None = None
) -> Iterator[Session]:
    """Yield a Session per Cursor CLI/agent store.db, synthesizing an NDJSON transcript.

    ``home.path`` is the Cursor home (holds ``chats/`` and/or ``acp-sessions/``). Each
    session is materialized to ``<cache_root>/<home_label>/<session-uuid>.jsonl`` — a
    ``cursor_store_session`` header line, then one line per message verbatim — and that
    file becomes the Session's ``abs_path``, mirroring ``find_opencode_candidates``.
    Sessions with zero messages are skipped (nothing useful to replay).
    """
    root = (cache_root or cursor_store_cache_root()) / home.label
    for db_path in _find_cursor_store_dbs(home.path):
        result = _cursor_read_store_db(db_path)
        if result is None:
            continue
        header, messages = result
        if not messages:
            continue
        session_id = header.get("agentId") or db_path.parent.name
        if not header.get("model"):
            msg_model = _cursor_last_message_model(messages)
            if msg_model:
                header = {**header, "model": msg_model}
        # Path-traversal-safe cache naming, same guard as opencode's project_id/session id.
        rel = Path(f"{_safe_component(session_id, 'session')}.jsonl")
        abs_path = root / rel
        record = {"type": "cursor_store_session", **header}
        lines = [json.dumps(record, separators=(",", ":"))]
        lines.extend(json.dumps(m, separators=(",", ":")) for m in messages)
        data = ("\n".join(lines) + "\n").encode()
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            # Only write when the bytes actually differ. Other readers use
            # each file's MTIME as the session's timestamp, so an unconditional
            # rewrite would make every materialized Cursor session look newer
            # than it is and re-process the entire cache on every run.
            # Prior mtime captured before any write, for the undated case
            # below — same contract as the composer and OpenCode materializers.
            try:
                prior_mtime = abs_path.stat().st_mtime if abs_path.is_file() else None
            except OSError:
                prior_mtime = None
            if not (abs_path.is_file() and abs_path.read_bytes() == data):
                # Atomic, like its siblings: with the unchanged-content skip
                # above, a .jsonl truncated by a crash mid-write would never be
                # rewritten.
                tmp_path = abs_path.with_name(abs_path.name + ".tmp")
                tmp_path.write_bytes(data)
                os.replace(tmp_path, abs_path)
            # Stamp with the STORE's own activity time, not "now" — and do it
            # whether or not we just wrote.
            #
            # Readers take st_mtime as the session's timestamp and apply a
            # `--hours` look-back against it. A
            # file left at the filesystem clock makes a months-old Cursor session
            # look like it happened this minute: it slips through the look-back
            # and drags the high-water mark forward, which then hides everything
            # genuinely older.
            #
            # Applied OUTSIDE the fresh-write branch on purpose: a cache file
            # whose bytes are unchanged but whose mtime is wrong is corrected
            # too, since the unchanged-content guard would otherwise never
            # revisit it. `os.utime` on an already-correct file is a no-op for
            # mtime, so this stays idempotent.
            stamp_ms = header.get("updatedAt") or header.get("createdAt") or 0
            # Through the verifying helper, like the other two materializers:
            # a bare `os.utime` that silently no-ops would leave the write time
            # in place. Both fields here are raw record ints, where 0 means
            # unset, so the composer path's epoch-zero distinction does not
            # apply to this materializer.
            if not stamp_ms:
                # Undatable: keep the timestamp the file already carried rather
                # than letting a rewrite advance it to "now".
                if prior_mtime is not None and not _pin_cache_mtime(abs_path, prior_mtime * 1000):
                    logger.debug(
                        "cursor_store: could not restore the previous cache "
                        "mtime for %s; this undated session will be re-read "
                        "on the next run until it can",
                        abs_path,
                    )
            elif not _pin_cache_mtime(abs_path, stamp_ms):
                logger.debug(
                    "cursor_store: could not pin cache mtime for %s; this "
                    "session will be re-read on the next run until it can",
                    abs_path,
                )
        except OSError as exc:
            logger.debug("cursor_store materialize failed %s: %s", abs_path, exc)
            continue
        try:
            st = abs_path.stat()
        except OSError:
            continue

        cwd = header.get("cwd")
        mtime_ms = header.get("updatedAt") or header.get("createdAt") or 0
        try:
            mtime = datetime.fromtimestamp(mtime_ms / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            # updatedAt comes from a hand-rolled protobuf walk of a possibly
            # hot/corrupt db — an insane value must not abort ALL discovery.
            mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
        yield Session(
            agent=AgentKind.CURSOR,
            home_label=home.label,
            abs_path=abs_path,
            rel_path=rel,
            size_bytes=st.st_size,
            mtime=mtime,
            cwd=Path(cwd) if cwd else None,
            secret_findings=[],
        )


# ----- Discovery: Cursor IDE composers (state.vscdb) -------------------------
#
# The IDE keeps composer chats in its global ``state.vscdb`` rather than in a
# per-session store.db. They are synthesized into the same NDJSON shape as
# store.db (``cursor_store_session`` header + messages) so one parser handles both.

def cursor_composer_cache_root() -> Path:
    return cache_root() / "cursor-composer"
_CURSOR_COMPOSER_HOME_LABEL = "cursor-ide"


def _default_cursor_state_db() -> Path:
    """Cursor's global ``state.vscdb``."""
    return _default_state_db_path()


def _cursor_composer_cwd(composer: dict) -> str | None:
    """Workspace path from ``workspaceIdentifier.uri``, if present."""
    ws = composer.get("workspaceIdentifier")
    if not isinstance(ws, dict):
        return None
    uri = ws.get("uri")
    if isinstance(uri, dict):
        for key in ("fsPath", "path"):
            val = uri.get(key)
            if isinstance(val, str) and val:
                return val
        external = uri.get("external")
        if isinstance(external, str) and external:
            return _cursor_uri_to_path(external)
    elif isinstance(uri, str) and uri:
        return _cursor_uri_to_path(uri)
    return None


_CURSOR_CONTEXT_PATH_CAP = 200


def _cursor_composer_context_paths(composer: dict) -> list[str]:
    """Structured file paths an IDE composer record references, for repo attribution.

    Composer chats rarely run ``git remote`` and rarely open a PR, and their
    transcripts carry few tool-call file paths - so all three git-free resolver tiers
    miss and the session ends up unattributed. Cursor does record real paths, just
    never anywhere the parser looks, and far more composer records carry one of
    these than carry a ``workspaceIdentifier`` cwd.

    Collected here, most-trustworthy first:

    * ``trackedGitRepos[].repoPath`` and ``gitWorktree`` roots — literal repo
      roots, so a leaf match is as good as a git remote;
    * ``newlyCreatedFiles[].uri`` — the dominant carrier in practice;
    * ``context.selections[].uri`` — files the user explicitly attached;
    * ``originalFileStates`` — whose dict KEYS are ``file://`` URIs of every
      file the session edited;
    * ``workspaceIdentifier.uri``, the same value ``_cursor_composer_cwd``
      returns, included because repo attribution reads file paths, not ``cwd``.

    For the same reason these travel as their own header field rather than being
    folded into ``cwd``.

    Ordering is preserved and duplicates dropped, so the strongest signal leads
    and the downstream tally sees each path once. Capped at
    ``_CURSOR_CONTEXT_PATH_CAP`` because ``originalFileStates`` grows with the
    session and the header must stay a single NDJSON line.

    Args:
        composer: One decoded ``composerData:`` record.

    Returns:
        Absolute path strings (``file://`` scheme stripped, percent-decoded), or
        ``[]`` when the record references none. A Windows URI decodes with a
        leading slash before the drive (``/C:/Users/...``); harmless here because
        the consuming tier matches path SEGMENTS, not prefixes.
    """
    out: list[str] = []
    seen: set[str] = set()

    def each(node: object) -> list:
        """Iterate only a genuine list.

        ``x or []`` still hands a truthy SCALAR straight to ``for``, and the
        resulting TypeError escapes ``_cursor_read_composers`` — which catches
        only OSError/sqlite3.Error — so one malformed record would abort composer
        discovery for the whole machine, not just skip itself.
        """
        return node if isinstance(node, list) else []

    def add(value: object) -> None:
        if not isinstance(value, str) or not value:
            return
        path = _cursor_uri_to_path(value) if value.startswith("file://") else value
        if not path or path in seen:
            return
        seen.add(path)
        out.append(path)

    def add_uri(node: object) -> None:
        """A vscode URI, stored either as an object or a bare string."""
        if isinstance(node, dict):
            for key in ("fsPath", "path", "external"):
                if isinstance(node.get(key), str) and node[key]:
                    add(node[key])
                    return
        elif isinstance(node, str):
            add(node)

    for repo in each(composer.get("trackedGitRepos")):
        if isinstance(repo, dict):
            add(repo.get("repoPath"))
    worktree = composer.get("gitWorktree")
    if isinstance(worktree, dict):
        add(worktree.get("worktreePath"))
        add(worktree.get("mainWorktreePath"))
    for created in each(composer.get("newlyCreatedFiles")):
        if isinstance(created, dict):
            add_uri(created.get("uri"))
    context = composer.get("context")
    if isinstance(context, dict):
        for selection in each(context.get("selections")):
            if isinstance(selection, dict):
                add_uri(selection.get("uri"))
    states = composer.get("originalFileStates")
    if isinstance(states, dict):
        for key in states:
            add(key)
    workspace = composer.get("workspaceIdentifier")
    if isinstance(workspace, dict):
        add_uri(workspace.get("uri"))

    return out[:_CURSOR_CONTEXT_PATH_CAP]


def _cursor_bubble_to_messages(bubble: dict) -> list[dict]:
    """One IDE bubble → zero-or-more NDJSON messages the store parser understands.

    The bubble's ``createdAt`` (an ISO-8601 UTC string in real state.vscdb data,
    e.g. ``"2025-10-02T06:43:45.379Z"``) is forwarded onto every emitted message
    because it is the only per-message timestamp Cursor persists, and the parser
    times each turn from it. Store.db (CLI/ACP) messages carry no equivalent.
    """
    btype = bubble.get("type")
    text = bubble.get("text") if isinstance(bubble.get("text"), str) else ""
    bubble_id = bubble.get("bubbleId")
    created_at = bubble.get("createdAt") if isinstance(bubble.get("createdAt"), str) else None
    model = None
    mi = bubble.get("modelInfo")
    if isinstance(mi, dict) and isinstance(mi.get("modelName"), str):
        model = mi["modelName"]
    provider = {"cursor": {"modelName": model}} if model else None

    out: list[dict] = []
    if btype == 1:  # user
        msg: dict = {"role": "user", "content": text}
        if bubble_id:
            msg["id"] = bubble_id
        if created_at:
            msg["createdAt"] = created_at
        if provider:
            msg["providerOptions"] = provider
        out.append(msg)
        return out

    # assistant (type 2) and anything else with signal
    tfd = bubble.get("toolFormerData")
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    tool_name = None
    tool_call_id = None
    tool_args: dict | str = {}
    tool_result = None
    tool_error = False
    if isinstance(tfd, dict) and tfd:
        name = tfd.get("name") or tfd.get("toolName")
        if isinstance(name, str) and name:
            tool_name = name
            tool_call_id = tfd.get("toolCallId") or f"call-{bubble_id or 'x'}"
            raw_args = tfd.get("params") if tfd.get("params") is not None else tfd.get("rawArgs")
            if isinstance(raw_args, dict):
                tool_args = raw_args
            elif isinstance(raw_args, str) and raw_args:
                try:
                    parsed = json.loads(raw_args)
                    tool_args = parsed if isinstance(parsed, dict) else {"raw": raw_args}
                except (TypeError, ValueError):
                    tool_args = {"raw": raw_args}
            parts.append(
                {
                    "type": "tool-call",
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "args": tool_args,
                }
            )
            result = tfd.get("result")
            if result is not None:
                tool_result = result if isinstance(result, str) else json.dumps(result)
            status = tfd.get("status") or tfd.get("state")
            if (
                (
                    isinstance(status, str)
                    and ("error" in status.lower() or "fail" in status.lower())
                )
                or tfd.get("error")
                or tfd.get("isError")
            ):
                tool_error = True

    if not parts and not tool_name:
        return out

    asst: dict = {"role": "assistant", "content": parts}
    if bubble_id:
        asst["id"] = bubble_id
    if created_at:
        asst["createdAt"] = created_at
    if provider:
        asst["providerOptions"] = provider
    out.append(asst)

    if tool_name and tool_call_id is not None and tool_result is not None:
        tool_msg: dict = {
            "role": "tool",
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": tool_call_id,
                    "result": tool_result,
                    "isError": tool_error,
                }
            ],
        }
        if created_at:
            tool_msg["createdAt"] = created_at
        out.append(tool_msg)
    return out


def _cursor_like_escape(value: str) -> str:
    r"""Escape LIKE wildcards (% _ \) so a composer id is matched literally.

    Extracted (not inlined in an f-string) — Python 3.11 forbids backslash
    escapes inside f-string expressions.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cursor_read_composers(
    db_path: Path,
) -> list[tuple[dict, list[dict]]]:
    """Read every composer with ≥1 bubble from ``state.vscdb``. Never raises."""
    if not db_path.is_file():
        return []
    try:
        with contextlib.ExitStack() as stack:
            # Read the LIVE store, read-only, rather than copying it: the file
            # can be gigabytes, and copying the db and its WAL seconds apart can
            # produce a torn pair on a busy Cursor. A deferred read transaction
            # on WAL is a genuine snapshot and never blocks Cursor's own writers.
            try:
                con = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
                con.execute("BEGIN DEFERRED")
            except sqlite3.Error:
                # Some filesystems and older SQLite builds refuse a read-only
                # URI open of a WAL database. Snapshot it the old way rather
                # than losing the whole Cursor surface.
                tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="cursor_composer_"))
                copy = Path(tmp) / "state.vscdb"
                shutil.copy2(db_path, copy)
                for suffix in ("-wal", "-shm"):
                    side = Path(str(db_path) + suffix)
                    if side.is_file():
                        shutil.copy2(side, Path(str(copy) + suffix))
                con = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro", uri=True)
            try:
                try:
                    rows = con.execute(
                        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                    ).fetchall()
                except sqlite3.Error:
                    return []
                results: list[tuple[dict, list[dict]]] = []
                for key, value in rows:
                    if not isinstance(key, str) or not key.startswith("composerData:"):
                        continue
                    composer_id = key.split(":", 1)[1]
                    if not composer_id or composer_id == "empty-state-draft":
                        continue
                    try:
                        raw = value.decode() if isinstance(value, bytes) else value
                        composer = json.loads(raw)
                    except (TypeError, ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(composer, dict):
                        continue
                    try:
                        # Range, not LIKE. `ESCAPE` disables SQLite's LIKE
                        # optimisation outright, and the default
                        # case_sensitive_like=off disqualifies the BINARY
                        # autoindex anyway — so every one of these planned as a
                        # full table SCAN, 239 of them, ~9.9s per run. The
                        # prefix ends in ':' (0x3A), so ';' (0x3B) is exactly
                        # the next possible key: identical rows, via SEARCH.
                        prefix = f"bubbleId:{composer_id}:"
                        bubble_rows = con.execute(
                            "SELECT value FROM cursorDiskKV WHERE key >= ? AND key < ?",
                            (prefix, prefix[:-1] + ";"),
                        ).fetchall()
                    except sqlite3.Error:
                        continue
                    bubbles: list[dict] = []
                    for (bval,) in bubble_rows:
                        try:
                            braw = bval.decode() if isinstance(bval, bytes) else bval
                            bubble = json.loads(braw)
                        except (TypeError, ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(bubble, dict):
                            bubbles.append(bubble)
                    if not bubbles:
                        continue
                    bubbles.sort(key=lambda b: b.get("createdAt") or "")

                    model = None
                    mc = composer.get("modelConfig")
                    if isinstance(mc, dict) and isinstance(mc.get("modelName"), str):
                        model = mc["modelName"]
                    created = composer.get("createdAt")
                    updated = _cursor_composer_last_activity_ms(composer, bubbles)
                    header: dict = {
                        "agentId": composer_id,
                        "name": composer.get("name")
                        if isinstance(composer.get("name"), str)
                        else None,
                        "createdAt": created if isinstance(created, int) else None,
                        "updatedAt": updated if isinstance(updated, int) else None,
                        "surface": "ide",
                        "timezone": None,
                        "cwd": _cursor_composer_cwd(composer),
                        # Absent (not empty) when this composer referenced no
                        # files, so a reader can tell "no paths" from "old CLI".
                        **(
                            {"contextFiles": _ctx_paths}
                            if (_ctx_paths := _cursor_composer_context_paths(composer))
                            else {}
                        ),
                        "contextUsed": composer.get("contextTokensUsed")
                        if isinstance(composer.get("contextTokensUsed"), int)
                        else None,
                        "contextWindow": composer.get("contextTokenLimit")
                        if isinstance(composer.get("contextTokenLimit"), int)
                        else None,
                    }
                    if model:
                        header["model"] = model

                    messages: list[dict] = []
                    for bubble in bubbles:
                        messages.extend(_cursor_bubble_to_messages(bubble))
                    if not messages:
                        continue
                    results.append((header, messages))
                return results
            finally:
                con.close()
    except (OSError, sqlite3.Error) as exc:
        _warn_unreadable_db("cursor_composer", db_path, exc)
        return []


def _cursor_composer_last_activity_ms(composer: dict, bubbles: list[dict]) -> int | None:
    """Epoch-ms of a composer's newest real MESSAGE.

    The record-level ``lastUpdatedAt`` is not per-message truth. It is absent
    on most composer records, and it drifts in BOTH directions against the
    conversation's own messages: bumped without any message changing, and
    observed running days behind bubbles that kept arriving. Either direction
    makes it useless as a freshness signal.

    Bubble ``createdAt`` is the only per-message timestamp Cursor persists, so
    it is what "when did this session last do work" has to mean. Note the type
    split: bubbles carry an ISO-8601 STRING while the composer record carries
    epoch MILLISECONDS, which is why this returns ms and parses accordingly.

    Args:
        composer: The decoded ``composerData:`` record.
        bubbles: Its decoded ``bubbleId:`` records, in any order.

    Returns:
        Epoch milliseconds of the newest datable bubble; failing that the
        record's own fields, so a conversation whose bubbles carry no parseable
        timestamp still resolves to something real. None when nothing is
        datable at all — the caller must not substitute "now".
    """
    # None-sentinel, not 0: a bubble parsed as exactly epoch
    # zero is a real — if bizarre — timestamp, and a zero sentinel would
    # conflate it with "no datable bubble at all" and skip this branch.
    newest: int | None = None
    for bubble in bubbles:
        value = bubble.get("createdAt")
        if not isinstance(value, str) or not value:
            continue
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        ts = int(when.timestamp() * 1000)
        newest = ts if newest is None else max(newest, ts)
    if newest is not None:
        return newest
    for key in ("lastUpdatedAt", "conversationCheckpointLastUpdatedAt", "createdAt"):
        value = composer.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def find_cursor_composer_candidates(
    *,
    state_db: Path | None = None,
    cache_root: Path | None = None,
) -> Iterator[Session]:
    """Yield Sessions for IDE composer chats in ``state.vscdb``.

    Materializes the same ``cursor_store_session`` NDJSON shape as
    :func:`find_cursor_store_candidates`.
    """
    db_path = state_db or _default_cursor_state_db()
    composers = _cursor_read_composers(db_path)
    if not composers:
        return

    root = (cache_root or cursor_composer_cache_root()) / _CURSOR_COMPOSER_HOME_LABEL
    for header, messages in composers:
        session_id = header.get("agentId") or "session"
        if not header.get("model"):
            msg_model = _cursor_last_message_model(messages)
            if msg_model:
                header = {**header, "model": msg_model}

        rel = Path(f"{_safe_component(session_id, 'session')}.jsonl")
        abs_path = root / rel
        record = {"type": "cursor_store_session", **header}
        lines = [json.dumps(record, separators=(",", ":"))]
        lines.extend(json.dumps(m, separators=(",", ":")) for m in messages)
        data = ("\n".join(lines) + "\n").encode()
        cwd = header.get("cwd")
        # Split semantics: `updatedAt` is derived by
        # `_cursor_composer_last_activity_ms`, which already distinguishes
        # "no datable bubble" (None) from a genuine epoch-zero bubble (0) — so
        # it is read None-aware and a 0 is pinned as a real timestamp. Raw
        # record ints are different: a `createdAt: 0` means the field was
        # unset, exactly the way the derivation's own `> 0` gates read record
        # fields, so the fallback keeps the truthy gate.
        mtime_ms = header.get("updatedAt")
        if mtime_ms is None:
            mtime_ms = header.get("createdAt") or None

        # Readers take this file's FILESYSTEM mtime as the session's
        # timestamp, so rewriting every composer on every run would stamp all
        # of them "now" and re-qualify year-old conversations for the
        # look-back window. Skip the write when the bytes are identical, then
        # pin the mtime to the conversation's newest message.
        #
        # `prior_mtime` covers a conversation with no datable bubble AND no
        # record timestamp: there is nothing to pin to, so a later content
        # change would otherwise stamp it "now" and pull an undated
        # conversation into the look-back window. Keeping the timestamp the
        # file already had is the only honest choice.
        try:
            prior_mtime = abs_path.stat().st_mtime if abs_path.is_file() else None
        except OSError:
            prior_mtime = None
        try:
            unchanged = (
                abs_path.is_file()
                and abs_path.stat().st_size == len(data)
                and abs_path.read_bytes() == data
            )
        except OSError:
            unchanged = False
        if not unchanged:
            try:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                # Atomic: unchanged files are never rewritten, so a .jsonl
                # truncated by a crash mid-write would otherwise be permanent.
                tmp_path = abs_path.with_name(abs_path.name + ".tmp")
                tmp_path.write_bytes(data)
                os.replace(tmp_path, abs_path)
            except OSError as exc:
                logger.debug("cursor_composer materialize failed %s: %s", abs_path, exc)
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                continue
        if mtime_ms is None:
            # Undatable. Restore the timestamp the file already carried rather
            # than letting a rewrite advance it; a brand-new undated cache has
            # no prior evidence, so it keeps the write time and ages out of the
            # window normally instead of returning on every build.
            if prior_mtime is not None:
                # Through the verifying helper, not a bare suppressed utime.
                # An unverified restore has the same failure mode as an
                # unverified pin: the file silently keeps its write time and
                # the session re-qualifies on every build.
                if not _pin_cache_mtime(abs_path, prior_mtime * 1000):
                    logger.debug(
                        "cursor_composer: could not restore the previous cache "
                        "mtime for %s; this undated session will be re-read "
                        "on the next run until it can",
                        abs_path,
                    )
            else:
                logger.debug(
                    "cursor_composer: %s carries no datable message or record "
                    "timestamp; its cache keeps the write time",
                    abs_path,
                )
        elif not _pin_cache_mtime(abs_path, mtime_ms):
            # Do not skip the next write for a file whose timestamp we could
            # not pin. Silently continuing leaves the just-written mtime, which
            # is "now", and the unchanged-content guard would then make sure
            # this file is never revisited to correct it. Yield the session (the data is fine) but say so,
            # so a machine that cannot pin timestamps is diagnosable instead of
            # quietly leaving every session looking changed on each run.
            logger.debug(
                "cursor_composer: could not pin cache mtime for %s; this "
                "session will be re-read on the next run until it can",
                abs_path,
            )
        try:
            st = abs_path.stat()
        except OSError:
            continue
        try:
            # None = undated: the file's own (kept/restored) mtime is the only
            # evidence there is — and it is truer than the old `0 → 1970`
            # collapse this replaces.
            mtime = (
                datetime.fromtimestamp(mtime_ms / 1000, tz=UTC)
                if mtime_ms is not None
                else datetime.fromtimestamp(st.st_mtime, tz=UTC)
            )
        except (OverflowError, OSError, ValueError):
            mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
        yield Session(
            agent=AgentKind.CURSOR,
            home_label=_CURSOR_COMPOSER_HOME_LABEL,
            abs_path=abs_path,
            rel_path=rel,
            size_bytes=st.st_size,
            mtime=mtime,
            cwd=Path(cwd) if cwd else None,
            secret_findings=[],
        )


#
# A Kiro session is a DIRECTORY, not a file: sessions/<ws-hash>/<session-id>/ holds
# session.json (metadata) + messages.jsonl (event log), and sub-agent runs land in
# sub-executions/<execution-id>.jsonl. We synthesize one NDJSON per session — a
# `kiro_session` header line then the raw events, merged by timestamp — mirroring
# the cursor_store/opencode pattern so scan, redaction, and dedup are all
# unchanged downstream. Agent detection sniffs the header type.

def kiro_cache_root() -> Path:
    return cache_root() / "kiro"


def _count_kiro_sessions(home: Path) -> int:
    """Count session dirs under <home>/sessions/<ws-hash>/<session-id>/ (0 if none)."""
    sessions_root = home / "sessions"
    if not sessions_root.is_dir():
        return 0
    n = 0
    try:
        for ws_hash_dir in sessions_root.iterdir():
            if not ws_hash_dir.is_dir():
                continue
            for session_dir in ws_hash_dir.iterdir():
                if session_dir.is_dir() and (session_dir / "session.json").is_file():
                    n += 1
    except OSError:
        return n
    return n


def _kiro_cwd(session_meta: dict) -> Path | None:
    """The session's primary workspace root (``workspacePaths[0]``), else None.

    None means the session records no workspace, so it carries no cwd.
    """
    paths = session_meta.get("workspacePaths")
    if isinstance(paths, list) and paths:
        first = paths[0]
        if isinstance(first, str) and first.strip():
            return Path(first)
    return None


def _kiro_read_events(path: Path) -> list[dict]:
    """Parse one Kiro JSONL event log; unparseable/non-dict lines are dropped."""
    events: list[dict] = []
    try:
        data = path.read_bytes()
    except OSError:
        return events
    for raw_line in data.split(b"\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _kiro_source_mtime(messages_mtime: float, session_dir: Path) -> float:
    """Latest mtime across every file `_kiro_synthesize` reads for this session.

    That is ``messages.jsonl`` plus any ``sub-executions/*.jsonl``. The cache
    freshness guard compares against this so a sub-agent file updated after
    ``messages.jsonl`` still triggers re-synthesis — comparing against
    ``messages.jsonl`` alone would serve a cache missing those sub-agent events.
    A missing/unreadable ``sub-executions/`` dir falls back to ``messages_mtime``.
    """
    latest = messages_mtime
    sub_exec_dir = session_dir / "sub-executions"
    if sub_exec_dir.is_dir():
        try:
            for sub_file in sub_exec_dir.iterdir():
                if sub_file.suffix != ".jsonl":
                    continue
                with contextlib.suppress(OSError):
                    latest = max(latest, sub_file.stat().st_mtime)
        except OSError:
            pass
    return latest


def _kiro_synthesize(session_meta: dict, messages_path: Path, session_dir: Path) -> bytes | None:
    """Build one session's NDJSON: `kiro_session` header + events, merged by timestamp.

    Sub-execution (sub-agent) events are merged inline so the whole conversation —
    including sub-agent tool calls - travels as a single file.

    Returns None when the session has no ``turn_end`` event: an empty or
    metadata-only session has nothing worth replaying.
    """
    events = _kiro_read_events(messages_path)
    sub_exec_dir = session_dir / "sub-executions"
    if sub_exec_dir.is_dir():
        try:
            sub_files = sorted(p for p in sub_exec_dir.iterdir() if p.suffix == ".jsonl")
        except OSError:
            sub_files = []
        for sub_file in sub_files:
            events.extend(_kiro_read_events(sub_file))

    # Stable sort on timestamp: equal/absent timestamps keep file order (main
    # stream first, then each sub-execution in name order).
    events.sort(key=lambda e: str(e.get("timestamp") or ""))

    has_turns = any(
        isinstance(e.get("payload"), dict) and e["payload"].get("type") == "turn_end"
        for e in events
    )
    if not has_turns:
        return None

    # Marker key LAST so it always wins: if a future session.json carries its own
    # `type`, spreading it after would clobber the `kiro_session` marker and the
    # agent detection would fail to recognise the transcript (dropping it).
    header = {**session_meta, "type": "kiro_session"}
    lines = [json.dumps(header, separators=(",", ":"))]
    lines.extend(json.dumps(e, separators=(",", ":")) for e in events)
    return ("\n".join(lines) + "\n").encode()


def find_kiro_candidates(home: HomeSpec, *, cache_root: Path | None = None) -> Iterator[Session]:
    """Yield a Session per Kiro session, synthesizing an NDJSON transcript.

    ``home.path`` is the Kiro home (holds ``sessions/``). Each session is materialized
    to ``<cache_root>/<ws-hash>/<session-id>.jsonl`` and that file becomes the Session's
    ``abs_path``, mirroring ``find_cursor_store_candidates``.

    The cache is only rewritten when ``messages.jsonl`` is newer than it, so a re-run
    over unchanged sessions costs a stat() rather than a full re-synthesis.
    """
    sessions_root = home.path / "sessions"
    if not sessions_root.is_dir():
        logger.debug("kiro: no sessions dir under %s", home.path)
        return
    root = cache_root or kiro_cache_root()
    try:
        ws_hash_dirs = sorted(p for p in sessions_root.iterdir() if p.is_dir())
    except OSError as exc:
        logger.debug("kiro: cannot list %s: %s", sessions_root, exc)
        return

    for ws_hash_dir in ws_hash_dirs:
        ws_hash = _safe_component(ws_hash_dir.name, "unknown")
        try:
            session_dirs = sorted(p for p in ws_hash_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for session_dir in session_dirs:
            session_json_path = session_dir / "session.json"
            messages_path = session_dir / "messages.jsonl"
            if not session_json_path.is_file() or not messages_path.is_file():
                continue
            try:
                session_meta = json.loads(session_json_path.read_bytes())
            except (OSError, json.JSONDecodeError, ValueError):
                logger.debug("kiro: bad session.json in %s", session_dir)
                continue
            if not isinstance(session_meta, dict):
                continue
            try:
                messages_mtime = messages_path.stat().st_mtime
            except OSError:
                continue
            # Freshness is judged against every file the synthesis reads — not
            # just messages.jsonl. Sub-executions are merged in too, and a
            # sub-agent file updated after messages.jsonl would otherwise leave
            # the cache "fresh" while missing those events.
            source_mtime = _kiro_source_mtime(messages_mtime, session_dir)

            session_id = _safe_component(session_meta.get("id") or session_dir.name, "session")
            rel = Path(ws_hash) / f"{session_id}.jsonl"
            abs_path = root / rel
            cwd = _kiro_cwd(session_meta)

            fresh = False
            with contextlib.suppress(OSError):
                fresh = abs_path.stat().st_mtime >= source_mtime
            if not fresh:
                data = _kiro_synthesize(session_meta, messages_path, session_dir)
                if data is None:
                    continue
                # Atomic: freshness is judged by mtime, so a torn write would
                # otherwise look fresh and never be rewritten.
                tmp_path = abs_path.with_name(abs_path.name + ".tmp")
                try:
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path.write_bytes(data)
                    os.replace(tmp_path, abs_path)
                except OSError as exc:
                    logger.debug("kiro materialize failed %s: %s", abs_path, exc)
                    with contextlib.suppress(OSError):
                        tmp_path.unlink()
                    continue
            try:
                st = abs_path.stat()
            except OSError:
                continue
            yield Session(
                agent=AgentKind.KIRO,
                home_label=home.label,
                abs_path=abs_path,
                rel_path=rel,
                size_bytes=st.st_size,
                # messages.jsonl mtime, not the cache file's: the cache is rewritten
                # on re-synthesis and would otherwise report every session as "just now".
                mtime=datetime.fromtimestamp(messages_mtime, tz=UTC),
                cwd=cwd,
                secret_findings=[],
            )


# ----- Discovery: Goose sessions.db ------------------------------------------
#
# Goose (Block's open-source agent) keeps every session in one SQLite db at
# ``<data-dir>/sessions/sessions.db`` (older releases wrote one JSONL per session;
# Goose imports those into the db itself on startup). A session is one
# ``sessions`` row, which records ``working_dir``, plus its ``messages`` rows,
# whose ``content_json`` is the message's content-block array, and its
# ``usage_ledger`` rows, one per model request. The db is WAL-mode and live while
# Goose runs, so a consistent snapshot of it is copied to a temp dir before
# reading. Each session is synthesized into one NDJSON transcript: a
# ``goose_session`` header line, then its ``goose_message`` lines in order, each
# ``goose_usage`` line right after the assistant message it bills.
# The db has had many migrations, so only columns that exist are selected.

# Session types Goose itself hides from its session list: internal helper runs
# and subagents (whose work is not split into lanes yet).
_GOOSE_SKIPPED_TYPES = ("hidden", "sub_agent")

_GOOSE_SESSION_COLUMNS = (
    "id",
    "working_dir",
    "created_at",
    "updated_at",
    "session_type",
    "provider_name",
    "model_config_json",
    "parent_session_id",
)
_GOOSE_USAGE_COLUMNS = (
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost",
    "is_compaction",
)

# Goose stores message times in seconds; a value past this is milliseconds.
_GOOSE_MS_THRESHOLD = 10_000_000_000


def goose_cache_root() -> Path:
    return cache_root() / "goose"


def _goose_db(home: Path) -> Path:
    return home / "sessions" / "sessions.db"


def _goose_columns(con: sqlite3.Connection, table: str) -> set[str]:
    """The columns of ``table`` (empty when it does not exist). ``table`` is a constant."""
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _goose_epoch(value: object) -> float | None:
    """Epoch seconds from a Goose timestamp: an epoch number, or SQLite's UTC text."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            value = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
    if not isinstance(value, int | float) or value < 0:
        return None
    return value / 1000 if value > _GOOSE_MS_THRESHOLD else float(value)


def _goose_json(raw: object, kind: type) -> object | None:
    try:
        obj = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, kind) else None


def _goose_model(raw: object) -> str | None:
    config = _goose_json(raw, dict)
    name = config.get("model_name") if isinstance(config, dict) else None
    return name if isinstance(name, str) and name else None


def _goose_synthesize(
    con: sqlite3.Connection,
    session: dict,
    message_columns: set[str],
    usage_columns: set[str],
) -> tuple[bytes, float | None]:
    """One session's NDJSON and its last activity (epoch seconds). b'' when it has no messages."""
    session_id = session["id"]
    optional = [c for c in ("message_id", "metadata_json") if c in message_columns]
    rows = con.execute(
        f"SELECT id, role, content_json, created_timestamp{''.join(', ' + c for c in optional)} "
        "FROM messages WHERE session_id = ? ORDER BY created_timestamp, id",
        (session_id,),
    ).fetchall()
    messages: list[dict] = []
    for row in rows:
        content = _goose_json(row["content_json"], list)
        if content is None or row["role"] not in ("user", "assistant"):
            continue
        message_id = row["message_id"] if "message_id" in optional else None
        metadata = _goose_json(row["metadata_json"], dict) if "metadata_json" in optional else None
        messages.append(
            {
                "type": "goose_message",
                "id": message_id if isinstance(message_id, str) and message_id else str(row["id"]),
                "role": row["role"],
                "created": _goose_epoch(row["created_timestamp"]),
                "content": content,
                "metadata": metadata or {},
            }
        )
    if not messages:
        return b"", None

    # Each ledger row goes right after the last assistant message at or before
    # its time: the request that produced that message is the one it bills.
    # Placing it by time alone would hand a request's tokens to the next turn
    # whenever the next prompt lands in the same second.
    usage_after: dict[int, list[dict]] = {}
    if {"session_id", "created_timestamp"} <= usage_columns:
        fields = [c for c in _GOOSE_USAGE_COLUMNS if c in usage_columns]
        usage_rows = con.execute(
            f"SELECT created_timestamp{''.join(', ' + c for c in fields)} "
            "FROM usage_ledger WHERE session_id = ? ORDER BY created_timestamp, id",
            (session_id,),
        ).fetchall()
        assistant_at = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
        assistant_times = [messages[i]["created"] or 0.0 for i in assistant_at]
        for row in usage_rows:
            created = _goose_epoch(row["created_timestamp"])
            n = bisect.bisect_right(assistant_times, created) if created is not None else len(assistant_at)
            position = assistant_at[n - 1] if n else -1
            usage_after.setdefault(position, []).append(
                {"type": "goose_usage", "created": created, **{c: row[c] for c in fields}}
            )
    records = list(usage_after.get(-1, []))
    for i, message in enumerate(messages):
        records.append(message)
        records.extend(usage_after.get(i, []))

    created_at = _goose_epoch(session.get("created_at"))
    updated_at = _goose_epoch(session.get("updated_at"))
    last = max(
        [t for t in (updated_at, *(r["created"] for r in records)) if t is not None], default=None
    )
    header = {
        "id": session_id,
        "working_dir": session.get("working_dir"),
        "created_at": created_at,
        "updated_at": last,
        "session_type": session.get("session_type"),
        "provider": session.get("provider_name"),
        "model": _goose_model(session.get("model_config_json")),
        "parent_session_id": session.get("parent_session_id"),
        # Marker key last, so no column can clobber it.
        "type": "goose_session",
    }
    lines = [json.dumps(header, separators=(",", ":"))]
    lines.extend(json.dumps(r, separators=(",", ":")) for r in records)
    return ("\n".join(lines) + "\n").encode(), last


def _goose_write(abs_path: Path, data: bytes, updated_s: float | None) -> bool:
    """Write one cache file only when its bytes change, then pin its mtime.

    Same contract as the OpenCode cache: readers take a session's time from
    this file's mtime, so an unconditional rewrite would date every session
    "now". Returns False when the file could not be written.
    """
    try:
        prior_mtime = abs_path.stat().st_mtime if abs_path.is_file() else None
    except OSError:
        prior_mtime = None
    try:
        unchanged = (
            abs_path.is_file()
            and abs_path.stat().st_size == len(data)
            and abs_path.read_bytes() == data
        )
    except OSError:
        unchanged = False
    if not unchanged:
        tmp_path = abs_path.with_name(abs_path.name + ".tmp")
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(data)
            os.replace(tmp_path, abs_path)
        except OSError as exc:
            logger.debug("goose materialize failed %s: %s", abs_path, exc)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            return False
    target = updated_s if updated_s is not None else prior_mtime
    if target is not None and not _pin_cache_mtime(abs_path, target * 1000):
        logger.debug(
            "goose: could not pin cache mtime for %s; this session will be "
            "re-read on the next run until it can",
            abs_path,
        )
    return True


def _goose_snapshot(db: Path, copy: Path) -> None:
    """Write one consistent snapshot of the live ``db`` to ``copy``.

    SQLite's backup API copies the database as one read transaction sees it,
    WAL included, so a commit or checkpoint by a running Goose cannot leave
    the copy and its WAL describing different states (copying the files one by
    one can). The source is opened read-only. Should SQLite refuse to open it
    that way (a WAL file without its -shm in a read-only directory), the
    files are copied instead; a torn copy then only costs a stale session
    until the next run. Raises OSError or sqlite3.Error when neither works.
    """
    try:
        source = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return
    except sqlite3.Error as exc:
        logger.debug("goose: snapshot of %s failed (%s); copying the files", db, exc)
    for path in (copy, copy.with_name(copy.name + "-wal"), copy.with_name(copy.name + "-shm")):
        with contextlib.suppress(OSError):
            path.unlink()
    shutil.copy2(db, copy)
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            shutil.copy2(side, copy.with_name(copy.name + suffix))


def find_goose_candidates(home: HomeSpec, *, cache_root: Path | None = None) -> Iterator[Session]:
    """Yield a Session per Goose session, materializing each into an NDJSON transcript.

    ``home.path`` is the Goose data dir (holds ``sessions/sessions.db``). Each
    session is synthesized to ``<cache_root>/<home_label>/<session_id>.jsonl``.
    Subagent and hidden sessions are skipped.
    """
    db = _goose_db(home.path)
    if not db.is_file():
        logger.debug("Skipping %s (no goose sessions.db)", home.path)
        return
    root = (cache_root or goose_cache_root()) / home.label
    with tempfile.TemporaryDirectory(prefix="goose_sessions_") as tmp:
        copy = Path(tmp) / "sessions.db"
        try:
            _goose_snapshot(db, copy)
            con = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro", uri=True)
        except (OSError, sqlite3.Error) as exc:
            _warn_unreadable_db("goose", db, exc)
            return
        try:
            con.row_factory = sqlite3.Row
            try:
                session_columns = _goose_columns(con, "sessions")
                message_columns = _goose_columns(con, "messages")
                usage_columns = _goose_columns(con, "usage_ledger")
                if "id" not in session_columns or not {
                    "session_id", "role", "content_json", "created_timestamp"
                } <= message_columns:
                    raise sqlite3.DatabaseError("unrecognized sessions.db schema")
                wanted = [c for c in _GOOSE_SESSION_COLUMNS if c in session_columns]
                sessions = [
                    dict(row) for row in con.execute(f"SELECT {', '.join(wanted)} FROM sessions")
                ]
            except sqlite3.Error as exc:
                _warn_unreadable_db("goose", db, exc)
                return
            for s in sessions:
                if s.get("session_type") in _GOOSE_SKIPPED_TYPES or not s.get("id"):
                    continue
                try:
                    data, updated_s = _goose_synthesize(con, s, message_columns, usage_columns)
                except sqlite3.Error as exc:
                    logger.debug("goose synthesize failed for session %s: %s", s["id"], exc)
                    continue
                if not data:
                    continue
                rel = Path(f"{_safe_component(s['id'], 'session')}.jsonl")
                abs_path = root / rel
                if not _goose_write(abs_path, data, updated_s):
                    continue
                try:
                    mtime = datetime.fromtimestamp(
                        updated_s if updated_s is not None else abs_path.stat().st_mtime, tz=UTC
                    )
                except (OSError, OverflowError, ValueError):
                    continue
                working_dir = s.get("working_dir")
                yield Session(
                    agent=AgentKind.GOOSE,
                    home_label=home.label,
                    abs_path=abs_path,
                    rel_path=rel,
                    size_bytes=len(data),
                    mtime=mtime,
                    cwd=Path(working_dir) if isinstance(working_dir, str) and working_dir else None,
                    secret_findings=[],
                )
        finally:
            con.close()


# ----- Discovery: GitHub Copilot CLI -----------------------------------------
# Copilot CLI writes one JSONL event log per session, so unlike Cursor / Kiro /
# opencode nothing is synthesized: the on-disk file IS the transcript the parser's
# `parse_copilot` reads.
#   <home>/session-state/<session-id>/events.jsonl        Copilot CLI >= 1.0.11
#   <home>/session-state/<session-id>.jsonl               legacy flat layout
# Copilot creates the session directory (and an events.jsonl holding only
# `session.start` + a model-change line) the moment the binary starts, so most
# session folders on a machine never held a prompt; those are skipped.

