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


def test_windsurf_camel_case_detection():
    assert detect_agent(b'{"type": "windsurfSession", "cascadeId": "c-1"}\n') == AgentType.WINDSURF
    assert detect_agent(b'{"type": "cascadeSession", "agentId": "a-1"}\n') == AgentType.WINDSURF
    assert detect_agent(b'{"stepId": 1, "stepType": 19, "userInput": "hi"}\n') == AgentType.WINDSURF
    assert detect_agent(b'{"cascadeId": "c-789", "title": "My Cascade"}\n') == AgentType.WINDSURF

    # Pretty-printed multi-line JSON document
    pretty = b'{\n  "cascadeId": "c-pretty",\n  "steps": [\n    {"stepId": 1, "stepType": 19}\n  ]\n}'
    assert detect_agent(pretty) == AgentType.WINDSURF


def test_windsurf_turn_closing_and_header_bounds():
    from datetime import datetime, UTC

    lines = [
        json.dumps({
            "type": "windsurf_session",
            "agentId": "ws-bounds",
            "createdAt": "2026-03-12T14:00:00Z",
            "lastModifiedAt": "2026-03-12T14:30:00Z",
            "cwd": "/workspace/app",
        }),
        # Turn 1: 14:00:05 to 14:00:10
        json.dumps({
            "step_id": 1,
            "step_type": 19,
            "timestamp": "2026-03-12T14:00:05Z",
            "user_input": "Start task",
        }),
        json.dumps({
            "step_id": 2,
            "step_type": 20,
            "timestamp": "2026-03-12T14:00:10Z",
            "visible": "Step 1 done",
        }),
        # Turn 2 starts 10 minutes later: 14:10:00 to 14:10:15
        json.dumps({
            "step_id": 3,
            "step_type": 19,
            "timestamp": "2026-03-12T14:10:00Z",
            "user_input": "Next task",
        }),
        json.dumps({
            "step_id": 4,
            "step_type": 20,
            "timestamp": "2026-03-12T14:10:15Z",
            "visible": "Step 2 done",
        }),
        json.dumps({
            "step_id": 5,
            "step_type": 2,  # STEP_TYPE_FINISH
            "timestamp": "2026-03-12T14:10:15Z",
        }),
    ]
    raw_data = "\n".join(lines).encode()
    trace, spans = parse_windsurf(raw_data, trace_id="local:windsurf:bounds-1")

    # Header bounds preserved on root span
    root = spans[0]
    assert root.start_time == datetime(2026, 3, 12, 14, 0, 0, tzinfo=UTC)
    assert root.end_time == datetime(2026, 3, 12, 14, 30, 0, tzinfo=UTC)

    # Turn 1 closed at its last event (14:00:10), NOT stretched to Turn 2's start (14:10:00)
    turn1 = next(s for s in spans if s.name == "Turn 1")
    assert turn1.start_time == datetime(2026, 3, 12, 14, 0, 5, tzinfo=UTC)
    assert turn1.end_time == datetime(2026, 3, 12, 14, 0, 10, tzinfo=UTC)

    # Turn 2 closed at finish (14:10:15)
    turn2 = next(s for s in spans if s.name == "Turn 2")
    assert turn2.start_time == datetime(2026, 3, 12, 14, 10, 0, tzinfo=UTC)
    assert turn2.end_time == datetime(2026, 3, 12, 14, 10, 15, tzinfo=UTC)


