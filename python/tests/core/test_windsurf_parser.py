"""Windsurf (Cascade) parser tests.

Tests cover:
1. Agent detection from session headers and trajectory step records.
2. Turning NDJSON steps into a universal span tree (Turn spans, Tool spans).
3. Tool taxonomy mapping (read_file -> Read, run_command -> Bash, edit -> Edit, etc.).
4. Failed tool calls and error status propagation.
5. Path argument normalization (`filePath`, `target_file`, `uri` -> `file_path`).
6. Single JSON document format with top-level `steps` list.
7. Redaction contract (truncation of large input/output values).
"""

from __future__ import annotations

import json

from agentplay._core.ingestion.coding_agent._sources.local.parsers.base import (
    AgentType,
    detect_agent,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.windsurf import (
    parse_windsurf,
)


def test_windsurf_detection():
    # Header format
    header = b'{"type": "windsurf_session", "agentId": "ws-123", "cascade_id": "c-1"}\n'
    assert detect_agent(header) == AgentType.WINDSURF

    # Step format
    step = b'{"step_id": 1, "step_type": 19, "timestamp": "2026-03-12T14:30:45Z", "user_input": "hello"}\n'
    assert detect_agent(step) == AgentType.WINDSURF

    # Tool call step
    tool_step = b'{"step_id": 2, "tool_calls": [{"tool_id": "t1", "tool_name": "read_file"}]}\n'
    assert detect_agent(tool_step) == AgentType.WINDSURF

    # Cascade metadata line
    cascade_meta = b'{"cascade_id": "554700e0-194b-4f64-84ac-045a9c42971a", "title": "My session"}\n'
    assert detect_agent(cascade_meta) == AgentType.WINDSURF


def test_parse_windsurf_ndjson():
    lines = [
        json.dumps({
            "type": "windsurf_session",
            "agentId": "test-session-1",
            "createdAt": "2026-03-12T14:30:00Z",
            "cwd": "/path/to/project",
            "model": "claude-3-7-sonnet",
        }),
        json.dumps({
            "step_id": 1,
            "step_type": 19,
            "timestamp": "2026-03-12T14:30:01Z",
            "user_input": "Please check main.py and fix the bug",
        }),
        json.dumps({
            "step_id": 2,
            "step_type": 20,
            "timestamp": "2026-03-12T14:30:02Z",
            "thinking": "I should read main.py first to inspect the issue.",
            "visible": "I will read main.py to inspect the code.",
            "provider": "anthropic",
        }),
        json.dumps({
            "step_id": 3,
            "step_type": 3,
            "timestamp": "2026-03-12T14:30:03Z",
            "tool_calls": [
                {
                    "tool_id": "call-read-1",
                    "tool_name": "read_file",
                    "params": {"file_path": "main.py"},
                }
            ],
        }),
        json.dumps({
            "step_id": 4,
            "step_type": 21,
            "timestamp": "2026-03-12T14:30:04Z",
            "tool_id": "call-read-1",
            "tool_result": "def main():\n    pass\n",
            "is_error": False,
        }),
        json.dumps({
            "step_id": 5,
            "step_type": 3,
            "timestamp": "2026-03-12T14:30:05Z",
            "tool_calls": [
                {
                    "tool_id": "call-edit-1",
                    "tool_name": "edit",
                    "params": {
                        "file_path": "main.py",
                        "old_string": "    pass\n",
                        "new_string": "    print('hello world')\n",
                    },
                }
            ],
        }),
        json.dumps({
            "step_id": 6,
            "step_type": 21,
            "timestamp": "2026-03-12T14:30:06Z",
            "tool_id": "call-edit-1",
            "tool_result": "Success",
            "is_error": False,
        }),
        json.dumps({
            "step_id": 7,
            "step_type": 20,
            "timestamp": "2026-03-12T14:30:07Z",
            "visible": "Fixed the bug in main.py.",
        }),
    ]
    raw_data = "\n".join(lines).encode()

    trace, spans = parse_windsurf(raw_data, trace_id="local:windsurf:test-session-1")

    # Root span
    root = spans[0]
    assert root.span_id == "local:windsurf:test-session-1:root"
    assert root.name == "Session (windsurf)"
    assert root.attributes["source.surface"] == "windsurf"
    assert root.attributes["agent.name"] == "windsurf"
    assert root.attributes["cwd"] == "/path/to/project"
    assert root.attributes["project.name"] == "project"
    assert root.status_code == "OK"

    # Turn span
    turns = [s for s in spans if s.name == "Turn 1"]
    assert len(turns) == 1
    turn1 = turns[0]
    assert turn1.attributes["input.value"] == "Please check main.py and fix the bug"
    assert "Fixed the bug in main.py." in turn1.attributes["output.value"]
    assert turn1.attributes["llm.model_name"] == "anthropic"

    # Tool spans: Read and Edit
    tool_spans = [s for s in spans if s.parent_id == turn1.span_id]
    assert len(tool_spans) == 2
    assert tool_spans[0].name == "Read"
    assert tool_spans[0].attributes["tool.name"] == "Read"
    assert '"file_path":"main.py"' in tool_spans[0].attributes["input.value"]

    assert tool_spans[1].name == "Edit"
    assert tool_spans[1].attributes["tool.name"] == "Edit"
    assert '"file_path":"main.py"' in tool_spans[1].attributes["input.value"]


def test_parse_windsurf_failed_tool():
    lines = [
        json.dumps({
            "step_id": 1,
            "step_type": 19,
            "timestamp": "2026-03-12T14:30:01Z",
            "user_input": "Run tests",
        }),
        json.dumps({
            "step_id": 2,
            "step_type": 3,
            "timestamp": "2026-03-12T14:30:02Z",
            "tool_calls": [
                {
                    "tool_id": "call-bash-1",
                    "tool_name": "run_command",
                    "params": {"command": "pytest"},
                }
            ],
        }),
        json.dumps({
            "step_id": 3,
            "step_type": 21,
            "timestamp": "2026-03-12T14:30:03Z",
            "tool_id": "call-bash-1",
            "tool_result": "AssertionError: test failed",
            "is_error": True,
            "exit_code": 1,
        }),
    ]
    raw_data = "\n".join(lines).encode()

    trace, spans = parse_windsurf(raw_data, trace_id="local:windsurf:fail-1")
    root = spans[0]
    assert root.status_code == "ERROR"

    tool_spans = [s for s in spans if s.name == "Bash"]
    assert len(tool_spans) == 1
    assert tool_spans[0].status_code == "ERROR"
    assert "AssertionError" in tool_spans[0].attributes["output.value"]


def test_parse_windsurf_file_path_normalization():
    lines = [
        json.dumps({
            "step_id": 1,
            "role": "user",
            "content": "Check files",
            "timestamp": 1741761045000,
        }),
        json.dumps({
            "step_id": 2,
            "tool_calls": [
                {
                    "tool_id": "c1",
                    "tool_name": "view_file",
                    "params": {"filePath": "src/app.py"},
                },
                {
                    "tool_id": "c2",
                    "tool_name": "write_to_file",
                    "params": {"target_file": "output.txt", "content": "done"},
                },
            ],
            "timestamp": 1741761046000,
        }),
    ]
    raw_data = "\n".join(lines).encode()

    _, spans = parse_windsurf(raw_data, trace_id="local:windsurf:norm-1")
    tool_spans = [s for s in spans if s.span_kind == "INTERNAL"]
    assert len(tool_spans) == 2

    # view_file maps to Read with normalized file_path
    assert tool_spans[0].name == "Read"
    assert '"file_path":"src/app.py"' in tool_spans[0].attributes["input.value"]

    # write_to_file maps to Write with normalized file_path
    assert tool_spans[1].name == "Write"
    assert '"file_path":"output.txt"' in tool_spans[1].attributes["input.value"]


def test_parse_windsurf_json_doc():
    doc = {
        "cascade_id": "doc-session-123",
        "cwd": "/workspace/app",
        "steps": [
            {
                "step_id": 1,
                "step_type": 19,
                "timestamp": "2026-03-12T14:00:00Z",
                "user_input": "Find functions",
            },
            {
                "step_id": 2,
                "step_type": 3,
                "timestamp": "2026-03-12T14:00:05Z",
                "tool_calls": [
                    {
                        "tool_id": "grep-1",
                        "tool_name": "grep_search",
                        "params": {"query": "def test"},
                    }
                ],
            },
        ],
    }
    raw_data = json.dumps(doc).encode()

    trace, spans = parse_windsurf(raw_data, trace_id="local:windsurf:doc-session-123")
    assert len(spans) == 3  # Root, Turn 1, Grep tool
    root = spans[0]
    assert root.attributes["project.name"] == "app"
    assert spans[2].name == "Grep"


def test_windsurf_edit_evidence_captured():
    from hashlib import sha256

    lines = [
        json.dumps({
            "step_id": 1,
            "step_type": 19,
            "timestamp": "2026-03-12T14:00:00Z",
            "user_input": "Update code",
        }),
        json.dumps({
            "step_id": 2,
            "step_type": 3,
            "timestamp": "2026-03-12T14:00:01Z",
            "tool_calls": [
                {
                    "tool_id": "c1",
                    "tool_name": "edit",
                    "params": {
                        "file_path": "main.py",
                        "old_string": "\nold_line = 1\n",
                        "new_string": "\nnew_line = 2\n",
                    },
                }
            ],
        }),
        json.dumps({
            "step_id": 3,
            "step_type": 21,
            "timestamp": "2026-03-12T14:00:02Z",
            "tool_id": "c1",
            "tool_result": "Success",
            "is_error": False,
        }),
    ]
    raw_data = "\n".join(lines).encode()
    _, spans = parse_windsurf(raw_data, trace_id="local:windsurf:edit-1")
    tool = next(s for s in spans if s.name == "Edit")
    evidence = tool.attributes["code.edit_evidence"]
    assert evidence["status"] == "captured"
    assert len(evidence["edits"]) == 1
    assert evidence["edits"][0]["before"] == [sha256(b"old_line = 1").hexdigest()]
    assert evidence["edits"][0]["after"] == [sha256(b"new_line = 2").hexdigest()]


def test_windsurf_edit_evidence_failed():
    lines = [
        json.dumps({
            "step_id": 1,
            "step_type": 19,
            "timestamp": "2026-03-12T14:00:00Z",
            "user_input": "Update code",
        }),
        json.dumps({
            "step_id": 2,
            "step_type": 3,
            "timestamp": "2026-03-12T14:00:01Z",
            "tool_calls": [
                {
                    "tool_id": "c1",
                    "tool_name": "edit",
                    "params": {
                        "file_path": "main.py",
                        "old_string": "\nold_line = 1\n",
                        "new_string": "\nnew_line = 2\n",
                    },
                }
            ],
        }),
        json.dumps({
            "step_id": 3,
            "step_type": 21,
            "timestamp": "2026-03-12T14:00:02Z",
            "tool_id": "c1",
            "tool_result": "Failed to apply edit",
            "is_error": True,
        }),
    ]
    raw_data = "\n".join(lines).encode()
    _, spans = parse_windsurf(raw_data, trace_id="local:windsurf:edit-fail")
    tool = next(s for s in spans if s.name == "Edit")
    evidence = tool.attributes["code.edit_evidence"]
    assert evidence["status"] == "not_successful"


def test_windsurf_truncation_contract():
    secret_tail = "SECRET_TOKEN_DO_NOT_LEAK"
    large_input = "A" * 3000 + secret_tail
    large_output = "B" * 3000 + secret_tail

    lines = [
        json.dumps({
            "step_id": 1,
            "step_type": 19,
            "timestamp": "2026-03-12T14:00:00Z",
            "user_input": large_input,
        }),
        json.dumps({
            "step_id": 2,
            "step_type": 3,
            "timestamp": "2026-03-12T14:00:01Z",
            "tool_calls": [
                {
                    "tool_id": "c1",
                    "tool_name": "run_command",
                    "params": {"command": large_input},
                }
            ],
        }),
        json.dumps({
            "step_id": 3,
            "step_type": 21,
            "timestamp": "2026-03-12T14:00:02Z",
            "tool_id": "c1",
            "tool_result": large_output,
        }),
    ]
    raw_data = "\n".join(lines).encode()
    _, spans = parse_windsurf(raw_data, trace_id="local:windsurf:trunc-1")

    turn = next(s for s in spans if s.name.startswith("Turn "))
    assert len(turn.attributes["input.value"]) == 2000
    assert secret_tail not in turn.attributes["input.value"]

    tool = next(s for s in spans if s.name == "Bash")
    assert len(tool.attributes["input.value"]) == 2000
    assert len(tool.attributes["output.value"]) == 2000
    assert secret_tail not in tool.attributes["input.value"]
    assert secret_tail not in tool.attributes["output.value"]


def test_local_source_windsurf_indexing_and_parse(tmp_path):
    from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

    ws_dir = tmp_path / "windsurf_home"
    traj_dir = ws_dir / "trajectories"
    traj_dir.mkdir(parents=True)

    session_file = traj_dir / "ws-session-abc.jsonl"
    session_file.write_text(
        json.dumps({
            "type": "windsurf_session",
            "agentId": "ws-session-abc",
            "createdAt": "2026-03-12T14:00:00Z",
            "cwd": str(tmp_path / "my_project"),
        })
        + "\n"
        + json.dumps({
            "step_id": 1,
            "step_type": 19,
            "timestamp": "2026-03-12T14:00:01Z",
            "user_input": "hello windsurf",
        })
        + "\n"
    )

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        windsurf_homes=[ws_dir],
    )

    sessions = source.discover_sessions()
    ws_sessions = [s for s in sessions if "ws-session-abc" in s.session_id]
    assert len(ws_sessions) == 1
    assert ws_sessions[0].session_id == "local:windsurf:ws-session-abc"

    assert source.get_agent_type(ws_sessions[0].session_id) == "windsurf"
    assert source.get_project_name(ws_sessions[0].session_id) == "my_project"

    spans = source.get_spans_by_session(ws_sessions[0].session_id)
    assert spans[0].name == "Session (windsurf)"
    assert spans[0].attributes["agent.name"] == "windsurf"
    assert spans[0].attributes["project.name"] == "my_project"
    assert len(spans) == 2  # Root and Turn 1


def test_discover_windsurf_homes(monkeypatch, tmp_path):
    from agentplay.discover import Homes

    ws_dir = tmp_path / "codeium_windsurf"
    ws_dir.mkdir()

    monkeypatch.setenv("AGENTPLAY_WINDSURF_HOMES", str(ws_dir))
    homes = Homes.default()
    assert ws_dir in homes.windsurf
    assert "windsurf" in homes.present()
