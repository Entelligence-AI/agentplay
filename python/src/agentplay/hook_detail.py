"""Hook failure details, read on demand and never persisted.

A map records that a hook failed (which hook, whether it blocked, the tool call
that triggered it) but never what it said or ran: both are free text. To show
them the local server opens the original transcript when the viewer asks and
returns the message and command of that one record. Nothing here writes to a
map, the index, an export or disk: the scan below caches *line numbers* (never
text), and a read re-parses that single line.

A record is addressed by the opaque ``srcUuid`` the builder stored on the error
event; the session's subagent transcripts are searched too, as the builder reads them. Callers (the server) decide who may ask. This module trusts the path it
is given.
"""

from __future__ import annotations

import functools
import itertools
import json
import os
from pathlib import Path
from typing import Any

from .prompt_images import Unavailable

MAX_CHARS = 4000
"""Longest message or command returned, in characters."""

_HOOK_ERRORS = ("hook_blocking_error", "hook_non_blocking_error")

def _not_found() -> Unavailable:
    return Unavailable(404, "not_found", "We couldn't find that hook failure.")

def _gone() -> Unavailable:
    return Unavailable(410, "gone", "The transcript for this session is no longer on this machine.")

def _stat(path: str) -> os.stat_result:
    try:
        return os.stat(path)
    except FileNotFoundError:
        raise _gone() from None

def _hook_attachment(rec: Any) -> dict[str, Any] | None:
    att = rec.get("attachment") if isinstance(rec, dict) else None
    if isinstance(att, dict) and att.get("type") in _HOOK_ERRORS:
        return att
    return None

@functools.lru_cache(maxsize=64)
def _scan(path: str, mtime_ns: int, size: int) -> dict[str, int]:
    """Line number of every hook error record, by uuid. Holds no text.

    ``mtime_ns`` and ``size`` are only the cache key: a transcript that grows
    gets a fresh scan.
    """
    lines: dict[str, int] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            if "hook_" not in line:  # cheap filter before parsing
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if _hook_attachment(rec) is not None and isinstance(rec.get("uuid"), str):
                lines.setdefault(rec["uuid"], n)
    return lines

def _text(value: Any) -> str | None:
    return value[:MAX_CHARS] if isinstance(value, str) and value else None

def _transcripts(path: str) -> list[str]:
    """The session transcript, then its subagents' (``<session>/subagents/**/agent-*.jsonl``)."""
    from ._core.ingestion.coding_agent._sources.local.source import claude_code_subagent_files

    # Not strict: one unreadable folder must not hide hooks in the readable ones.
    return [path, *(str(f) for f in claude_code_subagent_files(Path(path), strict=False))]

def _load(path: str, line_no: int, uuid: str) -> dict[str, Any] | None:
    """The hook attachment on one line, or None if the file changed under us."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            rec = json.loads(next(itertools.islice(fh, line_no, None), ""))
    except (OSError, ValueError):
        return None
    # The file may have changed since the scan; trust nothing the cached line says.
    return _hook_attachment(rec) if isinstance(rec, dict) and rec.get("uuid") == uuid else None

def read_hook_detail(path: str, uuid: str) -> tuple[str | None, str | None]:
    """The message and command of one hook failure in the transcript.

    Args:
        path: the transcript ``.jsonl``.
        uuid: the ``srcUuid`` of the map's error event.

    Returns:
        ``(message, command)``, each capped at ``MAX_CHARS`` and ``None`` when the
        record has none. A blocking hook's message is its ``blockingError``; a
        non-blocking one's is its stderr, or stdout when stderr is empty.

    Raises:
        Unavailable: 410 if the transcript is gone; 404 if no hook error record
            has that uuid.
    """
    _stat(path)
    for f in _transcripts(path):
        try:
            st = os.stat(f)
            line_no = _scan(f, st.st_mtime_ns, st.st_size).get(uuid)
        except OSError:
            continue
        att = None if line_no is None else _load(f, line_no, uuid)
        if att is not None:
            be = att.get("blockingError")
            if isinstance(be, dict):
                return _text(be.get("blockingError")), _text(be.get("command"))
            return _text(att.get("stderr")) or _text(att.get("stdout")), _text(att.get("command"))
    raise _not_found()
