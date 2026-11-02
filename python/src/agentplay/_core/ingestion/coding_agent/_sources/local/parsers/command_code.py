"""Parse Command Code session JSONL into the universal SpanData shape.

Command Code writes one file per session at
``~/.commandcode/projects/<project-slug>/<session-id>.jsonl``. The shape below
was read from real Command Code sessions (format `version` 3):

    {"type": "session", "id": <session id>, "cwd": <path>, "version": 3,
     "timestamp": <ISO>}
    {"type": "message", "id": ..., "parentId": ..., "timestamp": <ISO>,
     "message": {"role": "user", "content": [{"type": "text", ...}],
                 "meta": {"source": "user", "messageId": ...}}}
    {"type": "message", ..., "model": ..., "usage": {"inputTokens",
     "outputTokens", "cacheReadTokens", "cacheWriteTokens", "costUsd"},
     "message": {"role": "assistant", "content": [<text | thinking |
                 tool_use blocks>], "meta": {"source": "model", ...}}}
    {"type": "message", ..., "message": {"role": "user", "content":
     [{"type": "tool_result", "tool_use_id": ..., "content": [...]}],
     "meta": {"source": "tool", ...}}}

The sibling `<session-id>.checkpoints.jsonl` and `<session-id>.meta.json` files
hold file checkpoints and a title; nothing here reads them.

# Turn boundaries

A `user` message carrying text opens a turn when its `meta.source` is `user`
or `followup`. A `steering` message is typed while the agent is working and
joins the running turn. A message the CLI marks `meta.injected` is not a
prompt and opens no turn. `user` messages with `meta.source` `tool` only carry
`tool_result` blocks, which are paired with their `tool_use` by id.

# Tool failures

A `tool_result` carries no error flag; the CLI reports failure in the result
text. A shell call that exits non-zero starts its result with
`Exit code: <n>`, and `shell_output` (polling a background shell) reports a
finished command's code on an `Exit code: <n>` line. Other tools refuse with a
fixed sentence at the start of the result (see `_RE_REFUSAL`).

# Tokens

`inputTokens` includes `cacheReadTokens` (the cached count is never larger in
real sessions). The CLI records no reasoning count apart from `outputTokens`.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    as_count,
    parse_jsonl_records,
    safe_parse_dt,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData, model_id_or_none
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# input.value / output.value truncation, matching every other local parser.
_TRUNCATE = 2000

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("command-code")

# `meta.source` values of a user message that opens a turn.
_PROMPT_SOURCES = ("user", "followup")

_SHELL_TOOLS = ("shell_command", "powershell")

_SHELL_OUTPUT_TOOL = "shell_output"

# Path arguments, `file_path` first, so a large `write_file` keeps its path
# inside `input.value`'s 2,000 characters. Hoisting them does not change the
# argument values `extract_edit_evidence` reads.
_PATH_KEYS = ("file_path", "path", "paths")

# The CLI's refusals, as the first words of a result. Each was seen in a real
# session.
_RE_REFUSAL = re.compile(
    "|".join(
        (
            r"File has been modified since it was last read",
            r"File has not been read yet",
            r"Only part of this file has been read",
            r"Path must be absolute",
            r"No running shell task found",
            r"You are not in plan mode",
            r"Plan not approved",
            r"User declined",
            r"Sleep interrupted by the user",
            r"\[sub-agent (?:interrupted|stopped early)",
            r"Could not read the image",
            r"image_index \d+ is out of range",
            r"\d{3} upstream error",
        )
    )
)

_RE_EXIT_START = re.compile(r"Exit code: (-?\d+)")
_RE_EXIT_LINE = re.compile(r"^Exit code: (-?\d+)\s*$", re.MULTILINE)


def parse_command_code(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a Command Code session JSONL file into a span tree.

    Args:
        data: raw JSONL bytes of one `<session-id>.jsonl` file.
        trace_id: the source-prefixed session id (e.g. `local:command-code:<id>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by `LocalSource` from the
            header's `cwd`.

    Returns:
        `(trace, all_spans)`; empty spans when the file holds no messages.
    """
    records = parse_jsonl_records(data)
    header: dict = next(
        (r for r in records if r.get("type") == "session" and isinstance(r.get("id"), str)), {}
    )
    messages = _messages(records)
    if not messages:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    cwd = header.get("cwd") if isinstance(header.get("cwd"), str) else ""
    results = _tool_results(messages)

    times = [
        t
        for t in (
            safe_parse_dt(header.get("timestamp")),
            *(safe_parse_dt(m.get("timestamp")) for m in messages),
        )
        if t is not None
    ]
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
        ts = safe_parse_dt(msg.get("timestamp")) or session_start
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
            all_spans.append(_turn_span(trace_id, root_id, turn, cwd))
            all_spans.extend(turn["tools"])

    for msg in messages:
        body = msg["message"]
        role = body.get("role")
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        ts = safe_parse_dt(msg.get("timestamp"))

        if role == "user":
            prompt = _user_prompt(body.get("content"))
            if prompt is not None and not meta.get("injected"):
                if meta.get("source") in _PROMPT_SOURCES or turn is None:
                    _close_turn()
                    turn = _open_turn(msg, prompt)
                    continue
        elif role == "assistant":
            if turn is None:
                # Model activity before any prompt still needs a turn to live
                # under (same fallback as gemini.py / kiro.py).
                turn = _open_turn(msg, "")
            model = model_id_or_none(msg.get("model")) or ""
            if model:
                turn["model"] = model
                session_model = model
            for block in _blocks(body.get("content")):
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str) and block["text"]:
                    turn["output"].append(block["text"])
                elif kind == "tool_use":
                    index = len(turn["tools"])
                    turn["tools"].append(
                        _tool_span(trace_id, turn, msg, block, index, results, cwd)
                    )
            _add_usage(turn, msg, model)

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
            "harness.agent_session_id": header.get("id") or "",
            # The header's `version` is the file format, not the CLI version.
            "harness.agent_version": "",
        },
    )
    all_spans.insert(0, root_span)
    trace = TraceData(
        trace_id=trace_id,
        start_time=session_start,
        end_time=session_end,
        spans=all_spans,
    )
    return trace, all_spans


