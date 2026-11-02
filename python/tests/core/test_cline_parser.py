"""Cline parser.

The fixtures under ``fixtures/cline/`` are real Cline sessions
(``~/.cline/data/sessions/<id>/<id>.messages.json``) with every prompt, reply,
thought, path, file content, command argument and tool output replaced by a
placeholder. What survives is the structure: record shapes, ids (renumbered),
timestamps, model, token metrics, tool names, argument keys, shell program
names, and the ``success`` / ``is_error`` flags.

* ``edit_and_read``: two prompts; a three-file read where one file is missing,
  shell commands, and a text replacement.
* ``failed_shell``: three prompts; shell calls that fail (a non-zero exit in one
  item, a refused call with ``is_error``) among ones that succeed.
* ``read_search_edit``: one prompt; reads, codebase searches, shell commands
  and four edits to one file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.parsers.cline import parse_cline

FIXTURES = Path(__file__).parent / "fixtures" / "cline"
NAMES = ("edit_and_read", "failed_shell", "read_search_edit")


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.messages.json").read_bytes()


def _parse(name: str):
    _, spans = parse_cline(_fixture(name), f"local:cline:{name}")
    return spans


def _turns(spans):
    return [s for s in spans if s.name.startswith("Turn ")]


def _tools(spans):
    return [s for s in spans if ":tool:" in s.span_id]


def _doc(*messages: dict) -> bytes:
    return json.dumps({"version": 1, "sessionId": "s1", "messages": list(messages)}).encode()


def _call(name: str, args: dict, call_id: str = "c1") -> dict:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": args}
    return {"id": "a1", "role": "assistant", "ts": 1000, "content": [block]}


def _tool_input(name: str, args: dict) -> tuple[str, dict]:
    _, spans = parse_cline(_doc(_call(name, args)), "local:cline:x")
    (tool,) = _tools(spans)
    return tool.name, json.loads(tool.attributes["input.value"])


@pytest.mark.parametrize(
    ("name", "prompts", "tools"),
    [
        ("edit_and_read", 2, ["Read", "Bash", "Edit", "Bash", "Bash"]),
        ("failed_shell", 3, ["Bash"] * 12),
        (
            "read_search_edit",
            1,
            ["Read", "Bash", "Grep", "Read", "Grep", "Bash", "Read", "Grep", "Bash"]
            + ["Read", "Read", "Read", "Edit", "Read", "Edit", "Edit", "Read", "Edit", "Bash"],
        ),
    ],
)
def test_turn_and_tool_counts(name, prompts, tools):
    spans = _parse(name)
    assert spans[0].name == "Session" and spans[0].parent_id is None
    turns = _turns(spans)
    assert len(turns) == prompts
    assert [s.name for s in _tools(spans)] == tools
    assert all(s.attributes["tool.name"] == s.name for s in _tools(spans))
    assert {s.parent_id for s in _tools(spans)} <= {t.span_id for t in turns}


def test_failed_shell_calls_are_errors_and_the_rest_are_not():
    statuses = [s.status_code for s in _tools(_parse("failed_shell"))]
    # One item exiting non-zero, and one call refused with `is_error`.
    assert statuses.count("ERROR") == 2
    assert set(statuses) == {"OK", "ERROR"}
    failed = next(s for s in _tools(_parse("failed_shell")) if s.status_code == "ERROR")
    assert failed.attributes["code.edit_evidence"]["status"] == "not_successful"


def test_a_read_with_one_missing_file_is_an_error():
    read = _tools(_parse("edit_and_read"))[0]
    assert (read.name, read.status_code) == ("Read", "ERROR")


def test_call_whose_result_is_missing_is_unset():
    _, spans = parse_cline(_doc(_call("run_commands", {"command": "ls"})), "local:cline:x")
    assert _tools(spans)[0].status_code == "UNSET"


@pytest.mark.parametrize("name", NAMES)
def test_tokens_are_counted_once_per_request(name):
    messages = json.loads(_fixture(name))["messages"]
    metrics = [m["metrics"] for m in messages if "metrics" in m]
    spans = _parse(name)
    events = [e for t in _turns(spans) for e in t.attributes["llm.cost_events"]]
    assert sum(e["output_tokens"] for e in events) == sum(m["outputTokens"] for m in metrics)
    assert sum(e["cache_read_tokens"] for e in events) == sum(m["cacheReadTokens"] for m in metrics)
    # `inputTokens` includes the cached tokens, so fresh + cache adds back up.
    assert sum(e["input_tokens"] + e["cache_read_tokens"] + e["cache_write_5m_tokens"] for e in events) == sum(
        m["inputTokens"] for m in metrics
    )
    assert sum(t.attributes["llm.token_count.prompt"] for t in _turns(spans)) == sum(m["inputTokens"] for m in metrics)


def test_model_and_session_id():
    root = _parse("failed_shell")[0]
    assert root.attributes["llm.model_name"] == "cline-free/mimo-v2.6-flash"
    assert root.attributes["harness.agent_session_id"] == "fixture-failed_shell"


def test_shell_commands_become_one_command_string():
    assert _tool_input("run_commands", {"commands": ["git status", "ls"], "timeout": 5}) == (
        "Bash",
        {"command": "git status\nls", "timeout": 5},
    )
    assert _tool_input("run_commands", {"command": "ls -la"}) == ("Bash", {"command": "ls -la"})
    assert _tool_input("shell", {"commands": [{"command": "pwd"}]}) == ("Bash", {"command": "pwd"})


def test_read_takes_its_file_path_from_the_first_file():
    name, args = _tool_input("read_files", {"files": [{"path": "/a.py"}, "/b.py"]})
    assert (name, list(args)[0], args["file_path"]) == ("Read", "file_path", "/a.py")


def test_editor_with_old_text_is_an_edit_and_without_it_a_write():
    edit = _tool_input("editor", {"path": "/a.py", "old_text": "\nx\n", "new_text": "\ny\n"})
    assert edit == ("Edit", {"file_path": "/a.py", "old_string": "\nx\n", "new_string": "\ny\n"})
    write = _tool_input("editor", {"path": "/a.py", "new_text": "one\ntwo"})
    assert write == ("Write", {"file_path": "/a.py", "content": "one\ntwo"})
    # An insert has no old text but is not a whole-file write.
    assert _tool_input("editor", {"path": "/a.py", "new_text": "x", "insert_line": 3})[0] == "Edit"


def test_write_and_edit_record_edit_evidence():
    result = {
        "id": "u1",
        "role": "user",
        "ts": 2000,
        "content": [{"type": "tool_result", "tool_use_id": "c1", "name": "editor", "content": "ok"}],
    }
    _, spans = parse_cline(_doc(_call("editor", {"path": "/a.py", "new_text": "one\ntwo"}), result), "local:cline:x")
    (write,) = _tools(spans)
    evidence = write.attributes["code.edit_evidence"]
    assert (evidence["status"], evidence["edits"][0]["operation"], evidence["edits"][0]["path"]) == (
        "captured",
        "write",
        "/a.py",
    )


@pytest.mark.parametrize("data", [b"", b"not json", b"[]", b'{"messages": 3}', b'{"messages": [1, "x", null]}'])
def test_malformed_or_empty_input_yields_no_spans(data):
    trace, spans = parse_cline(data, "local:cline:x")
    assert spans == [] and trace.trace_id == "local:cline:x"
