"""Parse Antigravity CLI (`agy`) step logs into the universal SpanData shape.

`agy` keeps each conversation under
``~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/`` and writes
one JSON line per step to ``transcript_full.jsonl``. (``transcript.jsonl`` holds
the same steps with every tool arg stringified, so it is not read.) The shape
below was read from real `agy` 1.4.6 sessions:

    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
     "status": "DONE", "created_at": <ISO>, "content": <wrapped prompt>}
    {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", ...,
     "input_tokens", "cache_read_tokens", "output_tokens",
     "thinking"?, "content"?, "tool_calls"?: [{"name", "args"}]}
    {"step_index": 2, "source": "MODEL", "type": "GENERIC",
     "status": "DONE" | "ERROR", "error"?: <text>, "content": <result text>}
    {"step_index": n, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", ...}

# Turn boundaries

A `USER_INPUT` opens a turn. Its `content` wraps the prompt in
``<USER_REQUEST>`` and appends ``<ADDITIONAL_METADATA>`` (and sometimes
``<USER_SETTINGS_CHANGE>``) blocks the CLI writes; only the request is the
prompt. A subagent's conversation opens with a `SYSTEM_MESSAGE` (the delegated
task) instead, which opens a turn with no prompt. Later `SYSTEM_MESSAGE` steps
(a subagent reporting back, context after a resumed prompt) stay in the turn.

# Pairing calls with results

Tool calls carry no id. A planner step lists its calls in `tool_calls` (several
when the model runs them in parallel), and the next `GENERIC` steps are their
results, in the same order. A new planner step means the previous step's
remaining calls never got a result: they are closed as UNSET.

# Tool failures

A failed edit and a denied permission are `GENERIC` steps with
`status: "ERROR"` and an `error` text. A command that exits non-zero is still
`DONE`; its result opens with a ``Command exited with code N.`` sentence (``The
command exited ...`` in subagents), which is the failure signal. An ``Error:``
line in the result is the program's own output, not a CLI label.

# Tokens

Per planner step: `input_tokens`, `cache_read_tokens`, `output_tokens`. No
thinking count. Every captured session reported 0 cache reads, so whether input
includes them is unconfirmed; this follows the Gemini CLI convention (cache
reads are a subset of input).

# Model

The model is not in the step log, only in the conversation's protobuf
`gen_metadata`, so it is left blank. TODO: read it once a stdlib-only decode of
that blob is worth carrying.
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
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# input.value / output.value truncation, matching every other local parser.
_TRUNCATE = 2000

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("antigravity-cli")

_STEP_TYPES = ("USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "SYSTEM_MESSAGE")

# The only args that reach `input.value`, renamed to the canonical keys the map
# builder reads. Everything else (`toolSummary`, `toolAction`, a subagent's
# `Prompt`, `send_message`'s `Message`, line ranges, async flags, file text)
# is dropped, and so is every arg of a tool not listed here.
#
# The aliased names below were not used in a captured session. Their keys come
# from the jsonschema struct tags in the agy 1.4.6 binary. Only path and
# pattern keys are kept: `CodeContent`, `TargetContent`, `ReplacementContent`,
# `Instruction` and `explanation` are file or prompt text. `search_web`'s
# `Query` is a web search phrase, not a file pattern, so that tool stays empty.
_ARG_KEYS: dict[str, dict[str, str]] = {
    "read_file": {"Path": "file_path"},
    "view_file": {"AbsolutePath": "file_path"},
    "write_file": {"Path": "file_path", "Content": "content"},
    "write_to_file": {"TargetFile": "file_path"},
    "edit_file": {"Path": "file_path", "Target": "old_string", "Replacement": "new_string"},
    "replace_file_content": {"TargetFile": "file_path"},
    "multi_replace_file_content": {"TargetFile": "file_path"},
    "grep_search": {"SearchPath": "file_path", "Query": "pattern", "Includes": "glob"},
    "find_by_name": {"SearchDirectory": "file_path", "Pattern": "pattern", "Excludes": "glob"},
    "list_dir": {"DirectoryPath": "file_path"},
    "read_url_content": {"Url": "url"},
    "run_command": {"CommandLine": "command", "Cwd": "workdir"},
    "invoke_subagent": {"TypeName": "subagent_type", "Model": "model"},
}

_SHELL_TOOL = "run_command"

_RE_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_RE_EXIT_CODE = re.compile(r"^(?:The c|C)ommand exited with code (-?\d+)\.?\s*$")
# Result labels after which the program's own output starts.
_OUTPUT_LABELS = ("Command output:", "Output:", "Stdout:", "Stderr:")
_RE_CREATED = re.compile(r"^Created At: (\S+)\s*$")
_RE_COMPLETED = re.compile(r"^Completed At: (\S+)\s*$")


def parse_antigravity(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse an Antigravity CLI `transcript_full.jsonl` into a span tree.

    Args:
        data: raw JSONL bytes of one conversation's `transcript_full.jsonl`.
        trace_id: the source-prefixed session id (`local:antigravity-cli:<uuid>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by `LocalSource` from the
            conversation's workspace in `conversation_summaries.db`. The step
            log records no working directory.

    Returns:
        `(trace, all_spans)`; empty spans when the file holds no steps.
    """
    steps = _steps(parse_jsonl_records(data))
    if not steps:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    times = [t for t in (safe_parse_dt(s.get("created_at")) for s in steps) if t is not None]
    session_start = min(times) if times else datetime.now(UTC)
    session_end = max(times) if times else session_start

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []
    turn: dict | None = None
    turn_num = 0
    # Calls of the latest planner step still waiting for their `GENERIC` result.
    pending: list[tuple[dict, dict, dict, int]] = []

    def _open_turn(step: dict, prompt: str) -> dict:
        nonlocal turn_num
        turn_num += 1
        ts = safe_parse_dt(step.get("created_at")) or session_start
        return {
            "span_id": f"{trace_id}:turn:{turn_num}",
            "num": turn_num,
            "_src": step,
            "prompt": prompt,
            "start": ts,
            "end": ts,
            "output": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read": 0,
            "cost_events": [],
            "tools": [],
        }

    def _add_tool(owner: dict, span: SpanData) -> None:
        owner["tools"].append(span)
        # A tool can finish after its turn's last step; the turn ends no earlier.
        if span.end_time > owner["end"]:
            owner["end"] = span.end_time

    def _flush_pending() -> None:
        for owner, planner, call, index in pending:
            _add_tool(owner, _tool_span(trace_id, owner, planner, call, index, None))
        pending.clear()

    def _close_turn() -> None:
        _flush_pending()
        if turn is not None:
            all_spans.append(_turn_span(trace_id, root_id, turn))
            all_spans.extend(turn["tools"])

    for step in steps:
        stype = step.get("type")
        ts = safe_parse_dt(step.get("created_at"))

        if stype == "USER_INPUT":
            _close_turn()
            turn = _open_turn(step, _user_prompt(step.get("content")))
            continue
        if stype == "SYSTEM_MESSAGE":
            if turn is None:
                # A subagent's delegated task: a turn, but not a user prompt.
                turn = _open_turn(step, "")
        elif stype == "PLANNER_RESPONSE":
            if turn is None:
                turn = _open_turn(step, "")
            _flush_pending()
            text = step.get("content")
            if isinstance(text, str) and text:
                turn["output"].append(text)
            _add_usage(turn, step)
            pending.extend((turn, step, call, i) for i, call in enumerate(_tool_calls(step)))
        elif stype == "GENERIC" and pending:
            owner, planner, call, index = pending.pop(0)
            _add_tool(owner, _tool_span(trace_id, owner, planner, call, index, step))

        if turn is not None and ts is not None and ts > turn["end"]:
            turn["end"] = ts
    _close_turn()
    # A tool can finish after the step that recorded it. `Completed At` is
    # that end, and it can be later than every step's created_at.
    for span in all_spans:
        if "tool.name" in span.attributes and span.end_time > session_end:
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
            # Not in the step log (see the module docstring).
            "llm.model_name": "",
            "session.id": trace_id,
            "project.name": project_name or "",
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


