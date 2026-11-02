"""Parse a materialized Goose session into the universal SpanData shape.

Goose keeps its sessions in SQLite (``<data-dir>/sessions/sessions.db``);
`agentplay.materialize.find_goose_candidates` turns each session into the
NDJSON this module reads:

    {"type": "goose_session", "id", "working_dir", "created_at", "updated_at",
     "session_type", "provider", "model", "parent_session_id"}
    {"type": "goose_message", "id", "role": "user" | "assistant",
     "created": <epoch s>, "content": [<blocks>], "metadata": {"userVisible",
     "agentVisible"}}
    {"type": "goose_usage", "created", "model", "input_tokens",
     "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost",
     "is_compaction"}

`content` is Goose's own content-block array (camelCase, tagged by `type`):
`text`, `thinking`, `redactedThinking`, `image`, `toolRequest`
(`{id, toolCall: {status: "success", value: {name, arguments}} |
{status: "error", error}}`), `toolResponse` (`{id, toolResult: {status:
"success", value: {content, isError}} | {status: "error", error}}`), and UI-only
blocks (`toolConfirmationRequest`, `actionRequired`, `systemNotification`).

# Turn boundaries

A `user` message carrying text opens a turn. `user` messages that only carry
`toolResponse` blocks are paired with their `toolRequest` by id. A message whose
`metadata.userVisible` is false is Goose's own bookkeeping (a compaction
summary, an injected reminder) and is skipped.

# Tool failures

Goose records them: a `toolResult` with `status: "error"` (the tool could not
run) or `value.isError` true (it ran and failed, e.g. a shell exit code other
than 0). A `toolCall` with `status: "error"` is a call the model malformed.

# Tokens

Newer Goose stamps each request's usage on the assistant message it produced
(`metadata.usage`), which ties it to its turn exactly; the `goose_usage`
lines (Goose's `usage_ledger`) then only add compaction requests. Sessions
whose messages carry no usage fall back to the ledger, each row placed after
the last assistant message at or before its time. `input_tokens` includes
`cache_read_tokens` and `cache_write_tokens` (as recorded in real sessions);
each is clamped so no count goes negative.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    as_count,
    parse_jsonl_records,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData, model_id_or_none
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# input.value / output.value truncation, matching every other local parser.
_TRUNCATE = 2000

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("goose")

# Older Goose's single file tool; its `command` argument picks the operation.
_TEXT_EDITOR = "developer__text_editor"
_TEXT_EDITOR_COMMANDS = {"view": "Read", "write": "Write"}

# File tools whose `path` argument is renamed `file_path`, the key every
# downstream reader of `input.value` looks for.
_FILE_TOOLS = ("write", "edit", _TEXT_EDITOR)

# Argument renames onto the keys downstream readers (edit evidence, line
# counts) look for: `edit` names its replacement `before` / `after`, the older
# text editor `old_str` / `new_str` / `file_text`.
_ARG_RENAMES = {
    "edit": {"before": "old_string", "after": "new_string"},
    _TEXT_EDITOR: {"old_str": "old_string", "new_str": "new_string", "file_text": "content"},
}

# Goose stores times in seconds; a value past this is milliseconds.
_MS_THRESHOLD = 10_000_000_000


def parse_goose(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse one materialized Goose session into a span tree.

    Args:
        data: raw NDJSON bytes of one materialized session.
        trace_id: the source-prefixed session id (e.g. `local:goose:<id>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by `LocalSource` from the
            header's `working_dir`.

    Returns:
        `(trace, all_spans)`; empty spans when the session holds no messages.
    """
    records = parse_jsonl_records(data)
    header: dict = next((r for r in records if r.get("type") == "goose_session"), {})
    events = [
        r
        for r in records
        if (r.get("type") == "goose_message" and r.get("role") in ("user", "assistant"))
        or r.get("type") == "goose_usage"
    ]
    if not any(r.get("type") == "goose_message" for r in events):
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    raw_cwd = header.get("working_dir")
    cwd = raw_cwd if isinstance(raw_cwd, str) else ""
    header_model = model_id_or_none(header.get("model")) or ""
    results = _tool_results(events)
    # Newer Goose stamps each request's usage on the assistant message it
    # produced, an exact link; the ledger then only adds requests that made
    # no message (compactions).
    per_message_usage = any(_message_usage(r) is not None for r in events)

    times = [t for t in (_dt(header.get("created_at")), *(_dt(r.get("created")) for r in events)) if t]
    session_start = min(times) if times else datetime.now(UTC)
    session_end = max(times) if times else session_start

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []
    turn: dict | None = None
    turn_num = 0
    session_model = ""
    usage_num = 0

    def _open_turn(record: dict, prompt: str) -> dict:
        nonlocal turn_num
        turn_num += 1
        ts = _dt(record.get("created")) or session_start
        return {
            "span_id": f"{trace_id}:turn:{turn_num}",
            "num": turn_num,
            "_src": record,
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
            if not turn["model"]:
                turn["model"] = header_model
            all_spans.append(_turn_span(trace_id, root_id, turn, cwd))
            all_spans.extend(turn["tools"])

    def _usage(usage: dict, source: dict) -> None:
        nonlocal usage_num, session_model
        if turn is None:
            return
        usage_num += 1
        model = _add_usage(turn, usage, source, usage_num, header_model)
        if model:
            session_model = model

    for record in events:
        ts = _dt(record.get("created"))
        if record.get("type") == "goose_usage":
            if not per_message_usage or record.get("is_compaction") in (1, True):
                _usage(record, record)
        elif _visible(record):
            content = _blocks(record.get("content"))
            if record["role"] == "user":
                prompt = _user_prompt(content)
                if prompt is not None:
                    _close_turn()
                    turn = _open_turn(record, prompt)
                    continue
            else:
                if turn is None:
                    # Model activity before any prompt (a recipe's first
                    # step) still needs a turn to live under.
                    turn = _open_turn(record, "")
                for block in content:
                    kind = block.get("type")
                    if kind == "text" and isinstance(block.get("text"), str) and block["text"]:
                        turn["output"].append(block["text"])
                    elif kind == "toolRequest":
                        index = len(turn["tools"])
                        turn["tools"].append(_tool_span(trace_id, turn, record, block, index, results, cwd))
        if record.get("type") == "goose_message":
            # Counted even on a hidden message: the request was still billed.
            usage = _message_usage(record)
            if usage is not None:
                _usage(usage, record)
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
            "llm.model_name": session_model or header_model,
            "session.id": trace_id,
            "project.name": project_name or "",
            "harness.agent_session_id": header.get("id") if isinstance(header.get("id"), str) else "",
            # The db does not record which Goose version wrote a session.
            "harness.agent_version": "",
        },
    )
    all_spans.insert(0, root_span)
    trace = TraceData(trace_id=trace_id, start_time=session_start, end_time=session_end, spans=all_spans)
    return trace, all_spans


