"""Parse Roo Code (and Cline) session transcripts into the universal SpanData shape.

Roo Code stores sessions under VS Code global storage:
    User/globalStorage/rooveterinaryinc.roo-cline/tasks/<task-id>/

Inside each task directory:
  - `ui_messages.json`: Array of UI messages with timestamps, tool requests/outputs,
    and API token metrics (`api_req_started`).
  - `api_conversation_history.json`: Raw Anthropic/OpenAI API message parameters
    containing `tool_use` and `tool_result` blocks.

This parser handles both `ui_messages.json` (primary, carries timestamps and tokens)
and `api_conversation_history.json` payloads.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re
from typing import Any

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    SOURCE_KIND_JSON_DOC,
    as_count,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData, model_id_or_none
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

_TOOL_ALIASES: dict[str, str] = aliases_for("roo-code")

_TRUNCATE = 2000

_RE_EXIT_CODE = re.compile(
    r"(?:Process exited with code|Command exited with code|Exit code:)\s*(-?\d+)",
    re.IGNORECASE,
)


def _source_attrs(record: object) -> dict[str, Any]:
    """Source-cursor attributes for a Roo JSON document record (content-hash anchored)."""
    rec = record if isinstance(record, dict) else {}
    return {
        k: v
        for k, v in source_attrs(rec, kind=SOURCE_KIND_JSON_DOC).items()
        if k != "source.byte_offset"
    }


def _safe_json(val: Any) -> Any:
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip().startswith(("{", "[")):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _load_records(data: bytes) -> list[dict[str, Any]]:
    """Load items from either JSON array or NDJSON lines."""
    stripped = data.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(data)
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict)]
        if isinstance(parsed, dict):
            if "messages" in parsed and isinstance(parsed["messages"], list):
                return [m for m in parsed["messages"] if isinstance(m, dict)]
            return [parsed]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    records: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                records.append(rec)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return records


def _is_api_history(records: list[dict[str, Any]]) -> bool:
    """Check if records are shaped like api_conversation_history.json ({role, content})."""
    if not records:
        return False
    first = records[0]
    return "role" in first and "content" in first


def _normalize_tool_args(raw_args: dict[str, Any]) -> dict[str, Any]:
    """Map tool arguments to canonical names (e.g. path -> file_path)."""
    normalized: dict[str, Any] = dict(raw_args)
    if "path" in raw_args and "file_path" not in normalized:
        normalized["file_path"] = raw_args["path"]
    if "command" in raw_args and "CommandLine" not in normalized:
        normalized["CommandLine"] = raw_args["command"]
    return normalized


def parse_roo(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse Roo Code session data into universal TraceData and SpanData.

    Args:
        data: raw bytes of ui_messages.json or api_conversation_history.json.
        trace_id: namespaced session identifier (local:roo-code:<task-id>).
        user_id: optional user identifier.
        project_name: optional workspace/repo project name.

    Returns:
        tuple of (TraceData, list of SpanData).
    """
    records = _load_records(data)
    if not records:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    if _is_api_history(records):
        return _parse_api_history(records, trace_id, user_id=user_id, project_name=project_name)
    return _parse_ui_messages(records, trace_id, user_id=user_id, project_name=project_name)


