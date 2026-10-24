"""Antigravity CLI (`agy`) discovery for `LocalSource`.

`agy` keeps one step log per conversation at
``<home>/brain/<uuid>/.system_generated/logs/transcript_full.jsonl``. The step
log records no working directory and no parent conversation, so both come from
``<home>/conversation_summaries.db`` (see `antigravity_summaries`). The parser
itself lives in ``parsers/antigravity.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

from agentplay._core.ingestion.coding_agent._sources.local.parsers import AgentType, detect_agent

logger = logging.getLogger(__name__)

# Relative to an Antigravity CLI home.
TRANSCRIPT_GLOB = "brain/*/.system_generated/logs/transcript_full.jsonl"


def _path_from_file_uri(uri: object) -> str | None:
    """A local path from a ``file://`` URI, the same on every OS.

    ``file:///Users/dev/project`` gives ``/Users/dev/project`` and
    ``file:///C:/Users/dev/project`` gives ``C:/Users/dev/project`` (no leading
    slash before a drive letter), whichever OS reads it: the session may have
    been recorded on another machine. A host part (``file://server/share``) is
    kept as a UNC-style ``//server/share`` prefix.
    """
    if not isinstance(uri, str) or not uri.startswith("file:"):
        return None
    parts = urlsplit(uri)
    path = unquote(parts.path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    if parts.netloc and parts.netloc != "localhost":
        path = f"//{parts.netloc}{path}"
    return path or None


@dataclass(frozen=True)
class _AntigravitySummary:
    """The structural columns of one `conversation_summaries` row."""

    cwd: str | None
    parent_id: str | None


# Only structural columns. `title`, `preview` and `raw_summary` are prompt or
# model text and are never selected.
_ANTIGRAVITY_SUMMARY_SQL = (
    "SELECT conversation_id, workspace_uris, parent_conversation_id FROM conversation_summaries"
)


def _summary_file_stamp(path: Path) -> tuple[str, int | None, int | None]:
    """`(name, size, mtime_ns)` for one summaries file, or `(name, None, None)` when absent."""
    if not path.is_file():
        return (path.name, None, None)
    st = path.stat()
    return (path.name, st.st_size, st.st_mtime_ns)


def _antigravity_summary_files(db: Path) -> list[Path]:
    return [db.with_name(db.name + suffix) for suffix in ("", "-wal", "-shm")]


def _copy_antigravity_summaries(db: Path, tmp_dir: Path) -> None:
    """Copy the db and its sidecars without opening the original.

    `agy` may be writing the WAL while we copy. Stat size and mtime of the db,
    `-wal`, and `-shm` before and after each attempt, and retry up to 3 times
    when any of them changed (including a sidecar appearing or disappearing).
    A snapshot that is still moving after that is used as-is.
    """
    files = _antigravity_summary_files(db)
    for _ in range(3):
        before = tuple(_summary_file_stamp(path) for path in files)
        for src in files:
            dest = tmp_dir / src.name
            if src.is_file():
                shutil.copyfile(src, dest)
            elif dest.exists():
                dest.unlink()
        after = tuple(_summary_file_stamp(path) for path in files)
        if before == after:
            return
    logger.debug(
        "antigravity-cli: conversation summaries changed while copying; using the last copy"
    )


def _antigravity_summaries(home: Path) -> dict[str, _AntigravitySummary]:
    """cwd and parent conversation per conversation id, from `conversation_summaries.db`.

    The live database is in WAL mode and may be open in `agy`, so it is copied
    (with its `-wal` / `-shm` files when present) to a temporary directory and
    opened read-only there. Missing or unreadable: an empty dict, and sessions
    are indexed without a cwd.
    """
    db = home / "conversation_summaries.db"
    if not db.is_file():
        return {}
    rows: list[tuple] = []
    try:
        with tempfile.TemporaryDirectory(prefix="agentplay-antigravity-") as tmp:
            tmp_dir = Path(tmp)
            _copy_antigravity_summaries(db, tmp_dir)
            conn = sqlite3.connect(f"{(tmp_dir / db.name).as_uri()}?mode=ro", uri=True)
            try:
                rows = conn.execute(_ANTIGRAVITY_SUMMARY_SQL).fetchall()
            finally:
                # Closed before the directory is removed: Windows cannot delete an open file.
                conn.close()
    except (OSError, sqlite3.Error) as exc:
        logger.debug("antigravity-cli: conversation summaries unreadable: %s", exc)
        return {}
    out: dict[str, _AntigravitySummary] = {}
    for conversation_id, workspace_uris, parent_id in rows:
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        try:
            uris = json.loads(workspace_uris) if isinstance(workspace_uris, str) else None
        except (ValueError, RecursionError):
            uris = None
        cwd = None
        try:
            for uri in uris if isinstance(uris, list) else []:
                cwd = _path_from_file_uri(uri)
                if cwd:
                    break
        except ValueError:
            # urlsplit rejects a host like "[bad". Drop the row so one bad URI
            # cannot abort discovery of every other conversation.
            continue
        parent = parent_id if isinstance(parent_id, str) and parent_id else None
        out[conversation_id] = _AntigravitySummary(cwd=cwd, parent_id=parent)
    return out


def index_antigravity_home(
    home: Path,
    transcripts: Iterable[Path],
    record: Callable[[str, str], None],
) -> list[tuple[str, dict[str, Any]]]:
    """`(session_id, _FileRef fields)` for each Antigravity CLI step log in `home`.

    `transcripts` are the home's `TRANSCRIPT_GLOB` matches. `record` is
    `LocalSource._record_discovery`. The summaries db is read once per home,
    and only when the home has a `brain/` folder.
    """
    brain_dir = home / "brain"
    if not brain_dir.is_dir():
        logger.debug("Skipping non-existent path: %s", brain_dir)
        return []
    summaries = _antigravity_summaries(home)
    refs = []
    for jsonl_file in transcripts:
        ref = _index_antigravity_file(jsonl_file, home=home, summaries=summaries, record=record)
        if ref:
            refs.append(ref)
    return refs


def _index_antigravity_file(
    jsonl_file: Path,
    *,
    home: Path,
    summaries: dict[str, _AntigravitySummary],
    record: Callable[[str, str], None],
) -> tuple[str, dict[str, Any]] | None:
    """Index one Antigravity CLI `transcript_full.jsonl`."""
    # source.py imports this module, so its helpers are looked up at call time.
    from agentplay._core.ingestion.coding_agent._sources.local.source import (
        _agent_detected,
        _project_from_cwd,
        _read_head,
    )

    if not jsonl_file.is_file():
        return None
    size = jsonl_file.stat().st_size
    if size == 0:
        record("antigravity-cli", "empty_or_unreadable")
        return None
    try:
        head = _read_head(jsonl_file, stop_when=_agent_detected)
    except OSError:
        record("antigravity-cli", "empty_or_unreadable")
        return None

    agent = detect_agent(head)
    if agent is None:
        logger.warning(
            "agent detection failed for %s (%d bytes); session skipped",
            jsonl_file,
            size,
        )
        record("antigravity-cli", "detection_failed")
        return None
    if agent != AgentType.ANTIGRAVITY_CLI:
        record("antigravity-cli", "different_agent")
        return None

    # brain/<uuid>/.system_generated/logs/transcript_full.jsonl
    native_id = jsonl_file.parents[2].name
    session_id = f"local:{agent}:{native_id}"
    mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=UTC)
    summary = summaries.get(native_id)
    cwd = summary.cwd if summary else None
    parent = summary.parent_id if summary else None

    record("antigravity-cli", "indexed")
    return session_id, {
        "path": jsonl_file,
        "agent": agent,
        "mtime": mtime,
        "project_name": _project_from_cwd(cwd) if cwd else None,
        "cwd": cwd,
        "home": home,
        "parent_session_id": f"local:{agent}:{parent}" if parent else None,
    }


__all__ = ["TRANSCRIPT_GLOB", "index_antigravity_home"]
