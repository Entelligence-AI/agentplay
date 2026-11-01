"""Tests for Roo Code session detection and parsing."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from agentplay import builder, facts, search
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    AgentType,
    detect_agent,
    parse_roo,
)
from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

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
    assert tools[0].name == "LS"
    assert tools[0].status_code == "OK"
    assert tools[1].name == "Read"
    assert tools[1].status_code == "OK"


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