def test_windsurf_recursion_error_handling(monkeypatch):
    import json

    def mock_loads(s, *args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(json, "loads", mock_loads)
    # Should safely fail without crashing
    trace, spans = parse_windsurf(b'{"steps": []}', trace_id="local:windsurf:rec-1")
    assert spans == []


def test_local_source_windsurf_single_document_export(tmp_path):
    from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

    ws_dir = tmp_path / "windsurf_home"
    cascade_dir = ws_dir / "cascade"
    cascade_dir.mkdir(parents=True)

    # Write a pretty-printed single-document export
    doc = {
        "cascadeId": "cascade-export-pretty-1",
        "workspacePath": str(tmp_path / "project_alpha"),
        "steps": [
            {
                "stepId": 1,
                "stepType": 19,
                "timestamp": "2026-03-12T14:00:00Z",
                "userInput": "Find files",
            },
            {
                "stepId": 2,
                "stepType": 20,
                "timestamp": "2026-03-12T14:00:05Z",
                "visible": "Found files.",
            },
        ],
    }
    export_file = cascade_dir / "session.json"
    export_file.write_text(json.dumps(doc, indent=2))

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        windsurf_homes=[ws_dir],
    )

    sessions = source.discover_sessions()
    ws_sessions = [s for s in sessions if "cascade-export-pretty-1" in s.session_id]
    assert len(ws_sessions) == 1
    assert ws_sessions[0].session_id == "local:windsurf:cascade-export-pretty-1"

    assert source.get_agent_type(ws_sessions[0].session_id) == "windsurf"
    assert source.get_project_name(ws_sessions[0].session_id) == "project_alpha"

    spans = source.get_spans_by_session(ws_sessions[0].session_id)
    assert spans[0].name == "Session (windsurf)"
    assert spans[0].attributes["project.name"] == "project_alpha"
    assert len(spans) == 2  # Root and Turn 1


def test_windsurf_top_level_tool_with_cascade_id():
    """Verify that a JSONL record with cascade_id and top-level toolName is not classified as session metadata."""
    lines = [
        json.dumps({
            "type": "cascade_session",
            "cascade_id": "c-top-tool-1",
            "cwd": "/app",
        }),
        json.dumps({
            "user_input": "Run test command",
        }),
        # Top-level tool record with cascade_id but no step_id or tool_calls
        json.dumps({
            "cascade_id": "c-top-tool-1",
            "toolName": "run_command",
            "params": {"command": "pytest"},
            "result": "1 passed",
        }),
    ]
    raw_data = "\n".join(lines).encode()
    trace, spans = parse_windsurf(raw_data, trace_id="local:windsurf:c-top-tool-1")

    tool_spans = [s for s in spans if s.name == "Bash"]
    assert len(tool_spans) == 1
    assert tool_spans[0].attributes["tool.name"] == "Bash"
    assert "pytest" in tool_spans[0].attributes["input.value"]
    assert "1 passed" in tool_spans[0].attributes["output.value"]


def test_windsurf_large_json_export_over_5mb(tmp_path):
    """Verify that a Windsurf JSON export > 5 MB is read completely and indexed without data loss."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

    ws_dir = tmp_path / "windsurf_home"
    cascade_dir = ws_dir / "cascade"
    cascade_dir.mkdir(parents=True)

    # Construct an export > 5.5 MB
    big_chunk = "x" * (6 * 1024 * 1024)
    doc = {
        "cascadeId": "cascade-big-export",
        "workspacePath": str(tmp_path / "big_project"),
        "steps": [
            {
                "stepId": 1,
                "stepType": 19,
                "timestamp": "2026-03-12T14:00:00Z",
                "userInput": "Start big job",
            },
            {
                "stepId": 2,
                "stepType": 20,
                "timestamp": "2026-03-12T14:00:05Z",
                "visible": big_chunk,
            },
        ],
    }
    export_file = cascade_dir / "big_session.json"
    export_file.write_text(json.dumps(doc))
    assert export_file.stat().st_size > 5_000_000

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        windsurf_homes=[ws_dir],
    )
    sessions = source.discover_sessions()
    ws_sessions = [s for s in sessions if "cascade-big-export" in s.session_id]
    assert len(ws_sessions) == 1
    assert ws_sessions[0].session_id == "local:windsurf:cascade-big-export"

    spans = source.get_spans_by_session(ws_sessions[0].session_id)
    assert len(spans) >= 2
    turn = next(s for s in spans if s.name == "Turn 1")
    assert len(turn.attributes["output.value"]) == 2000
    assert turn.attributes["output.value"] == "x" * 2000


def test_windsurf_deeply_nested_json_recursion_safe(tmp_path):
    """Verify that a deeply nested JSON document does not crash indexing with RecursionError."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

    ws_dir = tmp_path / "windsurf_home"
    cascade_dir = ws_dir / "cascade"
    cascade_dir.mkdir(parents=True)

    # Build deeply nested brackets
    depth = 3000
    nested_str = ("[" * depth) + "1" + ("]" * depth)
    bad_file = cascade_dir / "bad_nested.json"
    bad_file.write_text(nested_str)

    # Also test detect_agent directly
    assert detect_agent(nested_str.encode()) is None

    # Discover sessions should catch the recursion error and continue
    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        windsurf_homes=[ws_dir],
    )
    sessions = source.discover_sessions()
    # It safely didn't crash and skipped the corrupt file
    assert not any("bad_nested" in s.session_id for s in sessions)
    assert source.discovery_stats().get("windsurf", {}).get("skipped", {}).get("detection_failed") == 1


