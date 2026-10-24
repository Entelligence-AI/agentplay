"""Parse Gemini CLI session JSONL into the universal SpanData shape.

Gemini CLI writes one file per session at
``~/.gemini/tmp/<project-slug>/chats/session-<date>-<short-id>.jsonl``. The
shape below was read from real Gemini CLI 0.65 sessions:

    {"sessionId": ..., "projectHash": <sha256 of the project root>,
     "startTime": <ISO>, "lastUpdated": <ISO>, "kind": "main"}
    {"id": ..., "timestamp": <ISO>, "type": "user", "content": [{"text": ...}]}
    {"$set": {"lastUpdated": <ISO>}}
    {"id": ..., "timestamp": <ISO>, "type": "gemini", "content": <text>,
     "thoughts": [...], "model": ..., "tokens": {"input", "output", "cached",
     "thoughts", "tool", "total"}, "toolCalls": [...]}
    {"id": ..., "timestamp": <ISO>, "type": "user",
     "content": [{"functionResponse": {...}}]}

# Records are rewritten, not appended once

The recorder appends a record each time it updates a message, so one message
`id` can appear more than once: a `gemini` message is written first without
`toolCalls` and again once the calls are attached, with the same `tokens`.
The last copy of each id wins, kept at the position of its first copy, so a
request's tokens are counted once.

# Turn boundaries

A `user` message carrying text opens a turn. The CLI injects a
`<session_context>` block as the first `user` message of every session; it is
not a prompt and opens no turn. `user` messages that only carry
`functionResponse` parts are tool results, which the matching `toolCalls`
entry already holds in `result`.

# Tool failures

A tool call records `status` (`success` | `error` | `cancelled`). The shell
tool reports `success` even when the command fails, and appends an
`Exit Code: <n>` line to its output only when n is non-zero (and a `Signal:`
line only when a signal ended the process). Those lines are the failure
signal for shell calls. The CLI writes `Output: <program output>` first, then
its labels in a fixed order: `Error:`, `Exit Code:`, `Signal:`,
`Background PIDs:`, `Process Group PGID:`. Only that trailing block is read,
so a label line the program printed is not taken as the verdict unless it is
the last line of output with no CLI label after it to tell them apart.

# Tokens

`input` includes `cached`; `thoughts` and `tool` are counted apart from
`output` and `input` (`total` = input + output + thoughts + tool). Thinking
tokens bill as output, so completion = output + thoughts.
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
_TOOL_ALIASES: dict[str, str] = aliases_for("gemini-cli")

_MESSAGE_TYPES = ("user", "gemini", "info", "warning", "error")

_SESSION_CONTEXT_PREFIX = "<session_context>"

_SHELL_TOOL = "run_shell_command"

# Path arguments, `file_path` first. `input.value` keeps 2,000 characters, and
# Gemini records `content` ahead of `file_path` on `write_file`, so a large
# write used to lose the path. `dir_path` is the directory argument on
# list/search/shell. The rest are keys edit evidence and the CLI's own path
# helper already accept. Hoisting them does not change the argument values
# `extract_edit_evidence` reads.
_PATH_KEYS = ("file_path", "dir_path", "path", "absolute_path", "filePath")

# The labels the CLI may append after `Output: ...`, last first. The result is
# read from its last line upward, each label at most once and only in this
# order, so a label line inside the program's output ends the scan.
_RE_TRAILER = (
    ("pgid", re.compile(r"^Process Group PGID: \S+\s*$")),
    ("pids", re.compile(r"^Background PIDs: .*$")),
    ("signal", re.compile(r"^Signal: (\S+)\s*$")),
    ("exit", re.compile(r"^Exit Code: (-?\d+)\s*$")),
)


def parse_gemini(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a Gemini CLI session JSONL file into a span tree.

    Args:
        data: raw JSONL bytes of one `session-*.jsonl` file.
        trace_id: the source-prefixed session id (e.g. `local:gemini-cli:<id>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by `LocalSource` (from the
            project's `.project_root`). The transcript itself records only a
            hash of the project path, so there is nothing to derive here.

    Returns:
        `(trace, all_spans)`; empty spans when the file holds no messages.
    """
    records = parse_jsonl_records(data)
    header: dict = next(
        (r for r in records if isinstance(r.get("sessionId"), str) and "type" not in r), {}
    )
    messages = _messages(records)
    if not messages:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    times = [
        t
        for t in (
            safe_parse_dt(header.get("startTime")),
            *(safe_parse_dt(m.get("timestamp")) for m in messages),
            *(safe_parse_dt(tc.get("timestamp")) for m in messages for tc in _tool_calls(m)),
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
            "reasoning_tokens": 0,
            "cache_read": 0,
            "cost_events": [],
            "model": "",
            "tools": [],
        }

    def _close_turn() -> None:
        if turn is not None:
            all_spans.append(_turn_span(trace_id, root_id, turn))
            all_spans.extend(turn["tools"])

    for msg in messages:
        mtype = msg.get("type")
        ts = safe_parse_dt(msg.get("timestamp"))

        if mtype == "user":
            prompt = _user_prompt(msg.get("content"))
            if prompt is not None:
                _close_turn()
                turn = _open_turn(msg, prompt)
                continue
        elif mtype == "gemini":
            if turn is None:
                # Model activity before any prompt still needs a turn to live
                # under (same fallback as kiro.py / cursor_store.py).
                turn = _open_turn(msg, "")
            model = model_id_or_none(msg.get("model")) or ""
            if model:
                turn["model"] = model
                session_model = model
            text = msg.get("content")
            if isinstance(text, str) and text:
                turn["output"].append(text)
            _add_usage(turn, msg, model)
            for index, call in enumerate(_tool_calls(msg)):
                turn["tools"].append(_tool_span(trace_id, turn, msg, call, index))

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
            "harness.agent_session_id": header.get("sessionId") or "",
            # The transcript does not record the CLI version.
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
        if isinstance(msg_id, str) and msg_id and record.get("type") in _MESSAGE_TYPES:
            # dict keeps first-insertion order; reassigning keeps the position.
            latest[msg_id] = record
    return list(latest.values())


def _tool_calls(msg: dict) -> list[dict]:
    calls = msg.get("toolCalls")
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _user_prompt(content: object) -> str | None:
    """The prompt text of a `user` message, or None when it opens no turn.

    None for tool-result messages (only `functionResponse` parts) and for the
    CLI's own `<session_context>` injection.
    """
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [
            p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)
        ]
    else:
        return None
    texts = [t for t in parts if t and not t.lstrip().startswith(_SESSION_CONTEXT_PREFIX)]
    if not texts:
        return None
    return "\n".join(texts)


