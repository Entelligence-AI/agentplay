"""Parse OpenAI Codex JSONL sessions into the universal SpanData shape.

Codex's format differs from Claude Code's: lines are typed `session_meta`,
`response_item`, `event_msg`, and turns are bracketed by user
`response_item` messages with `input_text` content blocks. Token counts
arrive as native `token_usage_record` records or legacy `event_msg` lines
(`type=token_count`). Native response IDs survive display-counter resets.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._paths import basename as path_basename
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    SOURCE_OFFSET_KEY,
    drop_untyped,
    parse_dt,
    parse_jsonl_records,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent.edit_lines import count_patch_lines
from agentplay._core.ingestion.coding_agent.native_operations import (
    ProcessEvidence,
    literal_operations,
    literal_results,
    shell_result,
)
from agentplay._core.ingestion.coding_agent.produced import (
    creation_command_attributes,
    creation_output_attributes,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData, model_id_or_none
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("codex")

# Codex tool calls arrive as two `response_item` payload types: regular
# `function_call` (JSON `arguments`) and `custom_tool_call` (freeform
# `input` — this is how `apply_patch` ships its patch text). Both pair with
# a `*_output` line via `call_id`, so both types must be matched or
# apply_patch spans are lost.
_TOOL_CALL_TYPES = ("function_call", "custom_tool_call")
_TOOL_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")

# First `Process exited with code N` line in an exec-style output — the
# unified-exec header Codex prepends before the command's own `Output:`
# section. Full-line MULTILINE match so a stray substring inside the
# command's stdout can't trigger it, and `search` (first match) so the
# header verdict wins even when the output body echoes a similar line.
_RE_EXEC_EXIT_CODE = re.compile(r"^Process exited with code (\d+)$", re.MULTILINE)

# File headers inside an `apply_patch` body. The codex patch format brackets
# hunks with `*** Begin Patch` / `*** End Patch` and names each touched file
# on a `*** <Verb> File: <path>` header; renames add a `*** Move to: <dest>`
# line under the Update header. `Update File` and `Add File` are the forms
# seen in transcripts; `Delete File` / `Move to` are documented parts of the
# same grammar and matched for completeness. Full-line MULTILINE match so
# patch BODY lines that merely quote a header (e.g. an added doc line
# `+*** Update File: x`) can't inject phantom paths.
_RE_PATCH_FILE_HEADER = re.compile(
    r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to): (.+?)\s*$", re.MULTILINE
)


def _apply_patch_input(patch_text: str) -> str:
    """Rewrite an apply_patch body into a `file_path`-keyed input string.

    Files-touched consumers (`file_paths.extract_file_path`, ...) regex-scan a tool span's
    `input.value` for `"file_path": "..."` pairs — they never see the
    patch grammar, so a raw apply_patch body yields zero files touched
    for every Codex session. This prefixes one `"file_path"` pair per
    file named in the patch headers, then appends the original patch
    text under `"patch"`. Paths go FIRST so they survive the parser's
    2000-char `input.value` truncation even for long patches (same
    lesson as opencode's `_normalize_file_path_arg`: the key must be in
    the retained prefix, not the truncated tail).

    Multi-file patches produce REPEATED `"file_path"` keys — legal JSON
    text, and the regex consumers match every occurrence; a `json.loads`
    consumer would keep only the last path, which no current consumer
    does (all are regex-based, per `file_paths.py`'s module contract).

    Args:
        patch_text: The raw `custom_tool_call.input` payload — an
            `*** Begin Patch` body, untruncated.

    Also stamps `lines_added` / `lines_removed`, computed from the untruncated
    body — see the comment at the append below for why it has to happen here.

    Returns:
        `{"lines_added": <n>, "lines_removed": <n>, "file_path": "<p1>"[,
        "file_path": "<p2>"...], "patch": "<body>"}` as a string, or `patch_text`
        unchanged when no file header is found (nothing to extract — keep the
        raw evidence).
    """
    paths = list(dict.fromkeys(_RE_PATCH_FILE_HEADER.findall(patch_text)))
    if not paths:
        return patch_text
    # AI lines of code, counted HERE because this is the last place the
    # untruncated body exists: `input.value` is capped at 2,000 characters just
    # below, which cuts most apply_patch spans
    # mid-string. `edit_lines.edit_line_counts` reads the stamp by regex, so a
    # truncated tail does not cost the session its counts.
    #
    # FIRST, ahead of the paths. The path list is unbounded — a multi-file patch
    # emits one pair per file — so a patch touching enough files, or files with
    # long enough paths, would push a trailing stamp past the cap and report
    # no counts for a session that was measured perfectly well. These two integers
    # cost ~45 characters of a 2,000-character budget; the paths can afford it,
    # and file-path extraction is regex-based (`file_paths.py`) so a partially
    # truncated path list still yields every path it retained.
    added, removed = count_patch_lines(patch_text)
    pairs = [f'"lines_added": {added}, "lines_removed": {removed}']
    pairs.extend(f'"file_path": {json.dumps(p)}' for p in paths)
    pairs.append(f'"patch": {json.dumps(patch_text)}')
    return "{" + ", ".join(pairs) + "}"


def parse_codex(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a Codex JSONL file into a span tree."""
    lines = _own_records(_parse_jsonl(data), data)
    if not lines:
        now = datetime.now(UTC)
        trace = TraceData(trace_id=trace_id, start_time=now, end_time=now)
        return trace, []

    # Session-level metadata. cwd, the native session id, and the CLI
    # version come from session_meta; the model name lives on
    # `turn_context` (session_meta only carries model_provider) —
    # reading only session_meta would leave `model_name` empty. Take the
    # first of each we see.
    model_name = ""
    cwd = ""
    agent_session_id = ""
    agent_version = ""
    for line in lines:
        payload = line.get("payload", {})
        ltype = line.get("type")
        if ltype == "session_meta":
            if not cwd:
                cwd = payload.get("cwd", "")
            if not agent_session_id:
                agent_session_id = payload.get("id", "") or ""
            if not agent_version:
                agent_version = payload.get("cli_version", "") or ""
        elif ltype == "turn_context" and not model_name:
            model_name = model_id_or_none(payload.get("model")) or ""
        if cwd and model_name and agent_session_id and agent_version:
            break

    if not project_name and cwd:
        project_name = path_basename(cwd)

    turns, native_ledger_rejected = _group_into_turns(lines)

    all_spans: list[SpanData] = []
    root_id = f"{trace_id}:root"

    all_timestamps = [parse_dt(line["timestamp"]) for line in lines if line.get("timestamp")]
    if all_timestamps:
        session_start = min(all_timestamps)
        session_end = max(all_timestamps)
    else:
        session_start = datetime.now(UTC)
        session_end = session_start

    for turn_num, turn in enumerate(turns, 1):
        turn_id = f"{trace_id}:turn:{turn_num}"

        turn_start = parse_dt(turn["start_ts"]) if turn.get("start_ts") else session_start
        turn_end = parse_dt(turn["end_ts"]) if turn.get("end_ts") else turn_start

        total_prompt = turn.get("input_tokens", 0)
        total_completion = turn.get("output_tokens", 0)
        total_reasoning = turn.get("reasoning_tokens", 0)
        cache_read = turn.get("cache_read", 0)

        # Match the attribute shape Claude parsers use so consumers
        # read every agent consistently. Codex doesn't expose
        # cache_write - leave it zero.
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
                # Session metadata is a fallback; individual calls retain
                # the working directory observed in their turn_context.
                "cwd": cwd or "",
                "llm.token_count.prompt": total_prompt,
                "llm.token_count.completion": total_completion,
                # Subset of total_completion, not additive -- see
                # `_reasoning_tokens`. Display-only; no consumer bills off it.
                "llm.token_count.reasoning": total_reasoning,
                "llm.token_count.cache_read": cache_read,
                "llm.token_count.cache_write": 0,
                "llm.token_count.input_fresh": max(0, total_prompt - cache_read),
                "llm.cost_events": turn["cost_events"],
                # Per-turn model — Codex can switch models mid-session; fall
                # back to the session model when a turn carries none.
                "llm.model_name": turn.get("model") or model_name,
            },
        )
        turn_span.attributes.update(source_attrs(turn.get("_src")))
        all_spans.append(turn_span)

        for tool in turn.get("tools", []):
            tool_start = parse_dt(tool["start_ts"]) if tool.get("start_ts") else turn_start
            tool_end = parse_dt(tool["end_ts"]) if tool.get("end_ts") else tool_start

            # Canonical name on both span name and `tool.name` — matching
            # cursor_store.py, which also doesn't keep the raw name; the
            # the tool distribution reads `tool.name`, so it shows the same
            # canonical vocabulary for every agent.
            tool_name = _TOOL_ALIASES.get(tool["name"], tool["name"])
            tool_span = SpanData(
                span_id=f"{trace_id}:tool:{tool['call_id']}",
                trace_id=trace_id,
                parent_id=turn_id,
                name=tool_name,
                span_kind="CHAIN",
                # `is_error` is decided from the untruncated output payload
                # when the matching `*_output` line lands (see
                # `_tool_output_is_error`); dangling calls carry no verdict
                # and remain UNSET.
                status_code=(
                    "UNSET"
                    if "is_error" not in tool or tool.get("provenance.capture") == "pending_process"
                    else "ERROR"
                    if tool.get("is_error")
                    else "OK"
                ),
                start_time=tool_start,
                end_time=tool_end,
                attributes={
                    "tool.name": tool_name,
                    "cwd": tool.get("cwd", ""),
                    "input.value": tool.get("input", "")[:2000],
                    **{
                        key: tool[key]
                        for key in (
                            "pr.create.provider",
                            "pr.create.repository",
                            "pr.create.requested_repository",
                            "pr.create.command_sha256",
                            "pr.create.output_url",
                            "pr.create.capture",
                            "provenance.capture",
                        )
                        if key in tool
                    },
                    "code.edit_evidence": (
                        tool.get("edit_evidence", {"status": "unsupported_tool", "edits": []})
                        if "is_error" in tool and not tool["is_error"]
                        else {"status": "not_successful", "edits": []}
                    ),
                    "output.value": tool.get("output", "")[:2000],
                },
            )
            tool_span.attributes.update(source_attrs(tool.get("_src")))
            all_spans.append(tool_span)

    # Session-level model: the last model actually used in a turn (handles
    # mid-session switches), falling back to the first turn_context model.
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
            "harness.agent_version": agent_version,
        },
    )
    root_span.attributes.update(_thread_attrs(lines))
    compactions, interruptions = _session_signals(lines)
    if compactions:
        root_span.attributes["harness.compactions"] = compactions
    if interruptions:
        root_span.attributes["harness.interruptions"] = interruptions
    all_spans.insert(0, root_span)
    incomplete_turns = [turn for turn in turns if turn.get("usage_incomplete")]
    # native_ledger_rejected covers the case a rejected native ledger leaves no
    # turn to mark: e.g. token_usage_record lines with no matching legacy
    # event_msg usage, so the session-wide loss would otherwise go unrecorded.
    if incomplete_turns or native_ledger_rejected:
        root_span.attributes["llm.usage_complete"] = False
        root_span.attributes["llm.usage_state"] = "unknown_components"
        totals = [
            turn["reported_total_tokens"]
            for turn in incomplete_turns
            if "reported_total_tokens" in turn
        ]
        if totals:
            root_span.attributes["llm.reported_total_tokens"] = max(totals)

    trace = TraceData(
        trace_id=trace_id,
        start_time=session_start,
        end_time=session_end,
        spans=all_spans,
    )
    return trace, all_spans