def test_windsurf_generic_steps_list_not_detected():
    """Verify that a non-Windsurf JSON file with generic steps is not detected as Windsurf."""
    # Empty steps without Cascade marker
    assert detect_agent(b'{"steps": []}') is None
    assert detect_agent(b'{\n  "steps": []\n}') is None

    # Generic steps without Cascade step markers
    generic_workflow = b'{\n  "steps": [\n    {"name": "Checkout", "uses": "actions/checkout@v3"}\n  ]\n}'
    assert detect_agent(generic_workflow) is None


def test_windsurf_empty_steps_export_skipped(tmp_path):
    """Verify that a Windsurf JSON export with steps: [] is skipped with no_conversation."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

    ws_dir = tmp_path / "windsurf_home"
    cascade_dir = ws_dir / "cascade"
    cascade_dir.mkdir(parents=True)

    doc = {
        "cascadeId": "cascade-empty-steps-1",
        "workspacePath": str(tmp_path / "empty_project"),
        "steps": [],
    }
    empty_file = cascade_dir / "empty_session.json"
    empty_file.write_text(json.dumps(doc))

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        windsurf_homes=[ws_dir],
    )
    sessions = source.discover_sessions()
    assert not any("cascade-empty-steps-1" in s.session_id for s in sessions)
    assert source.discovery_stats().get("windsurf", {}).get("skipped", {}).get("no_conversation") == 1


def test_windsurf_role_based_trajectory_steps_export(tmp_path):
    """Verify that role-based trajectorySteps exports without step IDs are recognized and parsed."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

    ws_dir = tmp_path / "windsurf_home"
    cascade_dir = ws_dir / "cascade"
    cascade_dir.mkdir(parents=True)

    doc = {
        "trajectorySteps": [
            {
                "role": "user",
                "content": "Please explain recursion",
            },
            {
                "role": "assistant",
                "content": "Recursion is when a function calls itself.",
            },
        ],
    }
    export_file = cascade_dir / "role_session.json"
    export_file.write_text(json.dumps(doc))

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        windsurf_homes=[ws_dir],
    )
    sessions = source.discover_sessions()
    assert len(sessions) == 1
    assert "role_session" in sessions[0].session_id

    spans = source.get_spans_by_session(sessions[0].session_id)
    assert len(spans) == 2  # Root + Turn 1
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.attributes["input.value"] == "Please explain recursion"
    assert "Recursion is when a function calls itself." in turn.attributes["output.value"]


def test_windsurf_header_only_jsonl_skipped(tmp_path):
    """Verify that a Windsurf JSONL file with only a session header (no conversation steps) is skipped."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

    ws_dir = tmp_path / "windsurf_home"
    traj_dir = ws_dir / "trajectories"
    traj_dir.mkdir(parents=True)

    header_only_file = traj_dir / "empty_header_session.jsonl"
    header_only_file.write_text(
        json.dumps({
            "type": "windsurf_session",
            "agentId": "empty-header-session",
            "createdAt": "2026-03-12T14:00:00Z",
            "cwd": "/path/to/project",
        })
        + "\n"
    )

    source = LocalSource(
        claude_code_homes=[],
        codex_homes=[],
        windsurf_homes=[ws_dir],
    )
    sessions = source.discover_sessions()
    assert not any("empty-header-session" in s.session_id for s in sessions)
    assert source.discovery_stats().get("windsurf", {}).get("skipped", {}).get("no_conversation") == 1