def _messages(records: list[dict]) -> list[dict]:
    """Conversation messages in order, keeping the last copy of each `id`."""
    latest: dict[str, dict] = {}
    for record in records:
        msg_id = record.get("id")
        if (
            isinstance(msg_id, str)
            and msg_id
            and record.get("type") == "message"
            and isinstance(record.get("message"), dict)
        ):
            # dict keeps first-insertion order; reassigning keeps the position.
            latest[msg_id] = record
    return list(latest.values())


def _blocks(content: object) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _tool_results(messages: list[dict]) -> dict[str, tuple[dict, dict]]:
    """Each `tool_result` block with the message that carried it, by `tool_use_id`."""
    out: dict[str, tuple[dict, dict]] = {}
    for msg in messages:
        if msg["message"].get("role") != "user":
            continue
        for block in _blocks(msg["message"].get("content")):
            use_id = block.get("tool_use_id")
            if block.get("type") == "tool_result" and isinstance(use_id, str) and use_id:
                out[use_id] = (block, msg)
    return out


def _user_prompt(content: object) -> str | None:
    """The prompt text of a `user` message, or None when it carries no text."""
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [
            b["text"]
            for b in _blocks(content)
            if b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
    texts = [t for t in parts if t.strip()]
    if not texts:
        return None
    return "\n".join(texts)


def _add_usage(turn: dict, msg: dict, model: str) -> None:
    """Fold one assistant message's `usage` record into its turn."""
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return
    prompt = as_count(usage.get("inputTokens"))
    cache_read = min(as_count(usage.get("cacheReadTokens")), prompt)
    # Taken to be inside `inputTokens` like `cacheReadTokens`; it was 0 in
    # every captured session.
    cache_write = min(as_count(usage.get("cacheWriteTokens")), prompt - cache_read)
    completion = as_count(usage.get("outputTokens"))
    if not prompt and not completion:
        return
    turn["prompt_tokens"] += prompt
    turn["completion_tokens"] += completion
    turn["cache_read"] += cache_read
    turn["cache_write"] += cache_write
    turn["cost_events"].append(
        {
            "timestamp": msg.get("timestamp") or "",
            "request_id": None,
            "message_id": f"command-code:{msg.get('id')}",
            "model": model,
            "input_tokens": prompt - cache_read - cache_write,
            "cache_read_tokens": cache_read,
            # The CLI does not say which cache tier a write went to.
            "cache_write_5m_tokens": cache_write,
            "cache_write_1h_tokens": 0,
            "output_tokens": completion,
            "reasoning_tokens": 0,
            "usage_basis": "message_tokens",
            **source_attrs(msg),
        }
    )


def _turn_span(trace_id: str, root_id: str, turn: dict, cwd: str) -> SpanData:
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
            "cwd": cwd,
            "llm.token_count.prompt": prompt,
            "llm.token_count.completion": turn["completion_tokens"],
            "llm.token_count.cache_read": cache_read,
            "llm.token_count.cache_write": turn["cache_write"],
            "llm.token_count.input_fresh": max(0, prompt - cache_read - turn["cache_write"]),
            "llm.cost_events": turn["cost_events"],
            "llm.model_name": turn["model"],
        },
    )
    span.attributes.update(source_attrs(turn["_src"]))
    return span