# Payload fields read as strings; any other type reads as absent.
_PAYLOAD_STR_FIELDS = (
    "type",
    "role",
    "model",
    "turn_id",
    "cwd",
    "call_id",
    "name",
    "id",
    "message",
)


def _parse_jsonl(data: bytes) -> list[dict]:
    """Lighter JSONL parser — Codex doesn't have noise types to skip.

    every line carries the shared source anchors (offset + hash). A retyped
    payload field reads as absent (see :func:`drop_untyped`) instead of raising
    later and losing the whole session.
    """
    records = parse_jsonl_records(data)
    for record in records:
        drop_untyped(record, ("payload",), dict)
        payload = record.get("payload")
        if payload is None:
            continue
        drop_untyped(payload, _PAYLOAD_STR_FIELDS, str)
        drop_untyped(payload, ("info",), dict)
        drop_untyped(payload, ("content",), list)
        info = payload.get("info")
        if isinstance(info, dict):
            drop_untyped(info, ("last_token_usage", "total_token_usage"), dict)
        if "content" in payload:
            payload["content"] = [b for b in payload["content"] if isinstance(b, dict)]
            for block in payload["content"]:
                drop_untyped(block, ("type", "text"), str)
    return records


def _own_records(lines: list[dict], data: bytes) -> list[dict]:
    """Drop the parent history a forked subagent rollout starts with.

    A subagent spawned with `fork_turns` copies the parent's conversation into
    its own rollout, all stamped with the fork time, and records where its own
    lines begin in `session_meta.subagent_history_start_ordinal` (the 0-based
    index of its first own line). Parsing the copy would count the parent's
    prompts as the subagent's turns and bill the parent's tokens twice. Only
    the subagent's own `session_meta` (always first) is kept from the copy.

    The ordinal counts physical lines, and the parsed records skip blank or
    unreadable ones, so the cut is made at that line's byte offset (the same
    normalized stream `parse_jsonl_records` stamps offsets on).

    Subagents only: a `guardian_review` rollout records the same field, but
    its own reviews are written BEFORE that ordinal, so cutting there would
    drop every request it made.
    """
    if not lines or lines[0].get("type") != "session_meta":
        return lines
    meta = lines[0].get("payload", {})
    if meta.get("thread_source") != "subagent":
        return lines
    start = meta.get("subagent_history_start_ordinal")
    if type(start) is not int or start <= 1:
        return lines
    stream = data.decode("utf-8", errors="replace").encode("utf-8")
    cut = -1
    for _ in range(start):
        cut = stream.find(b"\n", cut + 1)
        if cut < 0:
            return lines[:1]  # the whole file is the copied history
    cut += 1
    return [lines[0], *(line for line in lines[1:] if line.get(SOURCE_OFFSET_KEY, 0) >= cut)]