def is_antigravity_step(obj: dict) -> bool:
    """True for an agy step-log line: an int `step_index`, a `source` string and a step `type`.

    No other agent writes `step_index`, and these upper-case types are agy's own.
    """
    index = obj.get("step_index")
    return (
        obj.get("type") in _STEP_TYPES
        and isinstance(index, int)
        and not isinstance(index, bool)
        and isinstance(obj.get("source"), str)
    )


def _steps(records: list[dict]) -> list[dict]:
    """Known steps ordered by `step_index`, keeping the last copy of each index."""
    latest: dict[int, dict] = {}
    for record in records:
        index = record.get("step_index")
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and record.get("type") in _STEP_TYPES
        ):
            latest[index] = record
    return [latest[i] for i in sorted(latest)]


def _tool_calls(step: dict) -> list[dict]:
    calls = step.get("tool_calls")
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _user_prompt(content: object) -> str:
    """The prompt inside the CLI's ``<USER_REQUEST>`` wrapper."""
    if not isinstance(content, str):
        return ""
    match = _RE_USER_REQUEST.search(content)
    return match.group(1) if match else content.strip()


def _add_usage(turn: dict, step: dict) -> None:
    """Fold one planner step's token counts into its turn."""
    prompt = as_count(step.get("input_tokens"))
    cache_read = min(as_count(step.get("cache_read_tokens")), prompt)
    completion = as_count(step.get("output_tokens"))
    # A planner step that reports zero tokens is still a request. Dropping it
    # undercounts model-usage summaries.
    turn["prompt_tokens"] += prompt
    turn["completion_tokens"] += completion
    turn["cache_read"] += cache_read
    created = step.get("created_at")
    turn["cost_events"].append(
        {
            "timestamp": created if isinstance(created, str) else "",
            "request_id": None,
            "message_id": f"antigravity:{step.get('step_index')}",
            "model": "",
            "input_tokens": prompt - cache_read,
            "cache_read_tokens": cache_read,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "output_tokens": completion,
            "reasoning_tokens": 0,
            "usage_basis": "message_tokens",
            **source_attrs(step),
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
            # The step log records no working directory; LocalSource stamps
            # the workspace it read from `conversation_summaries.db`.
            "cwd": "",
            "llm.token_count.prompt": prompt,
            "llm.token_count.completion": turn["completion_tokens"],
            "llm.token_count.reasoning": 0,
            "llm.token_count.cache_read": cache_read,
            "llm.token_count.cache_write": 0,
            "llm.token_count.input_fresh": max(0, prompt - cache_read),
            "llm.cost_events": turn["cost_events"],
            "llm.model_name": "",
        },
    )
    span.attributes.update(source_attrs(turn["_src"]))
    return span


