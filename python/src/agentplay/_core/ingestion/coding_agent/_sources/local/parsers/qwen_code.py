"""Parse Qwen Code session JSONL into the universal SpanData shape.

Qwen Code writes one file per session at
``~/.qwen/projects/<sanitized-cwd>/chats/<session-id>.jsonl``. It began as a
Gemini CLI fork, but its transcript is no longer Gemini CLI's: every line is a
``ChatRecord`` linked to the one before it by ``uuid`` / ``parentUuid``, and the
message is a Gemini API ``Content``. The shape below was read from real Qwen
Code 0.26 sessions:

    {"uuid", "parentUuid", "sessionId", "timestamp", "type": "user", "cwd",
     "version", "message": {"role": "user", "parts": [{"text": ...}]}}
    {..., "type": "assistant", "model": ...,
     "message": {"role": "model", "parts": [{"text": ..., "thought": true},
                                             {"text": ...},
                                             {"functionCall": {"id", "name", "args"}}]},
     "usageMetadata": {"promptTokenCount", "candidatesTokenCount",
                       "cachedContentTokenCount", "thoughtsTokenCount", ...}}
    {..., "type": "tool_result",
     "message": {"role": "user", "parts": [{"functionResponse": {"id", "name", "response"}}]},
     "toolCallResult": {"callId", "status", "error", "errorType", "resultDisplay"}}
    {..., "type": "system", "subtype": "chat_compression" | "slash_command" | ...,
     "systemPayload": {...}}

# Turn boundaries

A ``user`` record with no ``subtype`` whose parts carry text opens a turn. A
``mid_turn_user_message`` (typed while tools ran) joins the running turn, and
the other user subtypes (Goal runtime messages, messages between session
agents) are not prompts and open none. Background subagents write their own
files under ``subagents/``; a record marked ``isSidechain`` is skipped here.

# Tool failures

``toolCallResult.status`` is ``success``, ``error`` or ``cancelled``. The shell
tool reports a non-zero exit (and a signal) as ``error`` itself, so the status
is the whole verdict. A result with no ``status`` falls back to its ``error``.

# Tokens

``usageMetadata`` has the Gemini API's field names, and ``promptTokenCount``
includes ``cachedContentTokenCount``. Thinking tokens bill as output, but
whether ``candidatesTokenCount`` already holds them depends on the provider:
the Gemini API counts ``thoughtsTokenCount`` apart (total = prompt + candidates
+ thoughts), while the OpenAI-compatible providers Qwen Code also talks to
report it as a subset (total = prompt + candidates, as in the real sessions).
Completion is therefore total - prompt, and candidates + thoughts only when no
total is recorded. Each assistant record is one request.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging

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
_TOOL_ALIASES: dict[str, str] = aliases_for("qwen-code")

# A user record of this subtype joins the running turn instead of opening one.
_JOINS_TURN = "mid_turn_user_message"

# Path arguments, `file_path` first, so a large `write_file` keeps its path
# inside `input.value`'s 2,000 characters. `path` is the directory argument of
# `list_directory`, `glob` and `grep_search`. Hoisting them does not change the
# argument values `extract_edit_evidence` reads.
_PATH_KEYS = ("file_path", "absolute_path", "path", "directory")

_FAILED_STATUSES = ("error", "cancelled")


def parse_qwen_code(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a Qwen Code session JSONL file into a span tree.

    Args:
        data: raw JSONL bytes of one `chats/<session-id>.jsonl` file.
        trace_id: the source-prefixed session id (e.g. `local:qwen-code:<id>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by `LocalSource` from the
            records' `cwd`.

    Returns:
        `(trace, all_spans)`; empty spans when the file holds no messages.
    """
    records = _records(parse_jsonl_records(data))
    if not any(r.get("type") in ("user", "assistant") for r in records):
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    results = _tool_results(records)
    times = [t for t in (safe_parse_dt(r.get("timestamp")) for r in records) if t is not None]
    session_start = min(times) if times else datetime.now(UTC)
    session_end = max(times) if times else session_start

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []
    turn: dict | None = None
    turn_num = 0
    session_model = ""
    session_id = ""
    version = ""
    compactions: list[dict] = []

    def _open_turn(rec: dict, prompt: str) -> dict:
        nonlocal turn_num
        turn_num += 1
        ts = safe_parse_dt(rec.get("timestamp")) or session_start
        return {
            "span_id": f"{trace_id}:turn:{turn_num}",
            "num": turn_num,
            "_src": rec,
            "prompt": prompt,
            "cwd": _str(rec.get("cwd")),
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

    for rec in records:
        kind = rec.get("type")
        ts = safe_parse_dt(rec.get("timestamp"))
        session_id = session_id or _str(rec.get("sessionId"))
        version = version or _str(rec.get("version"))

        if kind == "user":
            prompt = _user_prompt(rec)
            if prompt is not None and (rec.get("subtype") is None or turn is None):
                _close_turn()
                turn = _open_turn(rec, prompt)
                continue
        elif kind == "assistant":
            if turn is None:
                # Model activity before any prompt still needs a turn to live
                # under (same fallback as gemini.py / command_code.py).
                turn = _open_turn(rec, "")
            model = model_id_or_none(rec.get("model")) or ""
            if model:
                turn["model"] = model
                session_model = model
            for index, part in enumerate(_parts(rec)):
                text = part.get("text")
                if isinstance(text, str) and text and not part.get("thought"):
                    turn["output"].append(text)
                call = part.get("functionCall")
                if isinstance(call, dict):
                    turn["tools"].append(_tool_span(trace_id, turn, rec, call, index, results))
            _add_usage(turn, rec, model)
        elif kind == "system" and rec.get("subtype") == "chat_compression":
            compactions.append(_compaction(rec))

        if turn is not None and ts is not None and ts > turn["end"]:
            turn["end"] = ts
    _close_turn()

    root_attrs: dict = {
        "user.id": user_id or "",
        "llm.model_name": session_model,
        "session.id": trace_id,
        "project.name": project_name or "",
        "harness.agent_session_id": session_id,
        "harness.agent_version": version,
    }
    if compactions:
        root_attrs["harness.compactions"] = compactions
    root_span = SpanData(
        span_id=root_id,
        trace_id=trace_id,
        parent_id=None,
        name="Session",
        span_kind="CHAIN",
        status_code="OK",
        start_time=session_start,
        end_time=session_end,
        attributes=root_attrs,
    )
    all_spans.insert(0, root_span)
    trace = TraceData(
        trace_id=trace_id,
        start_time=session_start,
        end_time=session_end,
        spans=all_spans,
    )
    return trace, all_spans


def _records(records: list[dict]) -> list[dict]:
    """The session's own records in order, keeping the last copy of each `uuid`.

    The recorder only appends, but a resumed session can repeat a record; the
    copy keeps the position of the first. Subagent (sidechain) records belong
    to the subagent's own transcript.
    """
    latest: dict[str, dict] = {}
    for record in records:
        uid = record.get("uuid")
        if isinstance(uid, str) and uid and not record.get("isSidechain"):
            # dict keeps first-insertion order; reassigning keeps the position.
            latest[uid] = record
    return list(latest.values())


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _parts(rec: dict) -> list[dict]:
    message = rec.get("message")
    parts = message.get("parts") if isinstance(message, dict) else None
    return [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []


def _user_prompt(rec: dict) -> str | None:
    """The prompt text of a `user` record, or None when it opens no turn.

    None for records of a subtype that is not a prompt and for records that
    carry no text (tool results ride in `tool_result` records, but a user
    `Content` can also hold `functionResponse` parts).
    """
    if rec.get("type") != "user":
        return None
    subtype = rec.get("subtype")
    if subtype is not None and subtype != _JOINS_TURN:
        return None
    texts = [
        p["text"]
        for p in _parts(rec)
        if isinstance(p.get("text"), str) and p["text"].strip() and not p.get("thought")
    ]
    if not texts:
        return None
    return "\n".join(texts)


def _tool_results(records: list[dict]) -> dict[str, dict]:
    """Each `tool_result` record by the call id it answers."""
    out: dict[str, dict] = {}
    for rec in records:
        if rec.get("type") != "tool_result":
            continue
        info = rec.get("toolCallResult")
        call_id = info.get("callId") if isinstance(info, dict) else None
        if not isinstance(call_id, str) or not call_id:
            response = next(
                (p["functionResponse"] for p in _parts(rec) if isinstance(p.get("functionResponse"), dict)),
                {},
            )
            call_id = response.get("id")
        if isinstance(call_id, str) and call_id:
            out[call_id] = rec
    return out


def _add_usage(turn: dict, rec: dict, model: str) -> None:
    """Fold one assistant record's `usageMetadata` into its turn."""
    usage = rec.get("usageMetadata")
    if not isinstance(usage, dict):
        return
    prompt = as_count(usage.get("promptTokenCount")) + as_count(usage.get("toolUsePromptTokenCount"))
    cache_read = min(as_count(usage.get("cachedContentTokenCount")), prompt)
    thoughts = as_count(usage.get("thoughtsTokenCount"))
    candidates = as_count(usage.get("candidatesTokenCount"))
    total = as_count(usage.get("totalTokenCount"))
    # Whether `candidatesTokenCount` already holds the thinking tokens depends
    # on the provider (see the module docstring); the total settles it.
    completion = total - prompt if total > prompt else candidates + thoughts
    thoughts = min(thoughts, completion)
    if not prompt and not completion:
        return
    turn["prompt_tokens"] += prompt
    turn["completion_tokens"] += completion
    turn["reasoning_tokens"] += thoughts
    turn["cache_read"] += cache_read
    turn["cost_events"].append(
        {
            "timestamp": rec.get("timestamp") or "",
            "request_id": None,
            "message_id": f"qwen-code:{rec.get('uuid')}",
            "model": model,
            "input_tokens": prompt - cache_read,
            "cache_read_tokens": cache_read,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "output_tokens": completion,
            "reasoning_tokens": thoughts,
            "usage_basis": "message_tokens",
            **source_attrs(rec),
        }
    )


def _compaction(rec: dict) -> dict:
    """A `chat_compression` record as the root span's compaction entry."""
    payload = rec.get("systemPayload")
    info = payload.get("info") if isinstance(payload, dict) else None
    pre = as_count(info.get("originalTokenCount")) if isinstance(info, dict) else 0
    return {"timestamp": rec.get("timestamp") or "", "pre_tokens": pre}


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
            "cwd": turn["cwd"],
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


def _tool_span(
    trace_id: str,
    turn: dict,
    rec: dict,
    call: dict,
    index: int,
    results: dict[str, dict],
) -> SpanData:
    raw_name = call.get("name")
    raw_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
    tool_name = _TOOL_ALIASES.get(raw_name, raw_name)

    args = call.get("args")
    input_value = json.dumps(_path_keys_first(args)) if isinstance(args, dict) else "{}"

    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"{rec.get('uuid')}:{index}"
    result = results.get(call_id)
    info = result.get("toolCallResult") if result is not None else None
    info = info if isinstance(info, dict) else {}
    output_value = _output_text(result) if result is not None else ""

    if result is None:
        status_code = "UNSET"
    elif _failed(info):
        status_code = "ERROR"
    else:
        status_code = "OK"

    start = safe_parse_dt(rec.get("timestamp")) or turn["start"]
    end = (safe_parse_dt(result.get("timestamp")) if result is not None else None) or start
    if end < start:
        end = start
    if end > turn["end"]:
        turn["end"] = end

    cwd = _str((result or rec).get("cwd")) or turn["cwd"]
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
            "cwd": cwd,
            "input.value": input_value[:_TRUNCATE],
            "output.value": output_value[:_TRUNCATE],
            "code.edit_evidence": extract_edit_evidence(
                tool_name, input_value, successful=status_code == "OK"
            ),
        },
    )
    span.attributes.update(source_attrs(rec))
    return span


def _path_keys_first(args: dict) -> dict:
    """A copy of `args` with path keys emitted before everything else."""
    first = {key: args[key] for key in _PATH_KEYS if key in args}
    rest = {key: value for key, value in args.items() if key not in first}
    return {**first, **rest}


def _output_text(result: dict) -> str:
    """The model-facing text of a `tool_result` record."""
    for part in _parts(result):
        fr = part.get("functionResponse")
        response = fr.get("response") if isinstance(fr, dict) else None
        if isinstance(response, dict):
            for key in ("output", "error"):
                if isinstance(response.get(key), str):
                    return response[key]
            return json.dumps(response)
    return ""


def _failed(info: dict) -> bool:
    """Whether a `toolCallResult` reports a failure."""
    status = info.get("status")
    if isinstance(status, str):
        return status in _FAILED_STATUSES
    return bool(info.get("error"))


__all__ = ["parse_qwen_code"]
