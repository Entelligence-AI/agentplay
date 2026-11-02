"""Parse Cline session JSON into the universal SpanData shape.

Cline (CLI and VS Code extension) keeps each session in
``~/.cline/data/sessions/<session-id>/``:

    <session-id>.json            session metadata (cwd, model, status, git)
    <session-id>.messages.json   the conversation (parsed here)
    agent_<id>.messages.json     spawned-subagent conversations (not read here)

The messages file is one JSON document, read from real Cline sessions:

    {"version": 1, "sessionId": ..., "messages": [...], "system_prompt": ...}

Each message is Anthropic-shaped:

    {"id": ..., "role": "user" | "assistant", "ts": <epoch ms>,
     "content": [<text | thinking | tool_use | tool_result | image blocks>],
     "modelInfo": {"id": ..., "provider": ...},          # assistant only
     "metrics": {"inputTokens", "outputTokens",
                 "cacheReadTokens", "cacheWriteTokens"}}  # assistant only

# Turn boundaries

A `user` message carrying text opens a turn. A `user` message that only carries
`tool_result` blocks continues the running turn; each result is paired with its
`tool_use` by id.

# Tool failures

A `tool_result` fails when it sets `is_error`, or when any item of its content
list reports `"success": false` (a shell command's `Command exited with code
<n>`, a missing file, a timed-out fetch).

# Tokens

`inputTokens` includes `cacheReadTokens`: summed over a session the metrics
equal the session metadata's `usage`, and the cached count is never larger.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    SOURCE_KIND_JSON_DOC,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData, model_id_or_none
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# input.value / output.value truncation, matching every other local parser.
_TRUNCATE = 2000

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("cline")

# Cline's argument names -> the names the edit and path readers expect.
_ARG_RENAMES = {"path": "file_path", "old_text": "old_string", "new_text": "new_string"}

# Path arguments, `file_path` first, so a large write keeps its path inside
# `input.value`'s 2,000 characters.
_PATH_KEYS = ("file_path", "path", "paths")


def parse_cline(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse one Cline ``<session-id>.messages.json`` into a span tree.

    Args:
        data: raw bytes of the messages file.
        trace_id: the source-prefixed session id (e.g. `local:cline:<id>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by `LocalSource` from the
            session metadata's `cwd`.

    Returns:
        `(trace, all_spans)`; empty spans when the file holds no messages.
    """
    doc = _load(data)
    raw_messages = doc.get("messages")
    messages = [
        m
        for m in (raw_messages if isinstance(raw_messages, list) else [])
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]
    if not messages:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    results = _tool_results(messages)
    times = [t for t in (_dt(m.get("ts")) for m in messages) if t is not None]
    session_start = min(times) if times else datetime.now(UTC)
    session_end = max(times) if times else session_start

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []
    turn: dict | None = None
    turn_num = 0
    session_model = ""

    def _open_turn(msg: dict, prompt: str) -> dict:
        nonlocal turn_num
        turn_num += 1
        ts = _dt(msg.get("ts")) or session_start
        return {
            "span_id": f"{trace_id}:turn:{turn_num}",
            "num": turn_num,
            "_src": msg,
            "prompt": prompt,
            "start": ts,
            "end": ts,
            "output": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cost_events": [],
            "model": "",
            "tools": [],
        }

    def _close_turn() -> None:
        if turn is not None:
            all_spans.append(_turn_span(trace_id, root_id, turn))
            all_spans.extend(turn["tools"])

    for msg in messages:
        if msg["role"] == "user":
            prompt = _user_prompt(msg.get("content"))
            if prompt is not None:
                _close_turn()
                turn = _open_turn(msg, prompt)
        else:
            if turn is None:
                # Model activity before any prompt still needs a turn to live
                # under (same fallback as command_code.py / gemini.py).
                turn = _open_turn(msg, "")
            model = model_id_or_none((msg.get("modelInfo") or {}).get("id")) or ""
            if model:
                turn["model"] = model
                session_model = model
            for block in _blocks(msg.get("content")):
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str) and block["text"]:
                    turn["output"].append(block["text"])
                elif kind == "tool_use":
                    turn["tools"].append(_tool_span(trace_id, turn, msg, block, len(turn["tools"]), results))

        ts = _dt(msg.get("ts"))
        if turn is not None and ts is not None and ts > turn["end"]:
            turn["end"] = ts
    _close_turn()

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
            "harness.agent_session_id": doc.get("sessionId") if isinstance(doc.get("sessionId"), str) else "",
            "harness.agent_version": "",
        },
    )
    all_spans.insert(0, root_span)
    trace = TraceData(trace_id=trace_id, start_time=session_start, end_time=session_end, spans=all_spans)
    return trace, all_spans


def _load(data: bytes) -> dict:
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _dt(ts: object) -> datetime | None:
    """A message's epoch-millisecond `ts` as an aware datetime."""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _blocks(content: object) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _tool_results(messages: list[dict]) -> dict[str, tuple[dict, dict]]:
    """Each `tool_result` block with the message that carried it, by `tool_use_id`."""
    out: dict[str, tuple[dict, dict]] = {}
    for msg in messages:
        if msg["role"] != "user":
            continue
        for block in _blocks(msg.get("content")):
            use_id = block.get("tool_use_id")
            if block.get("type") == "tool_result" and isinstance(use_id, str) and use_id:
                out[use_id] = (block, msg)
    return out