def _dt(value: object) -> datetime | None:
    """A UTC datetime from Goose's epoch seconds (or milliseconds)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    seconds = value / 1000 if value > _MS_THRESHOLD else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _blocks(content: object) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _visible(record: dict) -> bool:
    """False for a message Goose keeps from the user (a summary, a reminder)."""
    metadata = record.get("metadata")
    return not (isinstance(metadata, dict) and metadata.get("userVisible") is False)


def _user_prompt(content: list[dict]) -> str | None:
    """The prompt text of a `user` message, or None when it carries no text."""
    texts = [
        b["text"]
        for b in content
        if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip()
    ]
    return "\n".join(texts) if texts else None


def _tool_results(events: list[dict]) -> dict[str, tuple[dict, dict]]:
    """Each `toolResponse` block with the message that carried it, by request id."""
    out: dict[str, tuple[dict, dict]] = {}
    for record in events:
        if record.get("type") != "goose_message":
            continue
        for block in _blocks(record.get("content")):
            call_id = block.get("id")
            if block.get("type") == "toolResponse" and isinstance(call_id, str) and call_id:
                out[call_id] = (block, record)
    return out


def _message_usage(record: dict) -> dict | None:
    """An assistant message's own `metadata.usage`, in the ledger's field names."""
    if record.get("type") != "goose_message" or record.get("role") != "assistant":
        return None
    metadata = record.get("metadata")
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return None
    inference = metadata.get("inference")
    model = inference.get("requestedModel") if isinstance(inference, dict) else None
    return {
        "created": record.get("created"),
        "model": model,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "cache_read_tokens": usage.get("cacheReadTokens"),
        "cache_write_tokens": usage.get("cacheWriteTokens"),
    }


def _add_usage(turn: dict, record: dict, source: dict, num: int, header_model: str) -> str:
    """Fold one model request's usage into its turn; returns the request's model.

    `record` holds the ledger's field names; `source` is the line it came from.
    """
    model = model_id_or_none(record.get("model")) or ""
    if model:
        turn["model"] = model
    prompt = as_count(record.get("input_tokens"))
    cache_read = min(as_count(record.get("cache_read_tokens")), prompt)
    cache_write = min(as_count(record.get("cache_write_tokens")), prompt - cache_read)
    completion = as_count(record.get("output_tokens"))
    if not prompt and not completion:
        return model
    turn["prompt_tokens"] += prompt
    turn["completion_tokens"] += completion
    turn["cache_read"] += cache_read
    turn["cache_write"] += cache_write
    created = _dt(record.get("created"))
    turn["cost_events"].append(
        {
            "timestamp": created.isoformat() if created else "",
            "request_id": None,
            "message_id": f"goose:usage:{num}",
            "model": model or header_model,
            "input_tokens": prompt - cache_read - cache_write,
            "cache_read_tokens": cache_read,
            # Goose does not say which cache tier a write went to.
            "cache_write_5m_tokens": cache_write,
            "cache_write_1h_tokens": 0,
            "output_tokens": completion,
            "reasoning_tokens": 0,
            "usage_basis": "message_tokens",
            **source_attrs(source),
        }
    )
    return model


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
    record: dict,
    block: dict,
    index: int,
    results: dict[str, tuple[dict, dict]],
    cwd: str,
) -> SpanData:
    call = block.get("toolCall") if isinstance(block.get("toolCall"), dict) else {}
    value = call.get("value") if call.get("status") == "success" else None
    value = value if isinstance(value, dict) else {}
    raw_name = value.get("name")
    raw_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
    raw_args = value.get("arguments")
    args = _normalize_args(raw_name, raw_args) if isinstance(raw_args, dict) else {}
    tool_name = _canonical_name(raw_name, args)
    input_value = json.dumps(args)

    call_id = block.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"{record.get('id')}:{index}"
    response, response_msg = results.get(call_id, (None, None))
    result = response.get("toolResult") if isinstance(response, dict) else None
    result = result if isinstance(result, dict) else None

    if call.get("status") == "error":
        status_code = "ERROR"
        output_value = call.get("error") if isinstance(call.get("error"), str) else ""
    elif result is None:
        status_code = "UNSET"
        output_value = ""
    else:
        status_code = "ERROR" if _failed(result) else "OK"
        output_value = _result_text(result)

    start = _dt(record.get("created")) or turn["start"]
    end = (_dt(response_msg.get("created")) if response_msg else None) or start
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
            "cwd": cwd,
            "input.value": input_value[:_TRUNCATE],
            "output.value": output_value[:_TRUNCATE],
            "code.edit_evidence": extract_edit_evidence(tool_name, input_value, successful=status_code == "OK"),
        },
    )
    span.attributes.update(source_attrs(record))
    return span


def _canonical_name(raw_name: str, args: dict) -> str:
    if raw_name == _TEXT_EDITOR:
        command = args.get("command")
        if isinstance(command, str) and command in _TEXT_EDITOR_COMMANDS:
            return _TEXT_EDITOR_COMMANDS[command]
    return _TOOL_ALIASES.get(raw_name, raw_name)


def _normalize_args(raw_name: str, args: dict) -> dict:
    """The call's arguments with file paths under `file_path`, emitted first.

    Path first so a large `write` keeps its path inside `input.value`'s 2,000
    characters; replacement and content keys take their downstream names.
    """
    out = dict(args)
    if raw_name in _FILE_TOOLS and "path" in out and "file_path" not in out:
        out["file_path"] = out.pop("path")
    for old, new in _ARG_RENAMES.get(raw_name, {}).items():
        if old in out and new not in out:
            out[new] = out.pop(old)
    if "file_path" in out:
        out = {"file_path": out.pop("file_path"), **out}
    return out


def _failed(result: dict) -> bool:
    if result.get("status") != "success":
        return True
    value = result.get("value")
    return isinstance(value, dict) and value.get("isError") is True


def _result_text(result: dict) -> str:
    """The model-facing text of a tool result.

    Goose sometimes returns the same output twice, once for the model and
    once for the UI (`annotations.audience`); only the model's copy is kept.
    """
    if result.get("status") != "success":
        error = result.get("error")
        return error if isinstance(error, str) else ""
    value = result.get("value")
    # Older sessions stored the content list itself as the value.
    content = value.get("content") if isinstance(value, dict) else value
    texts = [b for b in _blocks(content) if b.get("type") == "text" and isinstance(b.get("text"), str)]
    return "\n".join(b["text"] for b in texts if _for_model(b))


def _for_model(block: dict) -> bool:
    """False for a block annotated for an audience that excludes the model (UI-only text)."""
    annotations = block.get("annotations")
    audience = annotations.get("audience") if isinstance(annotations, dict) else None
    return not isinstance(audience, list) or "assistant" in audience


__all__ = ["parse_goose"]