def _parse_ui_messages(
    records: list[dict[str, Any]],
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse ui_messages.json format."""
    times: list[datetime] = []
    for r in records:
        ts = r.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            times.append(datetime.fromtimestamp(ts / 1000.0, tz=UTC))

    session_start = min(times) if times else datetime.now(UTC)
    session_end = max(times) if times else session_start

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []
    turn: dict[str, Any] | None = None
    turn_num = 0
    pending_tool: SpanData | None = None
    last_model: str = ""

    def _open_turn(prompt: str, ts: datetime, src: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal turn_num
        turn_num += 1
        return {
            "span_id": f"{trace_id}:turn:{turn_num}",
            "num": turn_num,
            "prompt": prompt,
            "start": ts,
            "end": ts,
            "output": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read": 0,
            "cost_events": [],
            "tools": [],
            "model": last_model,
            "_src": src,
        }

    def _close_turn() -> None:
        nonlocal pending_tool
        if pending_tool is not None and turn is not None:
            turn["tools"].append(pending_tool)
            pending_tool = None
        if turn is not None:
            all_spans.append(_build_turn_span(trace_id, root_id, turn))
            all_spans.extend(turn["tools"])

    for r in records:
        mtype = r.get("type")
        say = r.get("say")
        ask = r.get("ask")
        raw_text = r.get("text") or ""
        ts_val = r.get("ts")
        ts = (
            datetime.fromtimestamp(ts_val / 1000.0, tz=UTC)
            if isinstance(ts_val, (int, float)) and ts_val > 0
            else (turn["end"] if turn is not None else session_start)
        )

        # 1. Turn opener: user prompt, task initiation, or feedback
        is_turn_start = (
            (mtype == "say" and say in ("task", "user_feedback"))
            or (turn is None and raw_text and mtype == "say" and say == "text")
        )
        if is_turn_start:
            _close_turn()
            prompt_text = raw_text if isinstance(raw_text, str) else ""
            if prompt_text.strip().startswith("{"):
                parsed_prompt = _safe_json(prompt_text)
                if isinstance(parsed_prompt, dict) and "text" in parsed_prompt:
                    prompt_text = str(parsed_prompt["text"])
            turn = _open_turn(prompt_text, ts, r)
            continue

        if turn is None:
            turn = _open_turn("", ts, r)

        if ts > turn["end"]:
            turn["end"] = ts

        # 2. Token / Model Usage metrics
        if mtype == "say" and say in ("api_req_started", "api_req_finished"):
            parsed_usage = _safe_json(raw_text)
            if isinstance(parsed_usage, dict):
                p_tokens = as_count(parsed_usage.get("tokensIn")) or as_count(parsed_usage.get("prompt_tokens"))
                c_tokens = as_count(parsed_usage.get("tokensOut")) or as_count(parsed_usage.get("completion_tokens"))
                cr_tokens = as_count(parsed_usage.get("cacheReads"))
                model_cand = model_id_or_none(parsed_usage.get("model") or parsed_usage.get("selectedModel"))
                if model_cand:
                    last_model = model_cand
                    turn["model"] = model_cand

                turn["prompt_tokens"] += p_tokens
                turn["completion_tokens"] += c_tokens
                turn["cache_read"] += cr_tokens
                turn["cost_events"].append(
                    {
                        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "model": turn["model"] or "",
                        "input_tokens": max(0, p_tokens - cr_tokens),
                        "cache_read_tokens": cr_tokens,
                        "cache_write_5m_tokens": as_count(parsed_usage.get("cacheWrites")),
                        "cache_write_1h_tokens": 0,
                        "output_tokens": c_tokens,
                        "reasoning_tokens": 0,
                        "usage_basis": "message_tokens",
                    }
                )
            continue

        # 3. Tool execution: Command
        if (mtype == "ask" and ask == "command") or (mtype == "say" and say == "command"):
            if pending_tool is not None and pending_tool.name == "Bash" and mtype == "say":
                continue
            if pending_tool is not None:
                turn["tools"].append(pending_tool)
                pending_tool = None
            cmd = raw_text if isinstance(raw_text, str) else ""
            tool_idx = len(turn["tools"]) + 1
            input_val = json.dumps(_normalize_tool_args({"command": cmd, "CommandLine": cmd}))
            pending_tool = SpanData(
                span_id=f"{trace_id}:tool:{turn['num']}:{tool_idx}",
                trace_id=trace_id,
                parent_id=turn["span_id"],
                name="Bash",
                span_kind="CHAIN",
                status_code="OK",
                start_time=ts,
                end_time=ts,
                attributes={
                    "tool.name": "Bash",
                    "input.value": input_val[:_TRUNCATE],
                    "output.value": "",
                    "cwd": "",
                    **_source_attrs(r),
                },
            )
            continue

        # 4. Command Output
        if mtype == "say" and say == "command_output":
            out_str = raw_text if isinstance(raw_text, str) else ""
            status_code = "OK"
            exit_match = _RE_EXIT_CODE.search(out_str)
            if exit_match and int(exit_match.group(1)) != 0:
                status_code = "ERROR"
            elif "command failed" in out_str.lower() or out_str.strip().startswith("Error:"):
                status_code = "ERROR"

            if pending_tool is not None:
                pending_tool.status_code = status_code
                pending_tool.end_time = ts
                pending_tool.attributes["output.value"] = out_str[:_TRUNCATE]
                turn["tools"].append(pending_tool)
                pending_tool = None
            continue

        # 5. Generic / File Tool
        if mtype == "ask" and ask == "tool":
            if pending_tool is not None:
                turn["tools"].append(pending_tool)
                pending_tool = None
            tool_data = _safe_json(raw_text)
            if isinstance(tool_data, dict):
                raw_tool = tool_data.get("tool") or tool_data.get("name") or "tool"
                canonical_tool = _TOOL_ALIASES.get(raw_tool, raw_tool)
                args = _normalize_tool_args({k: v for k, v in tool_data.items() if k not in ("tool", "name")})
                input_val = json.dumps(args)
                status_code = "ERROR" if (tool_data.get("success") is False or "error" in tool_data) else "OK"
                tool_idx = len(turn["tools"]) + 1
                pending_tool = SpanData(
                    span_id=f"{trace_id}:tool:{turn['num']}:{tool_idx}",
                    trace_id=trace_id,
                    parent_id=turn["span_id"],
                    name=canonical_tool,
                    span_kind="CHAIN",
                    status_code=status_code,
                    start_time=ts,
                    end_time=ts,
                    attributes={
                        "tool.name": canonical_tool,
                        "input.value": input_val[:_TRUNCATE],
                        "output.value": str(tool_data.get("content") or tool_data.get("output") or "")[:_TRUNCATE],
                        "cwd": "",
                        "code.edit_evidence": extract_edit_evidence(
                            canonical_tool, input_val, successful=status_code == "OK"
                        ),
                        **_source_attrs(r),
                    },
                )
            continue

        if mtype == "say" and say == "tool":
            tool_data = _safe_json(raw_text)
            if isinstance(tool_data, dict):
                raw_tool = tool_data.get("tool") or tool_data.get("name") or "tool"
                canonical_tool = _TOOL_ALIASES.get(raw_tool, raw_tool)
                if pending_tool is not None and pending_tool.name == canonical_tool:
                    pending_tool.end_time = ts
                    if tool_data.get("success") is False or "error" in tool_data:
                        pending_tool.status_code = "ERROR"
                    if "content" in tool_data or "output" in tool_data:
                        pending_tool.attributes["output.value"] = str(tool_data.get("content") or tool_data.get("output") or "")[:_TRUNCATE]
                    existing_args = _safe_json(pending_tool.attributes.get("input.value", "{}"))
                    if isinstance(existing_args, dict):
                        new_args = _normalize_tool_args({k: v for k, v in tool_data.items() if k not in ("tool", "name", "content", "output")})
                        existing_args.update(new_args)
                        input_val = json.dumps(existing_args)
                        pending_tool.attributes["input.value"] = input_val[:_TRUNCATE]
                        pending_tool.attributes["code.edit_evidence"] = extract_edit_evidence(
                            canonical_tool, input_val, successful=pending_tool.status_code == "OK"
                        )
                    turn["tools"].append(pending_tool)
                    pending_tool = None
                    continue

                if pending_tool is not None:
                    turn["tools"].append(pending_tool)
                    pending_tool = None

                args = _normalize_tool_args({k: v for k, v in tool_data.items() if k not in ("tool", "name")})
                input_val = json.dumps(args)
                status_code = "ERROR" if (tool_data.get("success") is False or "error" in tool_data) else "OK"
                tool_idx = len(turn["tools"]) + 1
                span = SpanData(
                    span_id=f"{trace_id}:tool:{turn['num']}:{tool_idx}",
                    trace_id=trace_id,
                    parent_id=turn["span_id"],
                    name=canonical_tool,
                    span_kind="CHAIN",
                    status_code=status_code,
                    start_time=ts,
                    end_time=ts,
                    attributes={
                        "tool.name": canonical_tool,
                        "input.value": input_val[:_TRUNCATE],
                        "output.value": str(tool_data.get("content") or tool_data.get("output") or "")[:_TRUNCATE],
                        "cwd": "",
                        "code.edit_evidence": extract_edit_evidence(
                            canonical_tool, input_val, successful=status_code == "OK"
                        ),
                        **_source_attrs(r),
                    },
                )
                turn["tools"].append(span)
            continue

        # 6. Errors
        if (mtype == "say" and say == "error") or (mtype == "ask" and ask == "api_req_failed"):
            if pending_tool is not None:
                pending_tool.status_code = "ERROR"
                pending_tool.attributes["output.value"] = str(raw_text)[:_TRUNCATE]
                turn["tools"].append(pending_tool)
                pending_tool = None
            elif turn is not None:
                turn["output"].append(f"Error: {raw_text}")
            continue

        # 7. Assistant output (regular text response or followup question)
        if (mtype == "say" and say == "text") or (mtype == "ask" and ask == "followup"):
            if pending_tool is not None:
                turn["tools"].append(pending_tool)
                pending_tool = None
            if isinstance(raw_text, str) and raw_text:
                parsed_q = _safe_json(raw_text)
                q_text = (
                    parsed_q.get("question") or parsed_q.get("text") or raw_text
                    if isinstance(parsed_q, dict)
                    else raw_text
                )
                turn["output"].append(q_text)
            elif isinstance(raw_text, dict):
                q_text = str(raw_text.get("question") or raw_text.get("text") or "")
                if q_text:
                    turn["output"].append(q_text)
            continue

    _close_turn()

    for span in all_spans:
        if span.end_time > session_end:
            session_end = span.end_time

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
            "session.id": trace_id,
            "project.name": project_name or "",
            "llm.model_name": last_model,
            "harness.agent_session_id": trace_id.rsplit(":", 1)[-1],
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


def _parse_api_history(
    records: list[dict[str, Any]],
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse api_conversation_history.json format."""
    now = datetime.now(UTC)
    session_start = now
    session_end = now

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []
    turn: dict[str, Any] | None = None
    turn_num = 0
    pending_calls: dict[str, SpanData] = {}
    last_model = ""

    def _open_turn(prompt: str, src: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal turn_num
        turn_num += 1
        return {
            "span_id": f"{trace_id}:turn:{turn_num}",
            "num": turn_num,
            "prompt": prompt,
            "start": now,
            "end": now,
            "output": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read": 0,
            "cost_events": [],
            "tools": [],
            "model": last_model,
            "_src": src,
        }

    def _close_turn() -> None:
        if turn is not None:
            all_spans.append(_build_turn_span(trace_id, root_id, turn))
            all_spans.extend(turn["tools"])

    for rec in records:
        role = rec.get("role")
        content = rec.get("content")

        if role == "user":
            texts: list[str] = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type")
                    if btype == "text" and isinstance(b.get("text"), str):
                        texts.append(b["text"])
                    elif btype == "tool_result":
                        call_id = str(b.get("tool_use_id") or "")
                        res_content = str(b.get("content") or "")
                        is_err = bool(b.get("is_error"))
                        if not is_err and "command failed" in res_content.lower():
                            is_err = True
                        if call_id in pending_calls:
                            span = pending_calls.pop(call_id)
                            span.status_code = "ERROR" if is_err else "OK"
                            span.attributes["output.value"] = res_content[:_TRUNCATE]

            if texts:
                _close_turn()
                turn = _open_turn("\n".join(texts), rec)

        elif role == "assistant":
            if turn is None:
                turn = _open_turn("", rec)

            if isinstance(content, str):
                turn["output"].append(content)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type")
                    if btype == "text" and isinstance(b.get("text"), str):
                        turn["output"].append(b["text"])
                    elif btype == "tool_use":
                        call_id = str(b.get("id") or f"call_{len(turn['tools']) + 1}")
                        raw_tool = str(b.get("name") or "tool")
                        canonical = _TOOL_ALIASES.get(raw_tool, raw_tool)
                        raw_input = b.get("input") if isinstance(b.get("input"), dict) else {}
                        args = _normalize_tool_args(raw_input)
                        input_val = json.dumps(args)
                        tool_idx = len(turn["tools"]) + 1
                        span = SpanData(
                            span_id=f"{trace_id}:tool:{turn['num']}:{tool_idx}",
                            trace_id=trace_id,
                            parent_id=turn["span_id"],
                            name=canonical,
                            span_kind="CHAIN",
                            status_code="OK",
                            start_time=now,
                            end_time=now,
                            attributes={
                                "tool.name": canonical,
                                "input.value": input_val[:_TRUNCATE],
                                "output.value": "",
                                "cwd": "",
                                "code.edit_evidence": extract_edit_evidence(
                                    canonical, input_val, successful=True
                                ),
                                **_source_attrs(rec),
                            },
                        )
                        turn["tools"].append(span)
                        pending_calls[call_id] = span

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
            "session.id": trace_id,
            "project.name": project_name or "",
            "llm.model_name": last_model,
            "harness.agent_session_id": trace_id.rsplit(":", 1)[-1],
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


def _build_turn_span(trace_id: str, root_id: str, turn: dict[str, Any]) -> SpanData:
    p_tokens = turn["prompt_tokens"]
    c_tokens = turn["completion_tokens"]
    cr_tokens = turn["cache_read"]
    return SpanData(
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
            "cwd": "",
            "llm.token_count.prompt": p_tokens,
            "llm.token_count.completion": c_tokens,
            "llm.token_count.reasoning": 0,
            "llm.token_count.cache_read": cr_tokens,
            "llm.token_count.cache_write": 0,
            "llm.token_count.input_fresh": max(0, p_tokens - cr_tokens),
            "llm.cost_events": turn["cost_events"],
            "llm.model_name": turn["model"] or "",
            **_source_attrs(turn.get("_src")),
        },
    )