def _user_prompt(content: object) -> str | None:
    """The prompt text of a `user` message, or None when it carries no text."""
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [b["text"] for b in _blocks(content) if b.get("type") == "text" and isinstance(b.get("text"), str)]
    texts = [t for t in parts if t.strip()]
    return "\n".join(texts) if texts else None


def _src(msg: dict) -> dict:
    return source_attrs(msg, kind=SOURCE_KIND_JSON_DOC, row_cursor=str(msg.get("id") or ""))


def _turn_span(trace_id: str, root_id: str, turn: dict) -> SpanData:
    prompt = turn["prompt_tokens"]
    cache_read = turn["cache_read"]
    span = SpanData(
        span_id=turn["span_id"],
        trace_id=trace_id,
        parent_id=root_id,
        name=f"Turn {turn['num']}",
        span_kind="CHAIN",
        status_code="OK",
        start_time=turn["start"],
        end_time=turn["end"],
        attributes={
            "input.value": turn["prompt"][:_TRUNCATE],
            "output.value": "\n".join(turn["output"])[:_TRUNCATE],
            # The messages file records no cwd; `LocalSource` fills it from the
            # session metadata.
            "cwd": "",
            "llm.token_count.prompt": prompt,
            "llm.token_count.completion": turn["completion_tokens"],
            "llm.token_count.cache_read": cache_read,
            "llm.token_count.cache_write": turn["cache_write"],
            "llm.token_count.input_fresh": max(0, prompt - cache_read - turn["cache_write"]),
            "llm.cost_events": turn["cost_events"],
            "llm.model_name": turn["model"],
        },
    )
    span.attributes.update(_src(turn["_src"]))
    return span


def _tool_span(
    trace_id: str,
    turn: dict,
    msg: dict,
    block: dict,
    index: int,
    results: dict[str, tuple[dict, dict]],
) -> SpanData:
    raw_name = block.get("name")
    raw_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    tool_name, args = _normalise(raw_name, args)
    input_value = json.dumps(args)

    call_id = block.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"{msg.get('id')}:{index}"
    result, result_msg = results.get(call_id, (None, None))
    output_value = _result_text(result) if result is not None else ""

    if result is None:
        status_code = "UNSET"
    else:
        status_code = "OK"

    start = _dt(msg.get("ts")) or turn["start"]
    end = (_dt(result_msg.get("ts")) if result_msg else None) or start
    if end < start:
        end = start
    if end > turn["end"]:
        turn["end"] = end

    span = SpanData(
        span_id=f"{trace_id}:tool:{call_id}",
        trace_id=trace_id,
        parent_id=turn["span_id"],
        name=tool_name,
        span_kind="CHAIN",
        status_code=status_code,
        start_time=start,
        end_time=end,
        attributes={
            "tool.name": tool_name,
            "cwd": "",
            "input.value": input_value[:_TRUNCATE],
            "output.value": output_value[:_TRUNCATE],
            "code.edit_evidence": extract_edit_evidence(tool_name, input_value, successful=status_code == "OK"),
        },
    )
    span.attributes.update(_src(msg))
    return span


def _normalise(raw_name: str, args: dict) -> tuple[str, dict]:
    """The canonical tool name and `args` renamed to the names readers expect.

    Shell commands become one `command` string; `path` becomes `file_path`
    (first path of a `files` list for reads); an `editor` call with no
    `old_text` and no `insert_line` creates a file, so it becomes a `Write`.
    """
    name = _TOOL_ALIASES.get(raw_name, raw_name)
    if name == "Bash":
        skip = ("command", "commands", "cmd")
        return name, {"command": _shell_command(args), **{k: v for k, v in args.items() if k not in skip}}
    if raw_name in ("editor", "edit") and not args.get("old_text") and "insert_line" not in args:
        name = "Write"
        args = {k: v for k, v in args.items() if k not in ("old_text", "new_text")} | {
            "content": args.get("new_text", "")
        }
    out = {_ARG_RENAMES.get(k, k): v for k, v in args.items()}
    if name == "Read" and "file_path" not in out:
        files = out.get("files")
        first = files[0] if isinstance(files, list) and files else None
        path = first.get("path") if isinstance(first, dict) else first
        if isinstance(path, str):
            out["file_path"] = path
    first_keys = {key: out[key] for key in _PATH_KEYS if key in out}
    return name, {**first_keys, **{k: v for k, v in out.items() if k not in first_keys}}


def _shell_command(args: dict) -> str:
    """One command string from `command`, `cmd`, or a `commands` string / list."""
    single = args.get("command") or args.get("cmd")
    if isinstance(single, str):
        return single
    many = args.get("commands")
    if isinstance(many, (str, dict)):
        many = [many]
    if not isinstance(many, list):
        return ""
    items = [c if isinstance(c, str) else c.get("command") or c.get("cmd") for c in many if isinstance(c, (str, dict))]
    return "\n".join(c for c in items if isinstance(c, str))


def _result_text(result: dict) -> str:
    """The text of a `tool_result`: a string, or the `result` / `error` of each item."""
    content = result.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for item in content:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict):
            texts.extend(item[k] for k in ("result", "error") if isinstance(item.get(k), str))
    return "\n".join(texts)


__all__ = ["parse_cline"]
