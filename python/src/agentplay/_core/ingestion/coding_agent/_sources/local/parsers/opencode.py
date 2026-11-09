"""Parse an opencode session (materialized NDJSON) into the universal SpanData shape.

opencode (sst/opencode) has no per-session file - it stores sessions in a SQLite db. Discovery
materializes each session into NDJSON, one record per message:

    {"id": <msgId>, "sessionID": <sid>, "message": {...}, "parts": [{...}, ...]}

``message`` carries ``role`` and, on assistants, ``modelID`` / ``tokens`` / ``path.cwd``. ``parts``
are the content units (``type``: ``text`` | ``reasoning`` | ``tool`` | ``step-start`` |
``step-finish`` | ...). We bracket user→assistant into turns and emit a root Session span plus
per-turn Turn spans plus per-tool tool spans — the same shape the Claude Code / Codex parsers
emit, so downstream code never branches on agent.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._paths import basename as path_basename
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    SOURCE_KIND_SQLITE,
    as_count,
    as_dict,
    drop_untyped,
    parse_jsonl_records,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import _apply_patch_input
from agentplay._core.ingestion.coding_agent.produced import (
    creation_command_attributes,
    creation_output_attributes,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("opencode")

# opencode's patch tools: `apply_patch` (current) and `patch` (older builds). Both
# take the body as `{"patchText": "*** Begin Patch ..."}`.
_PATCH_TOOLS = frozenset({"apply_patch", "patch"})


def _source_attrs(record: dict | None) -> dict:
    """``source_attrs(record, kind="sqlite")`` minus the offset.

    opencode sessions are re-materialized from SQLite into NDJSON fresh on
    every parse (see the module docstring) — the NDJSON byte offset
    ``parse_jsonl_records`` stamps on a record is a position in that
    throwaway intermediate, not a stable transcript location, and would
    drift across re-parses if kept. ``kind="sqlite"`` (content hash
    only) matches ``cursor_store.py``'s own rewritten-store anchor.
    """
    # the message id IS the addressable key in the re-materialized
    # store — the row cursor a seek can find again, unlike the throwaway
    # NDJSON offset. It is part of the record already, so the hash is unchanged.
    msg_id = record.get("id") if isinstance(record, dict) else None
    return {
        k: v
        for k, v in source_attrs(
            record,
            kind=SOURCE_KIND_SQLITE,
            row_cursor=msg_id if isinstance(msg_id, str) and msg_id else None,
        ).items()
        if k != "source.byte_offset"
    }


def parse_opencode(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a materialized opencode NDJSON transcript into a span tree."""
    records = _parse_jsonl(data)
    if not records:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    turns = _group_into_turns(records)

    # Session-level cwd/model: first assistant path.cwd / modelID we see.
    # opencode's native session id rides on every record as `sessionID`
    # (capital ID); no version field exists anywhere in the format.
    cwd = ""
    model_name = ""
    agent_session_id = ""
    for rec in records:
        msg = rec.get("message") or {}
        if not cwd:
            cwd = (msg.get("path") or {}).get("cwd") or ""
        if not model_name and msg.get("modelID"):
            model_name = msg["modelID"]
        if not agent_session_id and rec.get("sessionID"):
            agent_session_id = rec["sessionID"]
        if cwd and model_name and agent_session_id:
            break

    if not project_name and cwd:
        project_name = path_basename(cwd)

    all_ts = [ts for rec in records if (ts := _created(rec.get("message") or {}))]
    if all_ts:
        session_start, session_end = min(all_ts), max(all_ts)
    else:
        session_start = session_end = datetime.now(UTC)

    all_spans: list[SpanData] = []
    root_id = f"{trace_id}:root"

    for turn_num, turn in enumerate(turns, 1):
        turn_id = f"{trace_id}:turn:{turn_num}"
        turn_start = turn.get("start") or session_start
        turn_end = turn.get("end") or turn_start

        turn_span = SpanData(
            span_id=turn_id,
            trace_id=trace_id,
            parent_id=root_id,
            name=f"Turn {turn_num}",
            span_kind="CHAIN",
            status_code="OK",
            start_time=turn_start,
            end_time=turn_end,
            attributes={
                "input.value": turn.get("user_query", "")[:2000],
                "output.value": turn.get("assistant_text", "")[:2000],
                "cwd": turn.get("cwd") or cwd or "",
                # opencode reports fresh input, output, reasoning, and cache read/write
                # separately (input + output + reasoning + cache == total). Normalize to the
                # same attribute shape the Claude/Codex parsers use.
                "llm.token_count.prompt": turn.get("prompt", 0),
                "llm.token_count.completion": turn.get("completion", 0),
                "llm.token_count.input_fresh": turn.get("input_fresh", 0),
                "llm.token_count.cache_read": turn.get("cache_read", 0),
                "llm.token_count.cache_write": turn.get("cache_write", 0),
                "llm.model_name": turn.get("model") or model_name,
            },
        )
        turn_span.attributes.update(_source_attrs(turn.get("_src")))
        all_spans.append(turn_span)

        for tool in turn.get("tools", []):
            all_spans.append(
                SpanData(
                    span_id=f"{trace_id}:tool:{tool['call_id']}",
                    trace_id=trace_id,
                    parent_id=turn_id,
                    name=tool["name"],
                    span_kind="CHAIN",
                    status_code=(
                        "ERROR"
                        if tool.get("is_error")
                        else "OK"
                        if tool.get("completed")
                        else "UNSET"
                    ),
                    start_time=tool.get("start") or turn_start,
                    end_time=tool.get("end") or turn_end,
                    attributes={
                        "tool.name": tool["name"],
                        "code.edit_evidence": tool["edit_evidence"],
                        "input.value": tool.get("input", "")[:2000],
                        **{
                            key: tool[key]
                            for key in (
                                "pr.create.provider",
                                "pr.create.capture",
                                "pr.create.repository",
                                "pr.create.requested_repository",
                                "pr.create.command_sha256",
                                "pr.create.output_url",
                            )
                            if key in tool
                        },
                        "output.value": tool.get("output", "")[:2000],
                        **_source_attrs(tool.get("_src")),
                    },
                )
            )

    # Session model = last model actually used (handles mid-session switches).
    session_model = next((t["model"] for t in reversed(turns) if t.get("model")), model_name)

    root_span = SpanData(
        span_id=root_id,
        trace_id=trace_id,
        parent_id=None,
        name="Session",
        span_kind="CHAIN",
        status_code="OK",
        start_time=session_start,
        end_time=session_end,
        attributes={
            "user.id": user_id or "",
            "llm.model_name": session_model,
            "session.id": trace_id,
            "project.name": project_name or "",
            "harness.agent_session_id": agent_session_id,
        },
    )
    all_spans.insert(0, root_span)

    trace = TraceData(
        trace_id=trace_id, start_time=session_start, end_time=session_end, spans=all_spans
    )
    return trace, all_spans