def _tool_span(
    trace_id: str,
    turn: dict,
    msg: dict,
    block: dict,
    index: int,
    results: dict[str, tuple[dict, dict]],
    cwd: str,
) -> SpanData:
    raw_name = block.get("name")
    raw_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
    tool_name = _TOOL_ALIASES.get(raw_name, raw_name)

    args = block.get("input")
    input_value = json.dumps(_path_keys_first(args)) if isinstance(args, dict) else "{}"
    call_cwd = args.get("cwd") if isinstance(args, dict) else None

    call_id = block.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"{msg.get('id')}:{index}"
    result, result_msg = results.get(call_id, (None, None))
    output_value = _result_text(result) if result is not None else ""

    if result is None:
        status_code = "UNSET"
    elif _failed(raw_name, output_value):
        status_code = "ERROR"
    else:
        status_code = "OK"

    start = safe_parse_dt(msg.get("timestamp")) or turn["start"]
    end = (safe_parse_dt(result_msg.get("timestamp")) if result_msg else None) or start
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
            "cwd": call_cwd if isinstance(call_cwd, str) and call_cwd else cwd,
            "input.value": input_value[:_TRUNCATE],
            "output.value": output_value[:_TRUNCATE],
            "code.edit_evidence": extract_edit_evidence(
                tool_name, input_value, successful=status_code == "OK"
            ),
        },
    )
    span.attributes.update(source_attrs(msg))
    return span


def _path_keys_first(args: dict) -> dict:
    """A copy of `args` with path keys emitted before everything else."""
    first = {key: args[key] for key in _PATH_KEYS if key in args}
    rest = {key: value for key, value in args.items() if key not in first}
    return {**first, **rest}


def _result_text(result: dict) -> str:
    """The text of a `tool_result`; image blocks carry none."""
    content = result.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(
        b["text"]
        for b in _blocks(content)
        if b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def _failed(raw_name: str, output: str) -> bool:
    """Failure evidence in a result's text (the CLI records no error flag)."""
    if raw_name in _SHELL_TOOLS:
        m = _RE_EXIT_START.match(output)
        return m is not None and m.group(1) != "0"
    if raw_name == _SHELL_OUTPUT_TOOL:
        return any(code != "0" for code in _RE_EXIT_LINE.findall(output))
    return _RE_REFUSAL.match(output.lstrip()) is not None


__all__ = ["parse_command_code"]