def _add_usage(turn: dict, msg: dict, model: str) -> None:
    """Fold one `gemini` message's token record into its turn."""
    tokens = msg.get("tokens")
    if not isinstance(tokens, dict):
        return
    prompt = as_count(tokens.get("input")) + as_count(tokens.get("tool"))
    cache_read = min(as_count(tokens.get("cached")), prompt)
    thoughts = as_count(tokens.get("thoughts"))
    completion = as_count(tokens.get("output")) + thoughts
    if not prompt and not completion:
        return
    turn["prompt_tokens"] += prompt
    turn["completion_tokens"] += completion
    turn["reasoning_tokens"] += thoughts
    turn["cache_read"] += cache_read
    turn["cost_events"].append(
        {
            "timestamp": msg.get("timestamp") or "",
            "request_id": None,
            "message_id": f"gemini:{msg.get('id')}",
            "model": model,
            "input_tokens": prompt - cache_read,
            "cache_read_tokens": cache_read,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "output_tokens": completion,
            "reasoning_tokens": thoughts,
            "usage_basis": "message_tokens",
            **source_attrs(msg),
        }
    )


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
            # The transcript records no working directory; LocalSource stamps
            # the project root it read from `.project_root`.
            "cwd": "",
            "llm.token_count.prompt": prompt,
            "llm.token_count.completion": turn["completion_tokens"],
            # Subset of completion, not additive. Display-only.
            "llm.token_count.reasoning": turn["reasoning_tokens"],
            "llm.token_count.cache_read": cache_read,
            "llm.token_count.cache_write": 0,
            "llm.token_count.input_fresh": max(0, prompt - cache_read),
            "llm.cost_events": turn["cost_events"],
            "llm.model_name": turn["model"],
        },
    )
    span.attributes.update(source_attrs(turn["_src"]))
    return span


def _tool_span(trace_id: str, turn: dict, msg: dict, call: dict, index: int) -> SpanData:
    raw_name = call.get("name")
    raw_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
    tool_name = _TOOL_ALIASES.get(raw_name, raw_name)

    args = call.get("args")
    input_value = json.dumps(_path_keys_first(args)) if isinstance(args, dict) else "{}"
    response = _response(call)
    output_value = _output_text(response)

    status = call.get("status")
    if status in ("error", "cancelled") or (status == "success" and _failed(raw_name, response)):
        status_code = "ERROR"
    elif status == "success":
        status_code = "OK"
    else:
        status_code = "UNSET"

    start = safe_parse_dt(msg.get("timestamp")) or turn["start"]
    end = safe_parse_dt(call.get("timestamp")) or start
    if end < start:
        end = start

    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"{msg.get('id')}:{index}"
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


def _response(call: dict) -> dict:
    """The `functionResponse.response` object of a tool call's result."""
    result = call.get("result")
    for part in result if isinstance(result, list) else []:
        if isinstance(part, dict):
            fr = part.get("functionResponse")
            if isinstance(fr, dict) and isinstance(fr.get("response"), dict):
                return fr["response"]
    return {}


def _output_text(response: dict) -> str:
    output = response.get("output")
    if isinstance(output, str):
        return output
    return json.dumps(response) if response else ""


def _failed(raw_name: str, response: dict) -> bool:
    """Failure evidence inside a result the CLI marked `success`."""
    if response.get("error"):
        return True
    if raw_name != _SHELL_TOOL:
        return False
    output = response.get("output")
    if not isinstance(output, str):
        return False
    verdict = _shell_trailer(output)
    if verdict.get("exit", "0") != "0":
        return True
    return verdict.get("signal", "(none)") not in ("(none)", "0")


def _shell_trailer(output: str) -> dict[str, str]:
    """The CLI's own labels at the end of a shell result, keyed by label."""
    lines = output.split("\n")
    found: dict[str, str] = {}
    slot = 0
    # Line 0 is `Output: ...`, written by the CLI ahead of any label.
    while len(lines) > 1 and slot < len(_RE_TRAILER):
        key, pattern = _RE_TRAILER[slot]
        slot += 1
        m = pattern.match(lines[-1])
        if m:
            found[key] = m.group(1) if m.groups() else ""
            lines.pop()
    return found


__all__ = ["parse_gemini"]
