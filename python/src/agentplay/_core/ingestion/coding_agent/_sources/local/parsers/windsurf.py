"""Parse Windsurf (Cascade agent) session transcripts into the universal SpanData shape.

Windsurf/Cascade persists session transcripts either as trajectory step logs (NDJSON /
JSONL) or as exported session documents. Each step or event carries trajectory metadata:

    {"type": "windsurf_session", "agentId": ..., "createdAt": ..., "cascade_id": ...,
     "model": ..., "cwd": ...}
    {"step_id": 1, "step_type": 19, "timestamp": "...", "user_input": "..."}
    {"step_id": 2, "step_type": 20, "timestamp": "...", "visible": "...", "thinking": "...",
     "provider": "anthropic"}
    {"step_id": 3, "step_type": ..., "timestamp": "...", "tool_calls": [
        {"tool_id": "call-1", "tool_name": "read_file", "params": {"file_path": "foo.py"}}
    ]}
    {"step_id": 4, "step_type": 21, "timestamp": "...", "tool_id": "call-1",
     "tool_result": "...", "is_error": false}

Tools are mapped to canonical names via `tool_taxonomy.py` (e.g., `read_file` -> `Read`,
`run_command` -> `Bash`, `edit`/`propose_code` -> `Edit`, `write_to_file` -> `Write`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._paths import basename as path_basename
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    parse_jsonl_records,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent.produced import (
    creation_command_attributes,
    creation_output_attributes,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# input.value / output.value truncation, matching other local parsers.
_TRUNCATE = 2000

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("windsurf")

# Default synthetic clock step when timestamps are missing.
_STEP = timedelta(seconds=1)


def _parse_timestamp(val: Any) -> datetime | None:
    """Parse an ISO timestamp string or unix timestamp (seconds or milliseconds)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # If timestamp is in milliseconds (> 1e11), convert to seconds
        ts_sec = val / 1000.0 if val > 1e11 else float(val)
        try:
            return datetime.fromtimestamp(ts_sec, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        # Try fromisoformat (handles standard ISO-8601 with timezone offsets or 'Z')
        iso_str = val.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass
    return None


def _normalize_file_path_arg(args: dict) -> dict:
    """Alias Windsurf's path arg keys (`path`, `filePath`, etc.) to `file_path`.

    Matches the behavior of cursor_store / kiro / opencode. Only fills
    `file_path` when absent; never removes original keys.
    """
    if "file_path" in args:
        return args
    for candidate in (
        "path",
        "filePath",
        "target_file",
        "targetFile",
        "file_name",
        "fileName",
        "uri",
        "relative_workspace_path",
        "absolute_path",
    ):
        path = args.get(candidate)
        if isinstance(path, str) and path:
            return {**args, "file_path": path}
    return args


def _extract_steps(data: bytes) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Extract header and steps from data (supports NDJSON lines or a single JSON document)."""
    # First, test if this is a single JSON document with a "steps" array.
    try:
        text = data.decode("utf-8").strip()
        if text.startswith("{") and text.endswith("}"):
            doc = json.loads(text)
            if isinstance(doc, dict):
                steps = doc.get("steps")
                if isinstance(steps, list):
                    header = {k: v for k, v in doc.items() if k != "steps"}
                    return header, [s for s in steps if isinstance(s, dict)]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    # Otherwise parse as JSONL / NDJSON lines.
    records = parse_jsonl_records(data)
    header: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = []

    for rec in records:
        rtype = rec.get("type")
        if rtype in ("windsurf_session", "windsurf_trajectory"):
            if header is None:
                header = rec
            continue
        # Also treat session-level metadata object as header
        if "cascade_id" in rec and "step_id" not in rec and "tool_calls" not in rec:
            if header is None:
                header = rec
            continue
        steps.append(rec)

    return header, steps


def parse_windsurf(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a Windsurf / Cascade transcript into a span tree.

    Args:
        data: raw JSONL or JSON bytes.
        trace_id: the source-prefixed session id (e.g. `local:windsurf:<session-id>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by LocalSource; derived from cwd if not given.

    Returns:
        `(trace, all_spans)` with the trace's `spans` matching the list.
    """
    header, steps = _extract_steps(data)
    header = header or {}

    now = datetime.now(UTC)
    if not steps and not header:
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    cwd = header.get("cwd") or header.get("workspace_path")
    if not project_name and cwd and isinstance(cwd, str):
        project_name = path_basename(cwd) or None

    # Determine session time boundaries
    first_step_time: datetime | None = None
    last_step_time: datetime | None = None

    for s in steps:
        ts = (
            _parse_timestamp(s.get("timestamp"))
            or _parse_timestamp(s.get("timestamp_unix_ms"))
            or _parse_timestamp(s.get("time"))
        )
        if ts is not None:
            if first_step_time is None or ts < first_step_time:
                first_step_time = ts
            if last_step_time is None or ts > last_step_time:
                last_step_time = ts

    header_start = _parse_timestamp(header.get("createdAt"))
    header_end = _parse_timestamp(header.get("updatedAt")) or _parse_timestamp(
        header.get("lastModifiedAt")
    )

    session_start = header_start or first_step_time or now
    session_end = header_end or last_step_time or session_start
    if session_end < session_start:
        session_end = session_start

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []

    turn_num = 0
    tool_span_count = 0
    current_turn: SpanData | None = None
    turn_output_parts: list[str] = []
    turn_model: str = header.get("model") or ""

    # Synthetic timestamp pointer in case steps have no native timestamps
    clock = session_start

    # Track pending tool results if results arrive out of order or separate:
    # call_id -> tool_span
    active_tool_spans: dict[str, SpanData] = {}

    def _close_turn() -> None:
        nonlocal current_turn, turn_output_parts, turn_model
        if current_turn is not None:
            current_turn.attributes["output.value"] = "\n".join(turn_output_parts)[:_TRUNCATE]
            if turn_model:
                current_turn.attributes["llm.model_name"] = turn_model
            current_turn.end_time = max(current_turn.end_time, clock)
        current_turn = None
        turn_output_parts = []

    def _open_turn(ts: datetime, input_value: str = "", src: dict | None = None) -> SpanData:
        nonlocal turn_num, current_turn, clock
        _close_turn()
        turn_num += 1
        clock = max(clock, ts)
        turn = SpanData(
            span_id=f"{trace_id}:turn:{turn_num}",
            trace_id=trace_id,
            parent_id=root_id,
            name=f"Turn {turn_num}",
            span_kind="CHAIN",
            status_code="OK",
            start_time=ts,
            end_time=ts,
            attributes={
                "input.value": input_value[:_TRUNCATE],
                **creation_command_attributes(input_value),
                "output.value": "",
                "cwd": cwd,
                "llm.model_name": turn_model,
            },
        )
        if src:
            turn.attributes.update(source_attrs(src))
        all_spans.append(turn)
        current_turn = turn
        return turn

    def _ensure_turn(ts: datetime, src: dict | None = None) -> SpanData:
        if current_turn is None:
            return _open_turn(ts, input_value="", src=src)
        return current_turn

    for step in steps:
        step_id = step.get("step_id")
        step_type = step.get("step_type")
        raw_role = step.get("role") or step.get("type")

        # Step timestamp or synthetic increment
        step_ts = (
            _parse_timestamp(step.get("timestamp"))
            or _parse_timestamp(step.get("timestamp_unix_ms"))
            or _parse_timestamp(step.get("time"))
        )
        if step_ts is None:
            clock += _STEP
            step_ts = clock
        else:
            clock = max(clock, step_ts)

        # Detect model provider info
        step_provider = step.get("provider") or step.get("model")
        if isinstance(step_provider, str) and step_provider:
            turn_model = step_provider

        # 1. User message / turn start
        is_user = (
            raw_role == "user"
            or step_type == 19  # user_input step in CortexTrajectoryStep
            or "user_input" in step
            or "user_message" in step
        )

        if is_user:
            user_msg = (
                step.get("user_input")
                or step.get("user_message")
                or step.get("content")
                or step.get("content_preview")
                or ""
            )
            if not isinstance(user_msg, str):
                user_msg = str(user_msg)
            _open_turn(step_ts, input_value=user_msg, src=step)
            continue

        # 2. Assistant message / response
        visible_text = step.get("visible") or step.get("content") or ""
        if isinstance(visible_text, str) and visible_text:
            _ensure_turn(step_ts, src=step)
            turn_output_parts.append(visible_text)
            if current_turn is not None:
                current_turn.end_time = max(current_turn.end_time, step_ts)

        # 3. Tool call execution results
        is_tool_result = (
            step_type == 21
            or "tool_result" in step
            or "result" in step
            or raw_role == "tool"
        )
        if is_tool_result:
            call_id = step.get("tool_id") or step.get("toolCallId") or step.get("id")
            result_val = step.get("tool_result") or step.get("result") or step.get("output") or ""
            is_err = bool(step.get("is_error") or step.get("error") or step.get("exit_code", 0) != 0)

            if call_id and str(call_id) in active_tool_spans:
                tool_span = active_tool_spans[str(call_id)]
                out_str = result_val if isinstance(result_val, str) else json.dumps(result_val)
                tool_span.attributes["output.value"] = out_str[:_TRUNCATE]
                tool_span.attributes.update(
                    creation_output_attributes(
                        out_str,
                        tool_span.attributes.get("pr.create.provider"),
                        tool_span.attributes.get("pr.create.repository"),
                    )
                )
                if is_err:
                    tool_span.status_code = "ERROR"
                tool_span.attributes["code.edit_evidence"] = extract_edit_evidence(
                    tool_span.name,
                    tool_span.attributes.get("input.value", "{}"),
                    successful=(tool_span.status_code != "ERROR"),
                )
                tool_span.end_time = max(tool_span.end_time, step_ts)
                if current_turn is not None:
                    current_turn.end_time = max(current_turn.end_time, step_ts)

        # 4. Tool calls
        raw_tool_calls = step.get("tool_calls")
        tool_call_list: list[dict[str, Any]] = []
        if isinstance(raw_tool_calls, list):
            for tc in raw_tool_calls:
                if isinstance(tc, dict):
                    tool_call_list.append(tc)
        elif "tool_name" in step or "toolName" in step:
            tool_call_list.append(step)

        for tc in tool_call_list:
            tool_span_count += 1
            call_id = tc.get("tool_id") or tc.get("id") or f"call_{step_id or tool_span_count}"
            raw_tool_name = tc.get("tool_name") or tc.get("name") or tc.get("tool") or "unknown"
            canonical_tool_name = _TOOL_ALIASES.get(raw_tool_name, raw_tool_name)

            params = tc.get("params") or tc.get("args") or tc.get("parameters") or {}
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except (json.JSONDecodeError, TypeError):
                    params = {"raw": params}
            elif not isinstance(params, dict):
                params = {"value": params}

            norm_params = _normalize_file_path_arg(params)
            input_val = json.dumps(norm_params, separators=(",", ":"))

            turn = _ensure_turn(step_ts, src=step)

            # Check if this tool call already contains its result or error flag
            tc_result = tc.get("result") or tc.get("output")
            tc_err = bool(tc.get("is_error") or tc.get("error") or tc.get("exit_code", 0) != 0)
            status_code = "ERROR" if tc_err else "OK"
            out_val = ""
            if tc_result is not None:
                out_val = tc_result if isinstance(tc_result, str) else json.dumps(tc_result)

            cmd_attrs = creation_command_attributes(input_val)
            out_attrs = creation_output_attributes(
                out_val,
                cmd_attrs.get("pr.create.provider"),
                cmd_attrs.get("pr.create.repository"),
            )

            has_result = tc_result is not None or "output" in tc or "result" in tc
            tool_span = SpanData(
                span_id=f"{trace_id}:tool:{tool_span_count}",
                trace_id=trace_id,
                parent_id=turn.span_id,
                name=canonical_tool_name,
                span_kind="INTERNAL",
                status_code=status_code,
                start_time=step_ts,
                end_time=step_ts,
                attributes={
                    "tool.name": canonical_tool_name,
                    "input.value": input_val[:_TRUNCATE],
                    "output.value": out_val[:_TRUNCATE],
                    "code.edit_evidence": extract_edit_evidence(
                        canonical_tool_name,
                        norm_params,
                        successful=has_result and not tc_err,
                    ),
                    **cmd_attrs,
                    **out_attrs,
                },
            )
            tool_span.attributes.update(source_attrs(step))

            all_spans.append(tool_span)
            turn.end_time = max(turn.end_time, step_ts)
            active_tool_spans[str(call_id)] = tool_span

    # Finalize any open turn
    _close_turn()

    # Determine overall status and timestamps
    overall_status = "OK"
    if any(s.status_code == "ERROR" for s in all_spans):
        overall_status = "ERROR"

    if all_spans:
        session_start = min(s.start_time for s in all_spans)
        session_end = max(s.end_time for s in all_spans)

    root_span = SpanData(
        span_id=root_id,
        trace_id=trace_id,
        parent_id=None,
        name="Session (windsurf)",
        span_kind="CHAIN",
        status_code=overall_status,
        start_time=session_start,
        end_time=session_end,
        attributes={
            "session.id": trace_id,
            "user.id": user_id,
            "project.name": project_name,
            "source.surface": "windsurf",
            "agent.name": "windsurf",
            "cwd": cwd,
            "llm.model_name": turn_model,
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
