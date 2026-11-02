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

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.parsers.cline import parse_cline

from .test_parser_robustness import WRONG_TYPES, _assign, _paths

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
        # 12 shell calls, 24 commands: a call that runs several commands is one span each.
        ("failed_shell", 3, ["Bash"] * 24),
        (
            "read_search_edit",
            1,
            ["Read", "Bash", "Bash", "Bash", "Grep", "Read", "Grep", "Bash", "Read", "Grep", "Bash"]
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


@pytest.mark.parametrize("args", [{"command": "ls"}, {"commands": ["ls", "pwd"]}])
def test_call_whose_result_is_missing_is_unset(args):
    _, spans = parse_cline(_doc(_call("run_commands", args)), "local:cline:x")
    assert {s.status_code for s in _tools(spans)} == {"UNSET"}


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


def test_a_single_shell_command_becomes_a_command_string():
    assert _tool_input("run_commands", {"command": "ls -la", "timeout": 5}) == (
        "Bash",
        {"command": "ls -la", "timeout": 5},
    )
    assert _tool_input("run_commands", {"commands": ["ls"]}) == ("Bash", {"command": "ls"})
    assert _tool_input("shell", {"commands": [{"command": "pwd"}]}) == ("Bash", {"command": "pwd"})
    assert _tool_input("shell", {"cmd": "pwd"}) == ("Bash", {"command": "pwd"})


def _shell_call(commands: list[str], content, **result_extra) -> list:
    block = {"type": "tool_result", "tool_use_id": "c1", "name": "run_commands", "content": content, **result_extra}
    result = {"id": "r1", "role": "user", "ts": 1900, "content": [block]}
    _, spans = parse_cline(_doc(_call("run_commands", {"commands": commands, "timeout": 5}), result), "local:cline:x")
    return _tools(spans)


def test_a_multi_command_call_becomes_one_span_per_command():
    commands = ["git add -A", "git commit -m x", "git push"]
    items = [{"query": c, "result": "o", "success": ok} for c, ok in zip(commands, (True, False, True), strict=True)]
    tools = _shell_call(commands, items)
    assert [json.loads(t.attributes["input.value"]) for t in tools] == [{"command": c, "timeout": 5} for c in commands]
    # Each command takes its own result item.
    assert [t.status_code for t in tools] == ["OK", "ERROR", "OK"]
    assert len({t.span_id for t in tools}) == 3
    # The call's time (1.0s to 1.9s) is shared out with no gap and no overlap.
    assert tools[0].start_time == datetime.fromtimestamp(1.0, UTC)
    assert tools[-1].end_time == datetime.fromtimestamp(1.9, UTC)
    assert all(a.end_time == b.start_time for a, b in zip(tools, tools[1:], strict=False))


def test_a_refused_multi_command_call_fails_every_command():
    tools = _shell_call(["ls", "pwd"], '{"error":"refused"}', is_error=True)
    assert [t.status_code for t in tools] == ["ERROR", "ERROR"]


def test_results_that_do_not_line_up_with_the_commands_are_shared():
    tools = _shell_call(["ls", "pwd"], [{"query": "ls", "result": "o", "success": False}])
    assert [t.status_code for t in tools] == ["ERROR", "ERROR"]


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


@pytest.mark.parametrize("name", ("edit_and_read", "failed_shell"))
def test_wrong_typed_fields_never_raise(name):
    base = json.loads(_fixture(name))
    failures = []
    for path in _paths(base):
        for bad in WRONG_TYPES:
            doc = copy.deepcopy(base)
            _assign(doc, path, bad)
            try:
                parse_cline(json.dumps(doc).encode(), "t")
            except Exception as exc:
                failures.append(f"{path} = {bad!r}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures[:25])


@pytest.mark.parametrize("model_info", ["x", ["x"], 3, {"id": 3}])
def test_malformed_model_info_is_ignored(model_info):
    msg = {**_call("run_commands", {"command": "ls"}), "modelInfo": model_info}
    _, spans = parse_cline(_doc(msg), "local:cline:x")
    assert spans[0].attributes["llm.model_name"] == ""
    assert len(_tools(spans)) == 1


@pytest.mark.parametrize("data", [b"", b"not json", b"[]", b'{"messages": 3}', b'{"messages": [1, "x", null]}'])
def test_malformed_or_empty_input_yields_no_spans(data):
    trace, spans = parse_cline(data, "local:cline:x")
    assert spans == [] and trace.trace_id == "local:cline:x"