def _thread_attrs(lines: list[dict]) -> dict:
    """Who started this thread, from the rollout's own `session_meta`.

    Codex Desktop writes each subagent (and each guardian review) as its own
    rollout. `thread_source` says which (`user`, `subagent`,
    `guardian_review`, ...), `parent_thread_id` names the thread that spawned
    it, and `source.subagent.thread_spawn.agent_path` is the task path the
    parent's `spawn_agent` call returned, which links a launch to its child.
    """
    meta = lines[0].get("payload", {}) if lines and lines[0].get("type") == "session_meta" else {}
    attrs = {}
    for key, value in (
        ("harness.thread_source", meta.get("thread_source")),
        ("harness.parent_thread_id", meta.get("parent_thread_id")),
    ):
        if isinstance(value, str) and value:
            attrs[key] = value
    source = meta.get("source")
    spawn = source.get("subagent") if isinstance(source, dict) else None
    spawn = spawn.get("thread_spawn") if isinstance(spawn, dict) else None
    path = spawn.get("agent_path") if isinstance(spawn, dict) else None
    if isinstance(path, str) and path:
        attrs["harness.agent_path"] = path[:200]
    return attrs


def _request_input(usage: object) -> int:
    """A request's full input (cached included): the context it was sent."""
    value = usage.get("input_tokens") if isinstance(usage, dict) else None
    return value if type(value) is int and value > 0 else 0


def _session_signals(lines: list[dict]) -> tuple[list[dict], list[str]]:
    """Compactions and user interrupts, each at its own timestamp.

    A `compacted` record is Codex replacing the history with a summary. Its
    `latest_token_usage_record` is usually null, so the context before it
    falls back to the last request's input seen in the rollout. A
    `turn_aborted` event with reason `interrupted` is the user stopping a turn
    (Esc). Neither carries text into the result.
    """
    compactions: list[dict] = []
    interruptions: list[str] = []
    last_input = 0
    for line in lines:
        kind = line.get("type")
        payload = line.get("payload", {})
        ts = line.get("timestamp")
        if kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            last_input = _request_input(info.get("last_token_usage")) or last_input
        elif kind == "token_usage_record":
            last_input = _request_input(payload.get("usage")) or last_input
        elif kind == "compacted" and ts:
            record = payload.get("latest_token_usage_record")
            usage = record.get("usage") if isinstance(record, dict) else None
            compactions.append({"timestamp": ts, "pre_tokens": _request_input(usage) or last_input})
        elif (
            kind == "event_msg"
            and payload.get("type") == "turn_aborted"
            and payload.get("reason") == "interrupted"
            and ts
        ):
            interruptions.append(ts)
    return compactions, interruptions


