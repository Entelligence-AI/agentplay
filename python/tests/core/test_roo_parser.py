"""Tests for Roo Code session detection and parsing."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from agentplay import builder, facts, search
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import record_hash
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    AgentType,
    detect_agent,
    parse_roo,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.base import (
    _ROO_API_TOOLS,
)
from agentplay._core.ingestion.coding_agent._sources.local.source import (
    LocalSource,
    _is_roo_api_history,
)
from agentplay._core.ingestion.coding_agent.metrics import compute_task_counters
from agentplay._core.ingestion.coding_agent.session_pure import build_session_metrics

FIXTURES = Path(__file__).parent / "fixtures" / "roo"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize(
    "name",
    [
        "read_and_edit.json",
        "failed_command.json",
        "api_history.json",
    ],
)
def test_fixture_is_detected_as_roo_code(name: str):
    data = _fixture_bytes(name)
    assert detect_agent(data) == AgentType.ROO_CODE


@pytest.mark.parametrize(
    "name",
    [
        "read_and_edit.json",
        "failed_command.json",
        "api_history.json",
    ],
)
def test_incomplete_array_head_is_detected_as_roo_code(name: str):
    # Simulates _read_head capping at 5MB, cutting off the closing ']'
    data = _fixture_bytes(name)
    incomplete = data[: len(data) // 2]
    assert detect_agent(incomplete) == AgentType.ROO_CODE


def test_large_array_head_without_closing_bracket_is_detected():
    # Simulates a large Roo session whose head has no closing bracket
    data = _fixture_bytes("read_and_edit.json")
    padded = data.rstrip()[:-1] + b"," + (b'{"ts": 1730000000000, "type": "say", "say": "text", "text": "filler"},') * 50
    cut_head = padded[:1500]
    assert b"]" not in cut_head
    assert detect_agent(cut_head) == AgentType.ROO_CODE


def test_api_history_tool_use_beyond_first_five_messages_is_detected():
    # Several conversational messages before the first tool_use
    messages = [
        {"role": "user", "content": f"Turn {i} question"}
        if i % 2 == 0
        else {"role": "assistant", "content": f"Turn {i} reply"}
        for i in range(8)
    ]
    messages.append({
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I will inspect the file."},
            {
                "type": "tool_use",
                "id": "call_01",
                "name": "read_file",
                "input": {"path": "src/app.py"},
            },
        ],
    })
    # Trailing message so truncating the array cuts off the trailing message,
    # leaving the preceding tool_use message intact for incremental decoding.
    messages.append({"role": "user", "content": "Waiting for results..."})

    # Escape raw keywords with JSON unicode escapes so raw-byte fallback cannot match
    raw = json.dumps(messages).encode("utf-8")
    escaped = (
        raw.replace(b"tool_use", b"\\u0074ool_use")
        .replace(b"read_file", b"\\u0072ead_file")
        .replace(b"tool", b"\\u0074ool")
    )

    # Verify raw-byte fallback signatures cannot match
    assert b"tool" not in escaped
    assert not any(tool.encode("utf-8") in escaped for tool in _ROO_API_TOOLS)
    assert b"say" not in escaped
    assert b"ask" not in escaped

    # Complete JSON array: recognized by parsed-array scan beyond first 5 messages
    assert detect_agent(escaped) == AgentType.ROO_CODE

    # Incomplete array (truncated during trailing message, no closing bracket):
    # recognized by incremental parsed-record scan beyond first 5 messages
    truncated = escaped[:-15]
    assert detect_agent(truncated) == AgentType.ROO_CODE


@pytest.mark.parametrize(
    "tool_name",
    [
        "apply_diff",
        "applyDiff",
        "search_files",
        "searchFiles",
        "listFiles",
        "listCodeDefinitionNames",
    ],
)
def test_api_history_with_supported_roo_tools_is_detected(tool_name: str):
    msg = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": tool_name,
                    "input": {},
                }
            ],
        }
    ]
    raw = json.dumps(msg).encode("utf-8")
    assert detect_agent(raw) == AgentType.ROO_CODE


@pytest.mark.parametrize(
    "malformed_name",
    [
        {"bad": "object"},
        [1, 2, 3],
        12345,
        None,
        True,
    ],
)
def test_api_history_with_non_string_tool_name_does_not_crash_detection(malformed_name):
    msg = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": malformed_name,
                    "input": {},
                }
            ],
        }
    ]
    raw_array = json.dumps(msg).encode("utf-8")
    assert detect_agent(raw_array) is None

    line = json.dumps(msg[0]).encode("utf-8") + b"\n"
    assert detect_agent(line) is None


def test_other_agent_fixtures_not_claimed_as_roo():
    gemini_fix = Path(__file__).parent / "fixtures" / "gemini" / "read_explain.jsonl"
    if gemini_fix.is_file():
        assert detect_agent(gemini_fix.read_bytes()) != AgentType.ROO_CODE

    antigravity_fix = Path(__file__).parent / "fixtures" / "antigravity" / "read_explain.jsonl"
    if antigravity_fix.is_file():
        assert detect_agent(antigravity_fix.read_bytes()) != AgentType.ROO_CODE


def test_roo_parser_read_and_edit():
    data = _fixture_bytes("read_and_edit.json")
    trace, spans = parse_roo(data, trace_id="local:roo-code:task-123", project_name="my-proj")

    assert trace.trace_id == "local:roo-code:task-123"
    assert len(spans) == 4  # 1 root + 1 turn + 2 tools

    root = spans[0]
    assert root.name == "Session"
    assert root.attributes["project.name"] == "my-proj"
    assert root.attributes["llm.model_name"] == "claude-3-7-sonnet"

    turn = spans[1]
    assert turn.name == "Turn 1"
    assert "Add helper function" in turn.attributes["input.value"]
    assert turn.attributes["llm.token_count.prompt"] == 1500
    assert turn.attributes["llm.token_count.completion"] == 220
    assert turn.attributes["llm.token_count.cache_read"] == 500
    assert turn.attributes["llm.token_count.cache_write"] == 0

    tool1 = spans[2]
    assert tool1.name == "Read"
    assert tool1.status_code == "OK"
    assert "src/utils.py" in tool1.attributes["input.value"]

    tool2 = spans[3]
    assert tool2.name == "Edit"
    assert tool2.status_code == "OK"
    assert "src/utils.py" in tool2.attributes["input.value"]
    assert tool2.attributes.get("code.edit_evidence") is not None


def test_roo_parser_failed_command_and_retry():
    data = _fixture_bytes("failed_command.json")
    trace, spans = parse_roo(data, trace_id="local:roo-code:task-fail")

    tools = [s for s in spans if "tool." in s.attributes or s.name in ("Bash", "Read", "Edit")]
    assert len(tools) == 2

    # First command exited with code 1 -> ERROR
    assert tools[0].name == "Bash"
    assert tools[0].status_code == "ERROR"
    assert "pytest tests/test_math.py" in tools[0].attributes["input.value"]

    # Second command exited with code 0 -> OK
    assert tools[1].name == "Bash"
    assert tools[1].status_code == "OK"
    assert "pytest tests/test_math.py -q" in tools[1].attributes["input.value"]


def test_roo_parser_api_history():
    data = _fixture_bytes("api_history.json")
    trace, spans = parse_roo(data, trace_id="local:roo-code:task-api")

    tools = [s for s in spans if s.parent_id is not None and s.name != "Turn 1"]
    assert len(tools) == 2
    assert tools[0].name == "ListDir"
    assert tools[0].status_code == "OK"
    assert tools[1].name == "Read"
    assert tools[1].status_code == "OK"

    counters = compute_task_counters(spans, start_turn=1, end_turn=1)
    assert counters.search_count == 1
    assert counters.read_count == 1


def test_roo_parser_followup_question_and_user_feedback():
    session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": "Fix the bug"},
        {
            "ts": 1730000001000,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps({"tokensIn": 100, "tokensOut": 20, "model": "claude-3-7-sonnet"}),
        },
        {"ts": 1730000002000, "type": "ask", "ask": "followup", "text": "Should I run tests first?"},
        {"ts": 1730000003000, "type": "say", "say": "user_feedback", "text": "Yes, please run tests"},
        {
            "ts": 1730000004000,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps({"tokensIn": 120, "tokensOut": 30, "model": "claude-3-7-sonnet"}),
        },
        {"ts": 1730000005000, "type": "say", "say": "text", "text": "All tests pass"},
    ]
    raw_bytes = json.dumps(session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:followup-task")
    turns = [s for s in spans if s.name.startswith("Turn ")]
    assert len(turns) == 2

    # Turn 1: prompt is "Fix the bug", output has the assistant followup question
    assert turns[0].name == "Turn 1"
    assert turns[0].attributes["input.value"] == "Fix the bug"
    assert "Should I run tests first?" in turns[0].attributes["output.value"]

    # Turn 2: prompt is user_feedback ("Yes, please run tests"), output is assistant response
    assert turns[1].name == "Turn 2"
    assert turns[1].attributes["input.value"] == "Yes, please run tests"
    assert "All tests pass" in turns[1].attributes["output.value"]


def test_roo_parser_handles_null_and_malformed_token_metrics():
    session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": "Run check"},
        {
            "ts": 1730000001000,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps(
                {
                    "tokensIn": None,
                    "tokensOut": "invalid",
                    "cacheReads": None,
                    "cacheWrites": [1, 2, 3],
                    "model": "claude-3-7-sonnet",
                }
            ),
        },
        {
            "ts": 1730000002000,
            "type": "say",
            "say": "api_req_finished",
            "text": json.dumps(
                {
                    "tokensIn": "not_an_int",
                    "prompt_tokens": 50,
                    "tokensOut": 20,
                    "cacheReads": 10,
                    "cacheWrites": None,
                    "model": "claude-3-7-sonnet",
                }
            ),
        },
        {"ts": 1730000003000, "type": "say", "say": "text", "text": "Done."},
    ]
    raw_bytes = json.dumps(session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:null-tokens")
    turn = spans[1]
    assert turn.name == "Turn 1"
    assert turn.attributes["llm.token_count.prompt"] == 50
    assert turn.attributes["llm.token_count.completion"] == 20
    assert turn.attributes["llm.token_count.cache_read"] == 10
    assert turn.attributes["llm.token_count.cache_write"] == 0
    cost_events = turn.attributes["llm.cost_events"]
    assert len(cost_events) == 1
    assert cost_events[0]["cache_write_5m_tokens"] == 0


def test_roo_parser_counts_request_lifecycle_with_both_started_and_finished_only_once():
    session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": "Run analysis"},
        {
            "ts": 1730000001000,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps({"tokensIn": 100, "tokensOut": 25, "model": "claude-3-7-sonnet"}),
        },
        {
            "ts": 1730000002000,
            "type": "say",
            "say": "api_req_finished",
            "text": json.dumps({"tokensIn": 100, "tokensOut": 25, "model": "claude-3-7-sonnet"}),
        },
        {"ts": 1730000003000, "type": "say", "say": "text", "text": "Analysis complete"},
    ]
    raw_bytes = json.dumps(session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:single-lifecycle")
    turn = spans[1]
    assert turn.name == "Turn 1"
    assert turn.attributes["llm.token_count.prompt"] == 100
    assert turn.attributes["llm.token_count.completion"] == 25
    cost_events = turn.attributes["llm.cost_events"]
    assert len(cost_events) == 1
    assert cost_events[0]["input_tokens"] == 100
    assert cost_events[0]["output_tokens"] == 25


def test_roo_builder_never_leaks_free_text():
    secret_prompt = "SECRET-ROO-PROMPT-12345"
    secret_arg = "SECRET-FILE-SECRET.ts"
    secret_output = "SECRET-COMMAND-OUTPUT-SECRET"

    fake_session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": secret_prompt},
        {
            "ts": 1730000001000,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps({"tokensIn": 500, "tokensOut": 100, "model": "claude-3-7-sonnet"}),
        },
        {
            "ts": 1730000002000,
            "type": "ask",
            "ask": "command",
            "text": f"git status {secret_arg}",
        },
        {"ts": 1730000003000, "type": "say", "say": "command_output", "text": secret_output},
        {"ts": 1730000004000, "type": "say", "say": "text", "text": "All done."},
    ]
    raw_bytes = json.dumps(fake_session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:secret-task")
    doc = builder.build_map_from_spans(spans, session_id="local:roo-code:secret-task", agent_tool="roo-code")

    raw_doc = json.dumps(doc) + json.dumps(facts.stamp(doc)) + json.dumps(search.postings(doc))
    assert secret_prompt not in raw_doc
    assert secret_arg not in raw_doc
    assert secret_output not in raw_doc
    assert doc["totals"]["calls"] == 1
    assert doc["totals"]["requests"] == 1


def test_roo_discovery_and_indexing(tmp_path: Path):
    tasks_dir = tmp_path / "globalStorage" / "rooveterinaryinc.roo-cline" / "tasks" / "task_abc"
    tasks_dir.mkdir(parents=True)
    ui_json = tasks_dir / "ui_messages.json"
    ui_json.write_bytes(_fixture_bytes("read_and_edit.json"))

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        roo_homes=[tmp_path / "globalStorage" / "rooveterinaryinc.roo-cline"],
    )
    index = source._build_index()
    assert "local:roo-code:task_abc" in index

    spans = source.get_spans_by_session("local:roo-code:task_abc")
    assert len(spans) == 4
    assert spans[0].name == "Session"


def test_roo_discovery_accepts_vscode_user_directory_override(tmp_path: Path):
    user_dir = tmp_path / "User"
    task_dir = user_dir / "globalStorage" / "rooveterinaryinc.roo-cline" / "tasks" / "task_user_dir"
    task_dir.mkdir(parents=True)
    ui_json = task_dir / "ui_messages.json"
    ui_json.write_bytes(_fixture_bytes("read_and_edit.json"))

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        roo_homes=[user_dir],
    )
    index = source._build_index()
    assert "local:roo-code:task_user_dir" in index

    spans = source.get_spans_by_session("local:roo-code:task_user_dir")
    assert len(spans) == 4
    assert spans[0].name == "Session"


def test_roo_discovery_scans_both_vscode_tasks_and_home_tasks_with_deduplication(tmp_path: Path):
    home_dir = tmp_path / "home"

    # 1. Session under vscode_tasks: home/globalStorage/rooveterinaryinc.roo-cline/tasks/task_vscode
    vscode_task_dir = home_dir / "globalStorage" / "rooveterinaryinc.roo-cline" / "tasks" / "task_vscode"
    vscode_task_dir.mkdir(parents=True)
    (vscode_task_dir / "ui_messages.json").write_bytes(_fixture_bytes("read_and_edit.json"))

    # 2. Session under home/tasks: home/tasks/task_home
    home_task_dir = home_dir / "tasks" / "task_home"
    home_task_dir.mkdir(parents=True)
    (home_task_dir / "ui_messages.json").write_bytes(_fixture_bytes("failed_command.json"))

    # Configure LocalSource with home_dir and also explicitly with vscode_tasks to verify overlapping file deduplication
    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        roo_homes=[home_dir, home_dir / "globalStorage" / "rooveterinaryinc.roo-cline" / "tasks"],
    )
    index = source._build_index()

    # Both sessions must be indexed
    assert "local:roo-code:task_vscode" in index
    assert "local:roo-code:task_home" in index

    # Verify deduplication: task_vscode was not indexed twice
    stats = source.discovery_stats()
    assert stats["roo-code"]["indexed"] == 2


def test_roo_discovery_accepts_api_history_without_roo_tool_markers(tmp_path: Path):
    tasks_dir = tmp_path / "tasks" / "task_no_tools"
    tasks_dir.mkdir(parents=True)
    api_json = tasks_dir / "api_conversation_history.json"
    history = [
        {"role": "user", "content": [{"type": "text", "text": "What is Python?"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Python is a programming language."}]},
    ]
    raw_bytes = json.dumps(history).encode("utf-8")
    api_json.write_bytes(raw_bytes)

    # Prove that generic detect_agent returns None for this payload (no Roo tool markers)
    assert detect_agent(raw_bytes) is None

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        roo_homes=[tmp_path],
    )
    index = source._build_index()
    assert "local:roo-code:task_no_tools" in index

    spans = source.get_spans_by_session("local:roo-code:task_no_tools")
    assert len(spans) == 2  # Root Session + Turn 1
    assert spans[0].name == "Session"
    turn = spans[1]
    assert turn.name == "Turn 1"
    assert turn.attributes["input.value"] == "What is Python?"
    assert turn.attributes["output.value"] == "Python is a programming language."


def test_roo_discovery_accepts_api_history_with_first_message_larger_than_head_cap(tmp_path: Path):
    tasks_dir = tmp_path / "tasks" / "task_large_first_msg"
    tasks_dir.mkdir(parents=True)
    api_json = tasks_dir / "api_conversation_history.json"

    # First message larger than 5 MB head cap (5 MB + 128 KB)
    large_payload = "A" * (5 * 1024 * 1024 + 128 * 1024)
    history = [
        {"role": "user", "content": [{"type": "text", "text": large_payload}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Received giant payload."}]},
    ]
    with api_json.open("w", encoding="utf-8") as f:
        json.dump(history, f)

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        roo_homes=[tmp_path],
    )
    index = source._build_index()
    assert "local:roo-code:task_large_first_msg" in index


def test_is_roo_api_history_truncated_first_record():
    # Truncated first message without closing bracket or object brace
    truncated_head = b'[{"role": "user", "content": "giant string without end'
    assert _is_roo_api_history(truncated_head) == (True, True)

    # Truncated first message with content as nested block array
    truncated_blocks = b'[{"role": "assistant", "content": [{"type": "text", "text": "giant string'
    assert _is_roo_api_history(truncated_blocks) == (True, True)

    # Empty array variations
    assert _is_roo_api_history(b"[]") == (True, False)
    assert _is_roo_api_history(b"  [   \n\t  ]  ") == (True, False)

    # Reject nested-only role and content
    assert _is_roo_api_history(b'[{"metadata": {"role": "user", "content": "truncated text...') == (False, False)
    assert _is_roo_api_history(b'[{"items": [{"role": "user", "content": "truncated text...') == (False, False)
    assert _is_roo_api_history(b'[{"metadata": {"role": "user", "content": "complete text"}}]') == (False, False)

    # Reject when role or content is missing at top-level
    assert _is_roo_api_history(b'[{"role": "user", "other": "truncated text...') == (False, False)
    assert _is_roo_api_history(b'[{"content": "giant text...", "other": 123') == (False, False)

    # Truncated non-API object
    truncated_non_api = b'[{"random_key": "some value without end'
    assert _is_roo_api_history(truncated_non_api) == (False, False)

    # Array of primitives
    assert _is_roo_api_history(b"[1, 2, 3]") == (False, False)

    # Non-array
    assert _is_roo_api_history(b'{"role": "user", "content": "hi"}') == (False, False)


def test_roo_discovery_skips_empty_api_history_and_empty_ui_messages(tmp_path: Path):
    empty_api_dir = tmp_path / "tasks" / "task_empty_api"
    empty_api_dir.mkdir(parents=True)
    (empty_api_dir / "api_conversation_history.json").write_bytes(b"[]")

    empty_ui_dir = tmp_path / "tasks" / "task_empty_ui"
    empty_ui_dir.mkdir(parents=True)
    (empty_ui_dir / "ui_messages.json").write_bytes(b"   []   ")

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        roo_homes=[tmp_path],
    )
    index = source._build_index()
    assert "local:roo-code:task_empty_api" not in index
    assert "local:roo-code:task_empty_ui" not in index

    stats = source.discovery_stats()
    assert stats["roo-code"]["skipped"].get("no_conversation") == 2


def test_roo_parser_switch_mode_is_not_subagent():
    # ui_messages format
    session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": "Switch mode"},
        {
            "ts": 1730000001000,
            "type": "ask",
            "ask": "tool",
            "text": json.dumps({"tool": "switch_mode", "mode_slug": "code"}),
        },
        {"ts": 1730000002000, "type": "say", "say": "text", "text": "Switched mode."},
    ]
    raw_bytes = json.dumps(session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:switch-mode-ui")
    tools = [s for s in spans if s.name == "SwitchMode"]
    assert len(tools) == 1
    assert tools[0].status_code == "OK"

    doc = builder.build_map_from_spans(spans, session_id="local:roo-code:switch-mode-ui", agent_tool="roo-code")
    tool_events = [e for e in doc["events"] if e.get("tool") == "SwitchMode"]
    assert len(tool_events) == 1
    assert tool_events[0]["kind"] == "tool"
    assert tool_events[0]["kind"] != "subagent"
    assert doc["totals"]["calls"] == 1
    assert doc["totals"]["subagents"] == 0

    # api_conversation_history format
    api_session = [
        {"role": "user", "content": "Switch to architect mode"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_switch",
                    "name": "switchMode",
                    "input": {"mode_slug": "architect"},
                }
            ],
        },
    ]
    raw_api = json.dumps(api_session).encode("utf-8")
    _, api_spans = parse_roo(raw_api, trace_id="local:roo-code:switch-mode-api")
    api_tools = [s for s in api_spans if s.name == "SwitchMode"]
    assert len(api_tools) == 1
    api_doc = builder.build_map_from_spans(api_spans, session_id="local:roo-code:switch-mode-api", agent_tool="roo-code")
    assert api_doc["totals"]["calls"] == 1
    assert api_doc["totals"]["subagents"] == 0


def test_roo_parser_source_cursor_attributes_on_fixtures():
    # read_and_edit.json
    data = _fixture_bytes("read_and_edit.json")
    records = json.loads(data)
    _, spans = parse_roo(data, trace_id="local:roo-code:fixture-test")
    anchored = [s for s in spans if s.name.startswith("Turn ") or s.attributes.get("tool.name")]
    assert len(anchored) == 3

    turn = anchored[0]
    assert turn.attributes["source.kind"] == "json_doc"
    assert turn.attributes["source.content_hash"] == record_hash(records[0])
    assert "source.byte_offset" not in turn.attributes

    read_tool = anchored[1]
    assert read_tool.name == "Read"
    assert read_tool.attributes["source.kind"] == "json_doc"
    assert read_tool.attributes["source.content_hash"] == record_hash(records[2])
    assert "source.byte_offset" not in read_tool.attributes

    edit_tool = anchored[2]
    assert edit_tool.name == "Edit"
    assert edit_tool.attributes["source.kind"] == "json_doc"
    assert edit_tool.attributes["source.content_hash"] == record_hash(records[4])
    assert "source.byte_offset" not in edit_tool.attributes

    # failed_command.json
    cmd_data = _fixture_bytes("failed_command.json")
    cmd_records = json.loads(cmd_data)
    _, cmd_spans = parse_roo(cmd_data, trace_id="local:roo-code:cmd-test")
    cmd_anchored = [s for s in cmd_spans if s.name.startswith("Turn ") or s.attributes.get("tool.name")]
    assert len(cmd_anchored) == 3  # Turn 1, Bash 1, Bash 2

    assert cmd_anchored[0].attributes["source.kind"] == "json_doc"
    assert cmd_anchored[0].attributes["source.content_hash"] == record_hash(cmd_records[0])
    assert cmd_anchored[1].attributes["source.kind"] == "json_doc"
    assert cmd_anchored[1].attributes["source.content_hash"] == record_hash(cmd_records[2])
    assert cmd_anchored[2].attributes["source.kind"] == "json_doc"
    assert cmd_anchored[2].attributes["source.content_hash"] == record_hash(cmd_records[5])

    # api_history.json
    api_data = _fixture_bytes("api_history.json")
    api_records = json.loads(api_data)
    _, api_spans = parse_roo(api_data, trace_id="local:roo-code:api-test")
    api_anchored = [s for s in api_spans if s.name.startswith("Turn ") or s.attributes.get("tool.name")]
    assert len(api_anchored) == 3  # Turn 1, ListDir, Read

    assert api_anchored[0].attributes["source.kind"] == "json_doc"
    assert api_anchored[0].attributes["source.content_hash"] == record_hash(api_records[0])
    assert api_anchored[1].attributes["source.kind"] == "json_doc"
    assert api_anchored[1].attributes["source.content_hash"] == record_hash(api_records[1])
    assert api_anchored[2].attributes["source.kind"] == "json_doc"
    assert api_anchored[2].attributes["source.content_hash"] == record_hash(api_records[3])


def test_roo_parser_handles_json_assistant_response_with_non_string_text():
    session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": "Process request"},
        # Assistant say: text where "text" inside JSON is an integer
        {
            "ts": 1730000001000,
            "type": "say",
            "say": "text",
            "text": json.dumps({"text": 123}),
        },
        # Assistant say: text where "text" inside JSON is a dict
        {
            "ts": 1730000002000,
            "type": "say",
            "say": "text",
            "text": json.dumps({"text": {"status": "success", "code": 0}}),
        },
        # Assistant ask: followup where "question" inside JSON is a list
        {
            "ts": 1730000003000,
            "type": "ask",
            "ask": "followup",
            "text": json.dumps({"question": ["option_a", "option_b"]}),
        },
        # Assistant say: text where text field is directly a dict
        {
            "ts": 1730000004000,
            "type": "say",
            "say": "text",
            "text": {"text": {"nested": True}},
        },
    ]
    raw_bytes = json.dumps(session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:non-string-text")
    turns = [s for s in spans if s.name.startswith("Turn ")]
    assert len(turns) == 1
    turn = turns[0]
    out_val = turn.attributes["output.value"]
    expected_output = (
        "123\n"
        '{"status": "success", "code": 0}\n'
        '["option_a", "option_b"]\n'
        '{"nested": true}'
    )
    assert out_val == expected_output
    assert '"text":' not in out_val
    assert '"question":' not in out_val


def test_roo_parser_handles_empty_question_falls_through_to_text():
    session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": "Start task"},
        # Branch 1: raw_text is a JSON string with question: "" and non-empty text
        {
            "ts": 1730000001000,
            "type": "say",
            "say": "text",
            "text": json.dumps({"question": "", "text": "Valid text from JSON string"}),
        },
        # Branch 2: raw_text is a dict with question: "" and non-empty text
        {
            "ts": 1730000002000,
            "type": "say",
            "say": "text",
            "text": {"question": "", "text": "Valid text from direct dict"},
        },
        # Empty question falling through to non-string text (preserving serialization)
        {
            "ts": 1730000003000,
            "type": "ask",
            "ask": "followup",
            "text": json.dumps({"question": "", "text": 456}),
        },
        # Non-string question is not treated as empty-string (serialization preserved)
        {
            "ts": 1730000004000,
            "type": "ask",
            "ask": "followup",
            "text": json.dumps({"question": 0, "text": "Ignored text"}),
        },
    ]
    raw_bytes = json.dumps(session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:empty-question-fallback")
    turns = [s for s in spans if s.name.startswith("Turn ")]
    assert len(turns) == 1
    out_val = turns[0].attributes["output.value"]
    assert "Valid text from JSON string" in out_val
    assert "Valid text from direct dict" in out_val
    assert "456" in out_val
    assert "0" in out_val
    assert "Ignored text" not in out_val


def test_roo_parser_accumulates_cache_writes_matching_cost_events():
    session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": "Execute task"},
        {
            "ts": 1730000001000,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps({
                "tokensIn": 1000,
                "tokensOut": 150,
                "cacheReads": 200,
                "cacheWrites": 350,
                "model": "claude-3-7-sonnet",
            }),
        },
        {
            "ts": 1730000002000,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps({
                "tokensIn": 800,
                "tokensOut": 100,
                "cacheReads": 100,
                "cacheWrites": 250,
                "model": "claude-3-7-sonnet",
            }),
        },
        {"ts": 1730000003000, "type": "say", "say": "text", "text": "Finished task"},
    ]
    raw_bytes = json.dumps(session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:cache-writes")
    turn = spans[1]
    assert turn.name == "Turn 1"
    assert turn.attributes["llm.token_count.cache_write"] == 600
    assert turn.attributes["llm.token_count.input_fresh"] == 900
    assert (
        turn.attributes["llm.token_count.input_fresh"]
        + turn.attributes["llm.token_count.cache_read"]
        + turn.attributes["llm.token_count.cache_write"]
        == turn.attributes["llm.token_count.prompt"]
    )
    cost_events = turn.attributes["llm.cost_events"]
    assert len(cost_events) == 2
    assert cost_events[0]["input_tokens"] == 450
    assert cost_events[0]["cache_read_tokens"] == 200
    assert cost_events[0]["cache_write_5m_tokens"] == 350
    assert (
        cost_events[0]["input_tokens"]
        + cost_events[0]["cache_read_tokens"]
        + cost_events[0]["cache_write_5m_tokens"]
        == 1000
    )
    assert cost_events[1]["input_tokens"] == 450
    assert cost_events[1]["cache_read_tokens"] == 100
    assert cost_events[1]["cache_write_5m_tokens"] == 250
    assert (
        cost_events[1]["input_tokens"]
        + cost_events[1]["cache_read_tokens"]
        + cost_events[1]["cache_write_5m_tokens"]
        == 800
    )
    assert turn.attributes["llm.token_count.cache_write"] == sum(
        e["cache_write_5m_tokens"] for e in cost_events
    )


def test_roo_parser_nonzero_cache_writes_reconcile_request_totals_and_session_metrics():
    session = [
        {"ts": 1730000000000, "type": "say", "say": "task", "text": "Run analysis"},
        {
            "ts": 1730000001000,
            "type": "say",
            "say": "api_req_started",
            "text": json.dumps({
                "tokensIn": 1000,
                "tokensOut": 150,
                "cacheReads": 200,
                "cacheWrites": 300,
                "model": "claude-3-7-sonnet",
            }),
        },
        {"ts": 1730000002000, "type": "say", "say": "text", "text": "Analysis complete"},
    ]
    raw_bytes = json.dumps(session).encode("utf-8")
    trace, spans = parse_roo(raw_bytes, trace_id="local:roo-code:reconcile-cache-writes")
    turn = spans[1]
    assert turn.name == "Turn 1"

    # Turn token counts
    assert turn.attributes["llm.token_count.prompt"] == 1000
    assert turn.attributes["llm.token_count.completion"] == 150
    assert turn.attributes["llm.token_count.cache_read"] == 200
    assert turn.attributes["llm.token_count.cache_write"] == 300
    assert turn.attributes["llm.token_count.input_fresh"] == 500
    assert (
        turn.attributes["llm.token_count.input_fresh"]
        + turn.attributes["llm.token_count.cache_read"]
        + turn.attributes["llm.token_count.cache_write"]
        == turn.attributes["llm.token_count.prompt"]
    )

    # Request cost-event totals reconcile with tokensIn
    cost_events = turn.attributes["llm.cost_events"]
    assert len(cost_events) == 1
    ev = cost_events[0]
    assert ev["input_tokens"] == 500
    assert ev["cache_read_tokens"] == 200
    assert ev["cache_write_5m_tokens"] == 300
    assert ev["input_tokens"] + ev["cache_read_tokens"] + ev["cache_write_5m_tokens"] == 1000

    # Aggregate session metrics reconcile
    metrics = build_session_metrics(spans)
    assert metrics["input_tokens"] == 1000
    assert metrics["input_fresh"] == 500
    assert metrics["cache_read_tokens"] == 200
    assert metrics["cache_write_tokens"] == 300
    assert (
        metrics["input_fresh"] + metrics["cache_read_tokens"] + metrics["cache_write_tokens"]
        == metrics["input_tokens"]
    )

    row = metrics["model_usage"][0]
    assert row["input_tokens"] == 500
    assert row["cache_read_tokens"] == 200
    assert row["cache_write_5m_tokens"] == 300
    assert row["input_tokens"] + row["cache_read_tokens"] + row["cache_write_5m_tokens"] == 1000