def _parse_jsonl(data: bytes) -> list[dict]:
    """every record carries the shared source anchors (offset + hash).

    A retyped field reads as absent (see :func:`drop_untyped`) instead of raising
    later and losing the whole session.
    """
    records = parse_jsonl_records(data)
    for rec in records:
        drop_untyped(rec, ("message",), dict)
        drop_untyped(rec, ("parts",), list)
        msg = rec.get("message")
        if msg is not None:
            drop_untyped(msg, ("time", "path", "tokens", "model"), dict)
            drop_untyped(msg, ("modelID",), str)
            drop_untyped(as_dict(msg.get("path")), ("cwd",), str)
            drop_untyped(as_dict(msg.get("tokens")), ("cache",), dict)
        for part in rec.get("parts", ()):
            if isinstance(part, dict):
                drop_untyped(part, ("state", "tokens"), dict)
                drop_untyped(part, ("text",), str)
                state = part.get("state")
                if state is not None:
                    drop_untyped(state, ("time",), dict)
                drop_untyped(as_dict(part.get("tokens")), ("cache",), dict)
    return records


def _created(message: dict) -> datetime | None:
    return _epoch_ms((message.get("time") or {}).get("created"))


def _epoch_ms(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _text_of(parts: list, kinds: tuple[str, ...]) -> str:
    return "\n".join(
        p["text"] for p in parts if isinstance(p, dict) and p.get("type") in kinds and p.get("text")
    )


def _new_turn(msg: dict, parts: list, created: datetime | None, src: dict | None = None) -> dict:
    model = msg.get("modelID")
    if not model and isinstance(msg.get("model"), dict):
        model = msg["model"].get("modelID")
    return {
        "_src": src,  # the record that opened the turn
        "user_query": _text_of(parts, ("text",)),
        "assistant_text": "",
        "start": created,
        "end": created,
        "prompt": 0,
        "completion": 0,
        "input_fresh": 0,
        "cache_read": 0,
        "cache_write": 0,
        "tools": [],
        "model": model,
        "cwd": (msg.get("path") or {}).get("cwd") or "",
    }


def _group_into_turns(records: list[dict]) -> list[dict]:
    """Bracket messages into turns: a user message opens a turn; assistant messages fold in."""
    turns: list[dict] = []
    current: dict | None = None
    # Dedup so a vendor rename doesn't flood the log: one warning per distinct
    # (session, bad value) pair, not one per record.
    seen_unknown_roles: set[tuple[str, str]] = set()
    seen_finish_shapes: set[tuple[str, str]] = set()

    for rec in records:
        msg = rec.get("message") or {}
        role = msg.get("role")
        parts = rec.get("parts") or []
        created = _created(msg)
        session_id = rec.get("sessionID") or "<unknown>"

        if role == "user":
            if current:
                turns.append(current)
            current = _new_turn(msg, parts, created, rec)
        elif role == "assistant":
            if current is None:
                current = _new_turn({}, [], created, rec)
            text = _text_of(parts, ("text",))
            if text:
                current["assistant_text"] = (
                    f"{current['assistant_text']}\n{text}" if current["assistant_text"] else text
                )
            if msg.get("modelID"):
                current["model"] = msg["modelID"]
            if not current.get("cwd"):
                current["cwd"] = (msg.get("path") or {}).get("cwd") or ""
            _fold_tokens(
                current,
                msg.get("tokens") or _step_finish_tokens(parts, session_id, seen_finish_shapes),
            )
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "tool":
                    tool = _tool_from_part(part, created)
                    tool["_src"] = rec  # the record that carried the call
                    current["tools"].append(tool)
            if created:
                current["end"] = created
        else:
            # A role opencode never documented (user/assistant only) drops the
            # whole record, tokens included, with nothing else to show for it.
            # str() collapses values a reader would not: 1 and "1" stringify
            # alike, so the second malformed role would drop with no warning.
            key = (session_id, type(role).__name__, repr(role))
            if key not in seen_unknown_roles:
                seen_unknown_roles.add(key)
                logger.debug(
                    "session %s: opencode message has unrecognised role %r; record dropped",
                    session_id,
                    role,
                )

    if current:
        turns.append(current)
    return turns


def _step_finish_tokens(
    parts: list, session_id: str = "<unknown>", seen: set[tuple[str, str]] | None = None
) -> dict | None:
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "step-finish" and part.get("tokens"):
            return part["tokens"]
    # The `step-finish` part is opencode's ONLY source of per-turn tokens when
    # the assistant message itself carries no `tokens` field. If the vendor
    # renames the part's `type` (or restructures it) while still shipping a
    # `tokens` payload under some other type, this loop would otherwise
    # silently zero the turn's tokens with no trace at all.
    for part in parts:
        if isinstance(part, dict) and part.get("tokens") and part.get("type") != "step-finish":
            ptype = part.get("type")
            # Same collision as the unknown-role key above: type plus repr, so
            # 1 and "1" are each named once.
            key = (session_id, type(ptype).__name__, repr(ptype))
            if seen is not None and key not in seen:
                seen.add(key)
                logger.debug(
                    "session %s: opencode part type %r carries tokens but is not "
                    "'step-finish'; turn tokens from this part are dropped",
                    session_id,
                    ptype,
                )
    return None


def _fold_tokens(turn: dict, tokens: dict | None) -> None:
    if not tokens:
        return
    cache = as_dict(tokens.get("cache"))
    fresh = as_count(tokens.get("input"))
    cache_read = as_count(cache.get("read"))
    cache_write = as_count(cache.get("write"))
    turn["input_fresh"] += fresh
    turn["cache_read"] += cache_read
    turn["cache_write"] += cache_write
    turn["prompt"] += fresh + cache_read + cache_write
    turn["completion"] += as_count(tokens.get("output")) + as_count(tokens.get("reasoning"))


def _tool_from_part(part: dict, created: datetime | None) -> dict:
    """One `tool`-type part -> the parser's intermediate tool dict.

    Tool names are canonicalized to the Claude-cased vocabulary via
    `_TOOL_ALIASES` here — the span builder stamps `name` on both the span
    name and `tool.name`, matching cursor_store.py (which also doesn't keep
    the raw name). Dict args are normalized (see `_normalize_file_path_arg`)
    so the shared `file_paths.py` extractors - which only ever match
    a snake_case `"file_path"` key, on purpose — find opencode's camelCase
    `filePath` too. Both quirks stay confined to this parser instead of
    widening the shared modules.
    """
    state = part.get("state") or {}
    raw_name = part.get("tool") or part.get("name") or "tool"
    if not isinstance(raw_name, str):
        raw_name = "unknown"
    name = _TOOL_ALIASES.get(raw_name, raw_name)
    tool_input = state.get("input", part.get("input"))
    if isinstance(tool_input, dict) and raw_name in _PATCH_TOOLS:
        tool_input = _patch_input(tool_input)
    if isinstance(tool_input, dict):
        tool_input = _normalize_file_path_arg(tool_input)
    if isinstance(tool_input, dict | list):
        tool_input = json.dumps(tool_input)
    command_attrs = creation_command_attributes(tool_input, name)
    return {
        "call_id": part.get("callID") or part.get("id") or name,
        "name": name,
        "input": str(tool_input or "")[:2000],
        **command_attrs,
        **creation_output_attributes(
            (state.get("output") or "") if state.get("status") == "completed" else "",
            command_attrs.get("pr.create.provider"),
            command_attrs.get("pr.create.repository"),
        ),
        "edit_evidence": extract_edit_evidence(
            "apply_patch" if raw_name in _PATCH_TOOLS else name,
            tool_input,
            successful=state.get("status") == "completed",
        ),
        "output": str(state.get("output") or "")[:2000],
        "is_error": state.get("status") == "error",
        "completed": state.get("status") == "completed",
        "start": _epoch_ms((state.get("time") or {}).get("start")) or created,
        "end": _epoch_ms((state.get("time") or {}).get("end")) or created,
    }


def _patch_input(args: dict) -> dict | str:
    """Rewrite a patch tool's `{"patchText": ...}` args the way Codex's apply_patch is.

    The shared consumers read a patch body only under `patch`/`input`, and the
    files-touched extractors only match `"file_path"` keys, so a `patchText` body
    gave no edit evidence, no files and no line counts. `_apply_patch_input`
    puts the line counts and one `"file_path"` per patched file ahead of the body,
    so they survive the 2,000-character `input.value` cut. Args without a string
    `patchText` are returned unchanged.
    """
    patch_text = args.get("patchText")
    if not isinstance(patch_text, str) or not patch_text:
        return args
    return _apply_patch_input(patch_text)


def _normalize_file_path_arg(args: dict) -> dict:
    """Alias opencode's camelCase `filePath` arg key to `file_path`.

    opencode's read/edit/write args key
    the touched file as `"filePath"`, but the shared files-touched
    extractors (file_paths.py) match only the snake_case
    `"file_path"`/`"notebook_path"` keys — deliberately narrow so every
    parser translates its own quirk at the boundary (same contract
    `cursor_store._normalize_file_path_arg` honors for Cursor's
    `path`/`paths`).

    Only fills `file_path` when it's absent, and never mutates/removes the
    original key — purely additive. Unlike the Cursor version, the added
    key goes FIRST, not last: opencode edit args carry large
    `oldString`/`newString` blobs and `input.value` truncates at 2000
    chars, so a trailing `file_path` would routinely be cut off. `grep`/
    `glob`'s `"path"` arg names a search DIRECTORY here, not a file, so —
    unlike Cursor, where `path` is the touched file — it is deliberately
    left unmapped rather than fabricating a `file_path` from it.

    Args:
        args: The tool part's raw input dict (already confirmed a dict by
            the caller).

    Returns:
        `args` unchanged, or a shallow copy with `file_path` prepended.
    """
    if "file_path" in args:
        return args
    file_path = args.get("filePath")
    if isinstance(file_path, str) and file_path:
        return {"file_path": file_path, **args}
    return args


__all__ = ["parse_opencode"]