# A user-role message whose text starts with one of these is context Codex
# injected, not a prompt: `<environment_context>`, `<user_instructions>`, ...,
# the repo's AGENTS.md, and the IDE extension's editor context (whose clean
# prompt follows as an `event_msg` `user_message`). A prompt the user wrote as
# a markdown heading (`# Plan`, `## Error Type`) is still a prompt.
_INJECTED_PREFIXES = ("<", "# AGENTS.md instructions", "# Context from my IDE setup")


def _total_only_usage(usage: dict) -> bool:
    """Recognize legacy exports whose component counters are absent/zero.

    The reported total is evidence of usage, never evidence of an input/output
    split. Nonzero conflicting components and native request ledgers still fail
    validation; this exception only preserves activity from total-only exports.
    """
    total = usage.get("total_tokens")
    return (
        type(total) is int
        and total > 0
        and all(
            key not in usage or (type(usage[key]) is int and usage[key] == 0)
            for key in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "reasoning_output_tokens",
            )
        )
    )


def _token_components(usage: dict) -> tuple[int, int, int]:
    """The three billed components, validated for internal consistency.

    Codex output already includes reasoning; cached input is an input subset.

    Split out of :func:`_token_usage` because the two kinds of failure below it
    mean opposite things for the running cumulative baseline. A figure that
    fails HERE is malformed: the snapshot contradicts itself and nothing in it
    can be trusted, the baseline included. A figure that passes here but is
    refused for an unpriceable extra component is well formed and its total is
    still good, so the baseline must move with it. Keeping both in one function
    is what made every cache-write record hold the baseline back.
    """
    values = []
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        value = usage.get(key, 0 if key == "cached_input_tokens" else None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Invalid Codex usage component: {key}")
        values.append(value)
    if values[1] > values[0]:
        raise ValueError("Codex cached input exceeds total input")
    if "total_tokens" in usage:
        total = usage["total_tokens"]
        if type(total) is not int or total != values[0] + values[2]:
            raise ValueError("Codex token total must equal input plus output")
    return tuple(values)


def _reasoning_tokens(usage: dict, output_tokens: int) -> int:
    """Display-only reasoning-token count from a single usage snapshot.

    Never affects billing: `_token_components` already docstrings that Codex
    `output_tokens` includes reasoning, so this is purely a breakdown for
    display, not a fourth billed component. Absent or malformed values fall
    back to 0 rather than raising — this field is optional metadata, unlike
    the billed components `_token_components` enforces. Clamped to
    `output_tokens` (the caller's already-validated billed component) so a
    malformed vendor payload can't report reasoning larger than the
    completion it is supposed to be a subset of.
    """
    value = usage.get("reasoning_output_tokens", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, output_tokens)


def _reject_unpriceable_components(usage: dict) -> None:
    """Refuse a well-formed snapshot this parser cannot price.

    The current OpenAI source reports zero writes. A nonzero value from a
    different provider lacks the TTL needed to price the write.
    Codex can run models from several providers, so a nonzero write stays a
    refusal, not an assumption.
    """
    if usage.get("cache_write_input_tokens"):
        raise ValueError("Codex cache-write pricing requires provider/TTL evidence")


def _token_usage(usage: dict) -> tuple[int, int, int]:
    """Billed components, refusing anything this parser cannot price."""
    values = _token_components(usage)
    _reject_unpriceable_components(usage)
    return values


def _native_usage_events(lines: list[dict]) -> dict[str, dict] | None:
    """Return provider-owned usage records after validating their ledger."""
    if not any(line.get("type") == "token_usage_record" for line in lines):
        return None
    owner = next(
        (line.get("payload", {}).get("id") for line in lines if line.get("type") == "session_meta"),
        None,
    )
    if not isinstance(owner, str) or not owner:
        raise ValueError("Codex native usage lacks its session owner")
    models_by_turn: dict[str, set[str]] = {}
    for line in lines:
        if line.get("type") == "turn_context":
            context = line.get("payload", {})
            model = model_id_or_none(context.get("model"))
            if context.get("turn_id") and model:
                models_by_turn.setdefault(context["turn_id"], set()).add(model)

    events: dict[str, dict] = {}
    seen_native: dict[str, tuple[dict, str]] = {}
    native_total = (0, 0, 0)
    for line in lines:
        kind = line.get("type")
        payload = line.get("payload", {})
        if kind == "token_usage_record":
            for key in ("thread_id", "turn_id", "response_id"):
                if not isinstance(payload.get(key), str) or not payload[key]:
                    raise ValueError(f"Codex native usage lacks {key}")
            if payload["thread_id"] != owner:
                raise ValueError("Codex inherited native usage requires owner reconciliation")
            usage_record = payload.get("usage")
            cumulative_record = payload.get("thread_token_usage")
            if not isinstance(usage_record, dict) or not isinstance(cumulative_record, dict):
                raise ValueError("Codex native usage lacks billed components")
            usage = _token_usage(usage_record)
            cumulative = _token_usage(cumulative_record)
            timestamp = line.get("timestamp")
            if not isinstance(timestamp, str) or not timestamp:
                raise ValueError("Codex native usage lacks a timestamp")
            parse_dt(timestamp)
            response_id = payload["response_id"]
            identity = (payload, timestamp)
            if response_id in seen_native:
                if seen_native[response_id] != identity:
                    raise ValueError("Codex native response has conflicting usage")
                continue
            if cumulative != tuple(a + b for a, b in zip(native_total, usage, strict=True)):
                raise ValueError("Codex native cumulative usage has a gap or legacy prefix")
            seen_native[response_id] = identity
            native_total = cumulative
            models = models_by_turn.get(payload["turn_id"], set())
            model = next(iter(models)) if len(models) == 1 else ""
            anchor = source_attrs(line)
            events[anchor["source.content_hash"]] = {
                "timestamp": timestamp,
                "request_id": None,
                "message_id": f"codex-response:{response_id}",
                "turn_id": payload["turn_id"],
                "model": model,
                "input_tokens": usage[0] - usage[1],
                "cache_read_tokens": usage[1],
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
                "output_tokens": usage[2],
                "reasoning_tokens": _reasoning_tokens(usage_record, usage[2]),
                "usage_basis": "token_usage_record",
                **anchor,
            }
    return events


def _group_into_turns(lines: list[dict]) -> tuple[list[dict], bool]:
    """Group Codex lines into turns keyed on user `response_item` messages.

    Codex turn boundary = a `response_item` with `role=user` carrying
    text that isn't a system context injection (`_INJECTED_PREFIXES`).
    A subagent's rollout has no user message: its first turn opens on the
    parent's `agent_message`. Tokens land on `event_msg` `token_count`
    lines and accumulate into the current turn.
    """
    turns: list[dict] = []
    current_turn: dict | None = None
    session_id = (
        next(
            (
                line.get("payload", {}).get("id")
                for line in lines
                if line.get("type") == "session_meta"
            ),
            "",
        )
        or ""
    )
    native_ledger_rejected = False
    try:
        native_events = _native_usage_events(lines)
    except ValueError as exc:
        # A vendor change to the native usage ledger must degrade the whole
        # session to the legacy token_count accounting path below, never abort
        # the transcript. Every turn and tool span still parses; only the more
        # precise native basis is lost for this session.
        native_ledger_rejected = True
        logger.debug(
            "session %s: native Codex usage ledger rejected (%s); falling back to legacy token_count accounting",
            session_id,
            exc,
        )
        native_events = None
    pending_native_events: dict[str, list[dict]] = {}
    emitted_native: set[str] = set()
    native_source_records = {
        source_attrs(line)["source.content_hash"]: line
        for line in lines
        if line.get("type") == "token_usage_record"
    }

    pending_calls: dict[str, dict] = {}  # call_id → pending function call
    processes = ProcessEvidence()

    # Session-wide counter minting synthetic call_ids for unpaired
    # `web_search_call` lines (they carry no call_id of their own).
    websearch_seq = 0

    prev_total_tokens: int | None = None
    prev_usage: tuple[int, int, int] | None = None
    seen_usage_records: set[str] = set()

    # Model active for the current turn. Codex emits a `turn_context` line
    # (with the model) before each turn and can switch models mid-session,
    # so we track the latest and stamp it on each turn.
    current_model = ""
    current_cwd = ""
    active_native_turn_id = ""

    for line in lines:
        line_type = line.get("type", "")
        payload = line.get("payload", {})
        ts = line.get("timestamp", "")

        if line_type in {"session_meta", "turn_context"}:
            observed_cwd = payload.get("cwd")
            if isinstance(observed_cwd, str) and observed_cwd:
                current_cwd = observed_cwd

        if line_type == "turn_context":
            model_id = model_id_or_none(payload.get("model"))
            active_native_turn_id = payload.get("turn_id") or ""
            if model_id:
                current_model = model_id
                # A context can precede the NEXT user message. Never relabel
                # already observed usage on the preceding turn with that model.
                if (
                    native_events is None
                    and current_turn is not None
                    and not current_turn["cost_events"]
                ):
                    current_turn["model"] = model_id
            continue

        if line_type == "response_item" and payload.get("role") == "user":
            content = payload.get("content", [])
            user_text = ""
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "input_text":
                    text = block.get("text", "")
                    if text and not text.startswith(_INJECTED_PREFIXES):
                        user_text = text
                        break

            if user_text:
                if current_turn:
                    turns.append(current_turn)
                current_turn = {
                    "_src": line,  # the line that opened the turn
                    "user_query": user_text,
                    "start_ts": ts,
                    "end_ts": ts,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "cache_read": 0,
                    "tools": [],
                    "cost_events": [],
                    "model": current_model,
                    "_native_turn_id": active_native_turn_id,
                }

        elif (
            line_type == "response_item"
            and payload.get("type") == "agent_message"
            and current_turn is None
        ):
            # A subagent's input: its parent's task message, not a user prompt.
            # It opens the subagent's first turn so the tool calls that follow
            # have a turn to land in. No prompt text is kept: like Claude Code,
            # a subagent's instructions are never a user turn.
            current_turn = {
                "_src": line,
                "user_query": "",
                "start_ts": ts,
                "end_ts": ts,
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cache_read": 0,
                "tools": [],
                "cost_events": [],
                "model": current_model,
                "_native_turn_id": active_native_turn_id,
            }

        elif line_type == "token_usage_record" and native_events is not None:
            event = native_events.get(source_attrs(line)["source.content_hash"])
            if event is not None and event["message_id"] not in emitted_native:
                emitted_native.add(event["message_id"])
                pending_native_events.setdefault(event["turn_id"], []).append(event)

        elif line_type == "event_msg":
            evt_type = payload.get("type", "")
            if evt_type == "user_message" and not current_turn:
                current_turn = {
                    "_src": line,
                    "user_query": payload.get("message", ""),
                    "start_ts": ts,
                    "end_ts": ts,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "cache_read": 0,
                    "tools": [],
                    "cost_events": [],
                    "model": current_model,
                    "_native_turn_id": active_native_turn_id,
                }
            elif evt_type == "token_count" and current_turn:
                if native_events is not None:
                    continue
                info = payload.get("info") or {}
                cum = info.get("total_token_usage") or {}
                last = info.get("last_token_usage") or {}
                if not cum and not last:
                    continue  # Rate-limit-only observation; no usage was reported.
                anchor = source_attrs(line)
                record_id = anchor["source.content_hash"]
                if record_id in seen_usage_records:
                    continue
                seen_usage_records.add(record_id)
                # Set only once this record's own cumulative figures have passed
                # every check that judges THEM, so the except below can move the
                # baseline forward without trusting a number that was itself the
                # problem. Leaving the baseline behind on an otherwise sound
                # record would make the next record's delta include the rejected
                # tokens and bill them twice; adopting the figures from a record
                # rejected BECAUSE its counter moved backwards or contradicted
                # itself would corrupt every turn after it. Neither is acceptable,
                # hence the narrow window.
                rejected_total: int | None = None
                rejected_cumulative: tuple[int, int, int] | None = None
                try:
                    cur_total = cum.get("total_tokens")
                    if cur_total is not None and (
                        isinstance(cur_total, bool)
                        or not isinstance(cur_total, int)
                        or cur_total < 0
                    ):
                        raise ValueError("Invalid Codex cumulative token count")
                    # Components only. The priceability refusal is deferred to
                    # just after the baseline is recorded below: a cache-write
                    # figure is well formed and its total is trustworthy, so
                    # raising here would hold the baseline at the previous
                    # record and make every later delta span two records.
                    cumulative = (
                        _token_components(cum)
                        if "input_tokens" in cum and not _total_only_usage(cum)
                        else None
                    )
                    if cur_total is not None and prev_total_tokens is not None:
                        if cur_total < prev_total_tokens:
                            # No inferred counter epochs. A decrease may be
                            # a reset or stale snapshot; hold capture for reconciliation.
                            raise ValueError("Codex cumulative usage moved backwards")
                        if cur_total == prev_total_tokens:
                            if (
                                cumulative is not None
                                and prev_usage is not None
                                and cumulative != prev_usage
                            ):
                                raise ValueError(
                                    "Codex repeated total has conflicting token components"
                                )
                            # Nothing to bill, but still something to say: a
                            # repeated total that carries an unpriceable
                            # component has shown us a cost we did not account
                            # for, and continuing quietly would let the session
                            # report a complete picture that is not complete.
                            # The baseline is untouched either way, the total
                            # has not moved.
                            _reject_unpriceable_components(cum)
                            continue
                    # Past this point the cumulative figures are trustworthy:
                    # well formed, not moving backwards, not contradicting a
                    # repeated total. Anything rejected from here on is a fault
                    # in `last`, in the delta, or in a component this parser
                    # cannot price, none of which impugns the running total.
                    rejected_total = cur_total
                    rejected_cumulative = cumulative
                    # Safe to refuse now: whatever happens next, the baseline
                    # moves to this record's own figures rather than staying on
                    # the one before it.
                    _reject_unpriceable_components(cum)
                    if _total_only_usage(cum) or _total_only_usage(last):
                        current_turn["usage_incomplete"] = True
                        if cur_total is not None:
                            current_turn["reported_total_tokens"] = cur_total
                        if not last or _total_only_usage(last):
                            if cur_total is not None:
                                prev_total_tokens = cur_total
                            prev_usage = cumulative
                            if ts:
                                current_turn["end_ts"] = ts
                            continue
                    basis = "last_token_usage"
                    if last:
                        usage = _token_usage(last)
                        reasoning = _reasoning_tokens(last, usage[2])
                        if cur_total is not None and prev_total_tokens is not None:
                            # The snapshot says the counter moved by exactly this
                            # much; the per-call components must add up to it or
                            # the billed figure disagrees with the vendor's own
                            # total. Same total definition as
                            # `_token_usage` (input incl. cached + output).
                            delta = cur_total - prev_total_tokens
                            if usage[0] + usage[2] != delta:
                                raise ValueError(
                                    "Codex last_token_usage disagrees with the cumulative delta"
                                )
                    elif cumulative is not None:
                        basis = "cumulative_delta"
                        # No per-call snapshot to read reasoning_output_tokens
                        # from here, only a running total with no matching
                        # prev-reasoning baseline tracked alongside prev_usage
                        # -- report 0 rather than fabricate a delta.
                        reasoning = 0
                        if prev_total_tokens is not None and prev_usage is None:
                            # A total-only prefix cannot provide a component delta.
                            # Establish this baseline for later observed increments.
                            current_turn["usage_incomplete"] = True
                            prev_total_tokens, prev_usage = cur_total, cumulative
                            continue
                        usage = tuple(
                            a - b for a, b in zip(cumulative, prev_usage or (0, 0, 0), strict=True)
                        )
                        if min(usage) < 0 or usage[1] > usage[0]:
                            # The total climbed, so `rejected_total` stays: that
                            # figure passed its own guards above. These components
                            # did not, and adopting them would measure every later
                            # delta against numbers this parser has just refused,
                            # rejecting the rest of the session off one bad record.
                            rejected_cumulative = None
                            raise ValueError("Codex cumulative components moved backwards")
                    else:
                        raise ValueError("Codex usage has no billed token components")
                    if cur_total is not None:
                        prev_total_tokens = cur_total
                    prev_usage = cumulative
                    in_t, cache_r, out_t = usage
                    if in_t or out_t:
                        if not ts:
                            raise ValueError("Codex billed usage has no timestamp")
                        current_turn["input_tokens"] += in_t
                        current_turn["output_tokens"] += out_t
                        current_turn["reasoning_tokens"] += reasoning
                        current_turn["cache_read"] += cache_r
                        current_turn["model"] = current_model
                        current_turn["cost_events"].append(
                            {
                                "timestamp": ts,
                                "request_id": None,
                                "message_id": f"codex-usage:{record_id}",
                                "model": current_model,
                                "input_tokens": in_t - cache_r,
                                "cache_read_tokens": cache_r,
                                "cache_write_5m_tokens": 0,
                                "cache_write_1h_tokens": 0,
                                "output_tokens": out_t,
                                "reasoning_tokens": reasoning,
                                "usage_basis": basis,
                                **anchor,
                            }
                        )
                except ValueError as exc:
                    # A vendor change to this one usage snapshot must not cost the
                    # whole transcript: lose this record's token accounting only,
                    # mark the turn's usage incomplete (same signal as a total-only
                    # export), and keep every other turn and tool span intact.
                    logger.debug(
                        "session %s record %s: Codex token_count usage rejected (%s); "
                        "marking turn usage incomplete and continuing",
                        session_id,
                        record_id,
                        exc,
                    )
                    current_turn["usage_incomplete"] = True
                    # Resync to whatever the vendor reported for THIS record, so
                    # the next record's delta starts from the right place. Only
                    # values that passed their own validation above are used; if
                    # the totals themselves were the unreadable part, the
                    # baseline stays put and the next record is judged against
                    # the last figure we could trust.
                    if rejected_total is not None:
                        prev_total_tokens = rejected_total
                    if rejected_cumulative is not None:
                        prev_usage = rejected_cumulative
                    continue

            if current_turn and ts:
                current_turn["end_ts"] = ts

        elif line_type == "response_item" and payload.get("type") in _TOOL_CALL_TYPES:
            call_id = payload.get("call_id", "")
            name = payload.get("name", "unknown")
            if not isinstance(name, str) or not name:
                name = "unknown"
            # function_call carries JSON `arguments`; custom_tool_call
            # (apply_patch) carries the raw patch text under `input`.
            arguments = str(payload.get("arguments") or payload.get("input") or "")
            evidence = extract_edit_evidence(name, arguments, successful=True)
            if name == "apply_patch":
                # Rewrite BEFORE truncation — paths must land in the
                # retained 2000-char prefix (see `_apply_patch_input`).
                arguments = _apply_patch_input(arguments)
            pending_calls[call_id] = {
                "_src": line,  # the line that requested the call
                "call_id": call_id,
                "name": name,
                "input": arguments[:2000],
                "_operations": literal_operations(arguments) if name == "exec" else [],
                "provenance.capture": "unsupported_orchestration"
                if name == "exec"
                else "native_operation",
                "_arguments": arguments if name in {"exec_command", "write_stdin"} else "",
                **creation_command_attributes(arguments, name),
                "edit_evidence": evidence,
                "cwd": current_cwd,
                "output": "",
                "start_ts": ts,
                "end_ts": ts,
            }
            if current_turn and ts:
                current_turn["end_ts"] = ts

        elif line_type == "response_item" and payload.get("type") == "web_search_call":
            # Codex built-in browsing: a single completed line per action —
            # no call_id, no paired `*_output`. Emit a point-in-time tool
            # (start == end, empty output) parented to the current turn,
            # mirroring how the other parsers surface single-event tools
            # (Cursor's `web_search` rows also carry no separate result
            # event). The `action` dict is the whole input: `search`
            # carries `query`/`queries`, `open_page` a `url`,
            # `find_in_page` a `url` + `pattern`.
            if current_turn is not None:
                websearch_seq += 1
                current_turn["tools"].append(
                    {
                        "_src": line,  # the line that carried the search
                        "call_id": f"websearch:{websearch_seq}",
                        "name": "web_search_call",
                        "input": json.dumps(payload.get("action") or {})[:2000],
                        "output": "",
                        "start_ts": ts,
                        "end_ts": ts,
                        # Only an explicit `failed` status is failure
                        # evidence; completed calls report `completed`.
                        "is_error": payload.get("status") == "failed",
                    }
                )
                if ts:
                    current_turn["end_ts"] = ts

        elif line_type == "response_item" and payload.get("type") in _TOOL_OUTPUT_TYPES:
            call_id = payload.get("call_id", "")
            output = payload.get("output", "")
            pending = pending_calls.get(call_id)
            operations = pending.get("_operations", []) if pending else []
            results = literal_results(output, len(operations)) if operations else []
            if pending and results and current_turn:
                for index, ((name, args), result) in enumerate(
                    zip(operations, results, strict=False)
                ):
                    arguments = args if isinstance(args, str) else json.dumps(args)
                    failed = _tool_output_is_error(json.dumps(result))
                    native = {
                        "_src": pending["_src"],
                        "call_id": f"{call_id}:op:{index}",
                        "name": name,
                        "provenance.capture": "literal_operation",
                        "input": arguments[:2000],
                        "cwd": args.get("workdir", pending["cwd"])
                        if isinstance(args, dict)
                        else pending["cwd"],
                        "start_ts": pending["start_ts"],
                        "end_ts": ts,
                        "output": json.dumps(result)[:2000],
                        "is_error": failed,
                        "edit_evidence": extract_edit_evidence(
                            name, arguments, successful=not failed
                        ),
                        **creation_command_attributes(arguments, name),
                    }
                    native.update(
                        creation_output_attributes(
                            result,
                            native.get("pr.create.provider"),
                            native.get("pr.create.repository"),
                        )
                    )
                    processes.observe(native, args, result)
                    current_turn["tools"].append(native)
                pending_calls.pop(call_id)
                current_turn["end_ts"] = ts
                continue
            if isinstance(output, list):
                output = "\n".join(
                    block["text"]
                    for block in output
                    if isinstance(block, dict)
                    and block.get("type") in {"text", "input_text", "output_text"}
                    and isinstance(block.get("text"), str)
                )
            if call_id in pending_calls:
                pending_calls[call_id].update(
                    creation_output_attributes(
                        output,
                        pending_calls[call_id].get("pr.create.provider"),
                        pending_calls[call_id].get("pr.create.repository"),
                    )
                )
                pending_calls[call_id]["output"] = str(output)[:2000]
                # Decide failure on the FULL payload, before truncation —
                # apply_patch's JSON output carries `metadata.exit_code`
                # after a potentially long `output` text, so a truncated
                # string would no longer parse.
                pending_calls[call_id]["is_error"] = _tool_output_is_error(output)
                pending_calls[call_id]["end_ts"] = ts
                try:
                    args = json.loads(pending_calls[call_id].get("_arguments") or "null")
                    result = shell_result(output)
                except (ValueError, TypeError, RecursionError):
                    pass
                else:
                    if isinstance(result, dict):
                        processes.observe(pending_calls[call_id], args, result)
                if current_turn:
                    current_turn["tools"].append(pending_calls.pop(call_id))
                    current_turn["end_ts"] = ts

        elif current_turn and ts:
            current_turn["end_ts"] = ts

    if current_turn:
        for call in pending_calls.values():
            current_turn["tools"].append(call)
        turns.append(current_turn)

    turns_by_native_id = {
        turn["_native_turn_id"]: turn for turn in turns if turn.get("_native_turn_id")
    }
    for native_turn_id, events in pending_native_events.items():
        target = turns_by_native_id.get(native_turn_id)
        if target is None:
            target = {
                "_src": native_source_records[events[0]["source.content_hash"]],
                "user_query": "",
                "start_ts": events[0]["timestamp"],
                "end_ts": events[-1]["timestamp"],
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cache_read": 0,
                "tools": [],
                "cost_events": [],
                "model": events[0]["model"],
                "_native_turn_id": native_turn_id,
            }
            turns.append(target)
            turns_by_native_id[native_turn_id] = target
        for event in events:
            target["input_tokens"] += event["input_tokens"] + event["cache_read_tokens"]
            target["cache_read"] += event["cache_read_tokens"]
            target["output_tokens"] += event["output_tokens"]
            target["reasoning_tokens"] += event["reasoning_tokens"]
            target["model"] = event["model"] or target["model"]
            target["cost_events"].append(event)
            target["start_ts"] = min(target["start_ts"], event["timestamp"])
            target["end_ts"] = max(target["end_ts"], event["timestamp"])

    return turns, native_ledger_rejected


def _tool_output_is_error(output: object) -> bool:
    """Best-effort failure detection on a Codex tool-call output payload.

    Same conservative semantics as `cursor_store._tool_result_is_error` /
    Claude Code's `is_error` flag: only clear failure evidence flips a tool
    span to ERROR; anything ambiguous stays OK. Codex never sets a failure
    status on the call line itself (`custom_tool_call.status` is
    `"completed"` even for rejected patches), so the output payload is the
    only signal. Recognized shapes:

    - exec-style plain text (`exec_command`/`write_stdin`): a
      `Process exited with code N` header line — nonzero N is a failure.
      Backgrounded processes (`Process running with session ID ...`) carry
      no header and stay OK.
    - `apply_patch` JSON (`{"output": ..., "metadata": {"exit_code": N}}`):
      nonzero `metadata.exit_code` is a failure; a truthy top-level
      `"error"` key too (mirrors cursor's `result.get("error")` check).
    - `apply_patch` plain text: the verifier's
      `apply_patch verification failed: ...` message is a failure.
    - MCP JSON dicts / image-block lists: no failure evidence — OK.

    Args:
        output: The `*_output` payload's `output` field, UNtruncated —
            usually a string, occasionally a list of content blocks.

    Returns:
        True only when the payload carries one of the failure shapes above.
    """
    if not isinstance(output, str) or not output:
        return False
    if output.startswith("apply_patch verification failed"):
        return True
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        if parsed.get("isError") is True:
            return True
        exit_code = parsed.get("exit_code")
        if type(exit_code) is int and exit_code != 0:
            return True
        metadata = parsed.get("metadata")
        if isinstance(metadata, dict):
            exit_code = metadata.get("exit_code")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
                return True
        return bool(parsed.get("error"))
    m = _RE_EXEC_EXIT_CODE.search(output)
    return bool(m and m.group(1) != "0")


__all__ = ["parse_codex"]