def _tool_span(
    trace_id: str,
    turn: dict,
    planner: dict,
    call: dict,
    index: int,
    result: dict | None,
) -> SpanData:
    raw_name = call.get("name")
    raw_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
    tool_name = _TOOL_ALIASES.get(raw_name, raw_name)
    input_value = json.dumps(_normalized_args(raw_name, call.get("args")))

    content = result.get("content") if result is not None else None
    content = content if isinstance(content, str) else ""
    if result is None:
        status_code = "UNSET"
    elif result.get("status") == "ERROR" or result.get("error"):
        status_code = "ERROR"
    elif raw_name == _SHELL_TOOL and _exit_code(content) not in (None, 0):
        status_code = "ERROR"
    else:
        status_code = "OK"
    # An error result repeats the `error` text, which can quote the command or
    # the edit target; it is never kept.
    output_value = content if status_code == "OK" else ""

    start = _labelled_time(content, _RE_CREATED) or safe_parse_dt(planner.get("created_at"))
    start = start or turn["start"]
    end = _labelled_time(content, _RE_COMPLETED)
    if end is None and result is not None:
        end = safe_parse_dt(result.get("created_at"))
    if end is None or end < start:
        end = start

    span = SpanData(
        span_id=f"{trace_id}:tool:{planner.get('step_index')}:{index}",
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
    span.attributes.update(source_attrs(result if result is not None else planner))
    return span


def _normalized_args(raw_name: str, args: object) -> dict[str, str]:
    """The whitelisted string args of one call, under canonical key names."""
    keys = _ARG_KEYS.get(raw_name, {})
    if not isinstance(args, dict):
        return {}
    return {new: args[old] for old, new in keys.items() if isinstance(args.get(old), str)}


def _exit_code(content: str) -> int | None:
    """The CLI's exit-code sentence, read only before the program's output starts."""
    for line in content.splitlines():
        if line.strip() in _OUTPUT_LABELS:
            return None
        match = _RE_EXIT_CODE.match(line)
        if match:
            return int(match.group(1))
    return None


def _labelled_time(content: str, pattern: re.Pattern[str]) -> datetime | None:
    """A ``Created At:`` / ``Completed At:`` timestamp from the result's header lines."""
    for line in content.splitlines()[:2]:
        match = pattern.match(line)
        if match:
            return safe_parse_dt(match.group(1))
    return None


__all__ = ["is_antigravity_step", "parse_antigravity"]
