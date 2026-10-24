"""Antigravity CLI (`agy`) parser, detection and discovery.

The fixtures under ``fixtures/antigravity/`` are real `agy` 1.4.6
``transcript_full.jsonl`` step logs with every prompt, reply, thought, file
content, tool summary, tool output, error text and command argument replaced
by a placeholder, paths replaced by ``/Users/dev/project/file_N``, conversation
ids renumbered and timestamps moved to UTC. What survives is the structure:
step types and order, statuses, token counts, tool names, arg keys, result
labels and exit codes.

* ``read_explain``: reads two files and explains them.
* ``edit_and_run``: reads a file, edits it, reads it again, runs it.
* ``failed_command``: a command exits 1, the retry exits 0.
* ``failed_edit``: an edit whose target text is not in the file.
* ``denied_command``: the user denies a command; the log ends on the result.
* ``parallel_reads``: one planner step with three `read_file` calls.
* ``create_file``: writes a new file, with a git command that exits 128.
* ``subagent_parent`` / ``subagent_child``: `invoke_subagent` and the child's
  own conversation, which opens with a `SYSTEM_MESSAGE`.
* ``multi_turn``: three prompts in one conversation (`--conversation`).

The conversation index (`conversation_summaries.db`) is not a fixture: the
tests build one from the real column schema with made-up rows.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentplay import builder, discover
from agentplay._core.ingestion.coding_agent import tool_taxonomy as tt
from agentplay._core.ingestion.coding_agent._sources.local import antigravity_index
from agentplay._core.ingestion.coding_agent._sources.local.antigravity_index import _path_from_file_uri
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    AgentType,
    antigravity,
    detect_agent,
    parse_antigravity,
)
from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

from .test_parser_robustness import WRONG_TYPES, _assign, _damage_cases, _encode, _paths

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
GEMINI_FIXTURES = Path(__file__).parent / "fixtures" / "gemini"
FIXTURE_ROOT = "/Users/dev/project"
NAMES = (
    "read_explain",
    "edit_and_run",
    "failed_command",
    "failed_edit",
    "denied_command",
    "parallel_reads",
    "create_file",
    "subagent_parent",
    "subagent_child",
    "multi_turn",
)
# The renumbered conversation id of each fixture (`brain/<id>/` on disk).
IDS = {name: f"00000000-0000-4000-8000-{n:012d}" for n, name in enumerate(NAMES, 1)}

# `conversation_summaries.db` exactly as agy 1.4.6 creates it.
SUMMARIES_SCHEMA = (
    "CREATE TABLE `conversation_summaries` (`conversation_id` text,`title` text NOT NULL DEFAULT \"\","
    "`preview` text NOT NULL DEFAULT \"\",`step_count` integer NOT NULL DEFAULT 0,"
    "`last_modified_time` datetime NOT NULL,`workspace_uris` text NOT NULL,"
    "`status` text NOT NULL DEFAULT \"\",`source` text NOT NULL DEFAULT \"\","
    "`project_id` text NOT NULL DEFAULT \"\",`agent_name` text NOT NULL DEFAULT \"\","
    "`parent_conversation_id` text NOT NULL DEFAULT \"\",`nesting_depth` integer NOT NULL DEFAULT 0,"
    "`battle_id` text NOT NULL DEFAULT \"\",`winning_conversation_id` text NOT NULL DEFAULT \"\","
    "`not_fully_idle` numeric NOT NULL DEFAULT false,`killed` numeric NOT NULL DEFAULT false,"
    "`last_user_input_time` datetime NOT NULL,`last_user_input_step_index` integer NOT NULL DEFAULT -1,"
    "`app_data_dir` text NOT NULL DEFAULT \"\",`raw_summary` blob,`group_id` text NOT NULL DEFAULT \"\","
    "PRIMARY KEY (`conversation_id`))"
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.jsonl").read_bytes()


def _parse(name: str):
    _, spans = parse_antigravity(_fixture(name), f"local:antigravity-cli:{IDS[name]}")
    return spans


def _turns(spans):
    return [s for s in spans if s.name.startswith("Turn ")]


def _tools(spans):
    return [s for s in spans if ":tool:" in s.span_id]


def _records(name: str) -> list[dict]:
    return [json.loads(line) for line in _fixture(name).splitlines() if line.strip()]


def _args(span) -> dict:
    return json.loads(span.attributes["input.value"])


# ---------------------------------------------------------------- detection


@pytest.mark.parametrize("name", NAMES)
def test_fixture_is_detected_as_antigravity_cli(name):
    assert detect_agent(_fixture(name)) == AgentType.ANTIGRAVITY_CLI


@pytest.mark.parametrize("name", ["read_explain", "edit_and_run", "failed_shell"])
def test_gemini_cli_sessions_are_not_claimed(name):
    assert detect_agent((GEMINI_FIXTURES / f"{name}.jsonl").read_bytes()) == AgentType.GEMINI_CLI


def test_claude_code_records_are_not_claimed():
    line = {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "hi"}}
    assert detect_agent(json.dumps(line).encode()) == AgentType.CLAUDE_CODE


@pytest.mark.parametrize(
    "line",
    [
        {"step_index": True, "source": "MODEL", "type": "GENERIC"},  # bool, not an index
        {"step_index": "0", "source": "MODEL", "type": "GENERIC"},
        {"step_index": 0, "type": "GENERIC"},  # no source
        {"step_index": 0, "source": "MODEL", "type": "SOMETHING_ELSE"},
    ],
)
def test_near_miss_lines_are_not_antigravity(line):
    assert detect_agent(json.dumps(line).encode()) is None


def test_a_later_step_alone_is_still_detected():
    line = {"step_index": 7, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE"}
    assert detect_agent(json.dumps(line).encode()) == AgentType.ANTIGRAVITY_CLI


# ---------------------------------------------------------------- span tree


@pytest.mark.parametrize(
    ("name", "turns", "tools"),
    [
        ("read_explain", 1, ["Read", "Read"]),
        ("edit_and_run", 1, ["Read", "Edit", "Read", "Bash"]),
        ("failed_command", 1, ["Bash", "Bash"]),
        ("failed_edit", 1, ["Edit"]),
        ("denied_command", 1, ["Bash"]),
        ("parallel_reads", 1, ["Read", "Read", "Read"]),
        ("subagent_parent", 1, ["Task"]),
        ("multi_turn", 3, ["Read", "Read", "Read", "Read", "Read"]),
    ],
)
def test_turn_and_tool_counts(name, turns, tools):
    spans = _parse(name)
    assert spans[0].name == "Session" and spans[0].parent_id is None
    assert len(_turns(spans)) == turns
    assert [s.name for s in _tools(spans)] == tools
    turn_ids = {t.span_id for t in _turns(spans)}
    assert all(s.parent_id in turn_ids for s in _tools(spans))
    assert all(s.attributes["tool.name"] == s.name for s in _tools(spans))


def test_read_session_keeps_only_the_prompt_and_the_path():
    spans = _parse("read_explain")
    (turn,) = _turns(spans)
    # Only the <USER_REQUEST> body; the CLI's metadata blocks are not the prompt.
    assert turn.attributes["input.value"] == "PROMPT-PLACEHOLDER-1"
    assert turn.attributes["output.value"] == "OUTPUT-PLACEHOLDER-3"
    assert [_args(s) for s in _tools(spans)] == [
        {"file_path": f"{FIXTURE_ROOT}/file_1.md"},
        {"file_path": f"{FIXTURE_ROOT}/file_2.js"},
    ]
    assert {s.status_code for s in _tools(spans)} == {"OK"}
    assert spans[0].attributes["harness.agent_session_id"] == IDS["read_explain"]
    assert spans[0].attributes["llm.model_name"] == ""


def test_session_and_turn_end_reach_a_later_tool_completion():
    data = _log(
        _prompt(),
        _planner(_call("read_file", Path="/p/a.py")),
        _result("Created At: 2026-10-23T10:00:02Z\nCompleted At: 2026-10-23T10:05:00Z\nok"),
    )
    trace, spans = parse_antigravity(data, "t")
    end = datetime(2026, 10, 23, 10, 5, tzinfo=UTC)
    assert trace.end_time == end
    assert spans[0].end_time == end
    # The last step is at 10:00:02, but the read completed at 10:05:00.
    (turn,) = _turns(spans)
    assert turn.end_time == end
    assert turn.end_time >= max(t.end_time for t in _tools(spans))


def test_edit_and_run_normalizes_args():
    read, edit, _, run = _tools(_parse("edit_and_run"))
    assert set(_args(edit)) == {"file_path", "old_string", "new_string"}
    assert _args(edit)["old_string"].count("\n") == 6  # line count survives
    assert _args(run) == {"command": "node ARG-1"}  # WaitMsBeforeAsync / toolSummary dropped
    assert {s.status_code for s in (read, edit, run)} == {"OK"}
    assert edit.attributes["code.edit_evidence"]["status"] != "not_successful"
    # Tool times come from the result's Created At / Completed At lines.
    assert read.start_time == datetime(2026, 10, 23, 7, 37, 3, tzinfo=UTC)
    assert read.end_time == datetime(2026, 10, 23, 7, 37, 4, tzinfo=UTC)


def test_failed_command_is_an_error_and_the_retry_is_not():
    failed, retry = _tools(_parse("failed_command"))
    # agy records status DONE for both; the exit-code sentence is the signal.
    assert failed.status_code == "ERROR"
    assert retry.status_code == "OK"
    assert failed.attributes["output.value"] == ""
    assert "OUTPUT-PLACEHOLDER-6" in retry.attributes["output.value"]


def test_failed_edit_is_an_error_without_its_text():
    (edit,) = _tools(_parse("failed_edit"))
    assert edit.status_code == "ERROR"
    assert edit.attributes["output.value"] == ""
    assert edit.attributes["code.edit_evidence"]["status"] == "not_successful"


def test_denied_command_is_an_error_and_ends_the_session():
    spans = _parse("denied_command")
    (run,) = _tools(spans)
    assert run.status_code == "ERROR"
    assert run.attributes["output.value"] == ""
    assert _records("denied_command")[-1]["type"] == "GENERIC"


def test_parallel_calls_pair_with_results_in_order():
    spans = _parse("parallel_reads")
    tools = _tools(spans)
    assert [s.span_id.rsplit(":tool:", 1)[1] for s in tools] == ["1:0", "1:1", "1:2"]
    assert [_args(s)["file_path"] for s in tools] == [
        f"{FIXTURE_ROOT}/file_1.md",
        f"{FIXTURE_ROOT}/file_2.js",
        f"{FIXTURE_ROOT}/file_3.js",
    ]
    assert [s.attributes["output.value"].splitlines()[-1] for s in tools] == [
        "OUTPUT-PLACEHOLDER-1",
        "OUTPUT-PLACEHOLDER-2",
        "OUTPUT-PLACEHOLDER-3",
    ]


def test_create_file_writes_and_flags_the_non_zero_exit():
    tools = _tools(_parse("create_file"))
    write = next(s for s in tools if s.name == "Write")
    assert set(_args(write)) == {"file_path", "content"}
    assert write.status_code == "OK"
    assert [s.status_code for s in tools].count("ERROR") == 1  # the exit-128 git call
    read = next(s for s in tools if s.name == "Read")
    assert _args(read) == {"file_path": f"{FIXTURE_ROOT}/file_1.js"}  # StartLine/EndLine dropped


def test_subagent_launch_keeps_type_and_model_only():
    (task,) = _tools(_parse("subagent_parent"))
    assert _args(task) == {"subagent_type": "research", "model": "inherit"}
    # The child's report (a SYSTEM_MESSAGE) stays in the parent's one turn.
    assert len(_turns(_parse("subagent_parent"))) == 1


def test_subagent_conversation_opens_a_turn_without_a_prompt():
    spans = _parse("subagent_child")
    (turn,) = _turns(spans)
    assert turn.attributes["input.value"] == ""
    tools = _tools(spans)
    assert [s.name for s in tools].count("Read") == 6  # view_file
    assert _args(next(s for s in tools if s.name == "Read")) == {"file_path": f"{FIXTURE_ROOT}/file_1.js"}
    bash = next(s for s in tools if s.name == "Bash")
    assert _args(bash) == {"command": "grep ARG-1 ARG-2 ARG-3", "workdir": FIXTURE_ROOT}
    # The subagent's last grep exits 1 ("The command exited with code 1.").
    assert [s.status_code for s in tools if s.name == "Bash"][-1] == "ERROR"
    send = tools[-1]
    assert send.name == "send_message"
    assert _args(send) == {}


def test_multi_turn_splits_on_user_input_only():
    turns = _turns(_parse("multi_turn"))
    assert [t.attributes["input.value"] for t in turns] == [
        "PROMPT-PLACEHOLDER-1",
        "PROMPT-PLACEHOLDER-2",
        "PROMPT-PLACEHOLDER-4",
    ]
    assert [len(t.attributes["llm.cost_events"]) for t in turns] == [4, 2, 2]


@pytest.mark.parametrize("name", NAMES)
def test_tokens_are_counted_once_per_planner_step(name):
    planners = [r for r in _records(name) if r["type"] == "PLANNER_RESPONSE"]
    turns = _turns(_parse(name))
    assert sum(t.attributes["llm.token_count.prompt"] for t in turns) == sum(p["input_tokens"] for p in planners)
    assert sum(t.attributes["llm.token_count.completion"] for t in turns) == sum(
        p["output_tokens"] for p in planners
    )
    assert sum(len(t.attributes["llm.cost_events"]) for t in turns) == len(planners)
    assert all(t.attributes["llm.token_count.reasoning"] == 0 for t in turns)


# ---------------------------------------------------------------- synthetic step logs

TS = "2026-10-23T10:00:{:02d}Z"


def _log(*steps: dict) -> bytes:
    lines = []
    for i, step in enumerate(steps):
        rec = {"step_index": i, "source": "MODEL", "status": "DONE", "created_at": TS.format(i), **step}
        lines.append(json.dumps(rec).encode())
    return b"\n".join(lines) + b"\n"


def _prompt(text: str = "go") -> dict:
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>"}


def _planner(*calls: dict, **extra) -> dict:
    step = {"type": "PLANNER_RESPONSE", "input_tokens": 10, "cache_read_tokens": 0, "output_tokens": 2, **extra}
    if calls:
        step["tool_calls"] = list(calls)
    return step


def _call(name: str, **args) -> dict:
    return {"name": name, "args": args}


def _result(content: str = "Created At: x\nCompleted At: y\nok", **extra) -> dict:
    return {"type": "GENERIC", "content": content, **extra}


def test_missing_result_between_planner_steps_is_unset():
    data = _log(
        _prompt(),
        _planner(_call("read_file", Path="/p/a.py"), _call("read_file", Path="/p/b.py")),
        _result(),  # only the first call gets a result
        _planner(_call("run_command", CommandLine="ls")),
        _result("Command exited with code 0.\n\nCommand output:\nx"),
        _planner(_call("read_file", Path="/p/c.py")),  # log ends before this result
    )
    tools = _tools(parse_antigravity(data, "t")[1])
    assert [(s.name, s.status_code) for s in tools] == [
        ("Read", "OK"),
        ("Read", "UNSET"),
        ("Bash", "OK"),
        ("Read", "UNSET"),
    ]
    # The new planner step closed the queue: its result did not go to the old call.
    assert _args(tools[2]) == {"command": "ls"}
    unset = tools[1]
    assert unset.attributes["output.value"] == ""
    assert unset.end_time == unset.start_time


def test_result_without_a_pending_call_is_ignored():
    data = _log(_prompt(), _result(), _planner(), _result())
    _, spans = parse_antigravity(data, "t")
    assert _tools(spans) == []
    assert len(_turns(spans)) == 1


@pytest.mark.parametrize(
    ("content", "status"),
    [
        ("Command exited with code 0.\n\nCommand output:\nError: printed by the program", "OK"),
        # The program printing the CLI's own sentence after the output label.
        ("Command exited with code 0.\n\nCommand output:\nCommand exited with code 3.", "OK"),
        ("Command exited with code 2.\n\nCommand output:\n", "ERROR"),
        ("\nThe command exited with code 1.\nStdout:\n\nStderr:\n", "ERROR"),
        ("\nThe command exited with code 0.\nOutput:\nx\n", "OK"),
        ("Command exited with code -9.\n", "ERROR"),
        ("no exit line at all", "OK"),
    ],
)
def test_exit_code_sentence_decides_command_status(content, status):
    data = _log(_prompt(), _planner(_call("run_command", CommandLine="x")), _result(content))
    (tool,) = _tools(parse_antigravity(data, "t")[1])
    assert tool.status_code == status


def test_exit_code_sentence_only_counts_for_commands():
    data = _log(_prompt(), _planner(_call("read_file", Path="/p/a")), _result("Command exited with code 4."))
    (tool,) = _tools(parse_antigravity(data, "t")[1])
    assert tool.status_code == "OK"


def test_error_status_or_error_field_marks_failure():
    data = _log(
        _prompt(),
        _planner(_call("edit_file", Path="/p/a", Target="a", Replacement="b"), _call("write_file", Path="/p/b")),
        _result(status="ERROR", error="target content not found"),
        _result(error="something failed"),
    )
    assert [s.status_code for s in _tools(parse_antigravity(data, "t")[1])] == ["ERROR", "ERROR"]


def test_zero_token_planner_still_records_a_request():
    data = _log(_prompt(), _planner(input_tokens=0, output_tokens=0, cache_read_tokens=0))
    (turn,) = _turns(parse_antigravity(data, "t")[1])
    (event,) = turn.attributes["llm.cost_events"]
    assert event["input_tokens"] == 0
    assert event["output_tokens"] == 0
    assert turn.attributes["llm.token_count.prompt"] == 0
    assert turn.attributes["llm.token_count.completion"] == 0


def test_cache_reads_are_a_subset_of_input():
    data = _log(_prompt(), _planner(input_tokens=100, cache_read_tokens=40, output_tokens=5),
                _planner(input_tokens=10, cache_read_tokens=99, output_tokens=1))
    (turn,) = _turns(parse_antigravity(data, "t")[1])
    attrs = turn.attributes
    assert attrs["llm.token_count.prompt"] == 110
    assert attrs["llm.token_count.cache_read"] == 50  # 40 + min(99, 10)
    assert attrs["llm.token_count.input_fresh"] == 60
    assert [e["input_tokens"] for e in attrs["llm.cost_events"]] == [60, 0]


def test_aliased_tools_keep_only_path_and_pattern_keys():
    """Schemas from the agy 1.4.6 binary. File text, prompts and explanations stay out."""
    data = _log(
        _prompt(),
        _planner(
            _call(
                "write_to_file",
                TargetFile="/Users/dev/project/a.py",
                CodeContent="SECRET-CONTENT",
                explanation="SECRET-WHY",
            ),
            _call(
                "replace_file_content",
                TargetFile="/Users/dev/project/b.py",
                Instruction="SECRET-INSTRUCTION",
                TargetContent="SECRET-OLD",
                ReplacementContent="SECRET-NEW",
            ),
            _call(
                "multi_replace_file_content",
                TargetFile="/Users/dev/project/c.py",
                ReplacementChunks=[{"TargetContent": "SECRET-OLD", "ReplacementContent": "SECRET-NEW"}],
            ),
            _call(
                "grep_search",
                Query="needle",
                SearchPath="/Users/dev/project",
                Includes="*.py",
                explanation="SECRET-WHY",
            ),
            _call(
                "find_by_name",
                Pattern="*.py",
                SearchDirectory="/Users/dev/project",
                Excludes="vendor/**",
                Type="file",
                toolSummary="SECRET-SUMMARY",
            ),
            _call("list_dir", DirectoryPath="/Users/dev/project", explanation="SECRET-WHY"),
            _call("search_web", Query="SECRET-WEB", domain="example.com"),
            _call("read_url_content", Url="https://example.com/doc", markdown_content="SECRET-PAGE"),
        ),
    )
    tools = _tools(parse_antigravity(data, "t")[1])
    assert [s.name for s in tools] == [
        "Write",
        "Edit",
        "Edit",
        "Grep",
        "Glob",
        "ListDir",
        "WebSearch",
        "WebFetch",
    ]
    assert [_args(s) for s in tools] == [
        {"file_path": "/Users/dev/project/a.py"},
        {"file_path": "/Users/dev/project/b.py"},
        {"file_path": "/Users/dev/project/c.py"},
        {"file_path": "/Users/dev/project", "pattern": "needle", "glob": "*.py"},
        {"file_path": "/Users/dev/project", "pattern": "*.py", "glob": "vendor/**"},
        {"file_path": "/Users/dev/project"},
        {},
        {"url": "https://example.com/doc"},
    ]
    dumped = json.dumps([s.attributes for s in tools], default=str)
    assert "SECRET" not in dumped


def test_unlisted_args_and_tools_carry_no_values():
    data = _log(
        _prompt(),
        _planner(
            _call("run_command", CommandLine="ls", toolSummary="SECRET-SUMMARY", toolAction="SECRET-ACTION"),
            _call("invoke_subagent", Prompt="SECRET-PROMPT", Role="SECRET-ROLE", TypeName="research"),
            _call("grep_search", Query="needle", SearchPath="/p", explanation="SECRET-QUERY"),
            _call("brand_new_tool", Anything="SECRET-ANY"),
            _call("read_file", Path=["not", "a", "string"]),
        ),
    )
    tools = _tools(parse_antigravity(data, "t")[1])
    assert [s.name for s in tools] == ["Bash", "Task", "Grep", "brand_new_tool", "Read"]
    assert [_args(s) for s in tools] == [
        {"command": "ls"},
        {"subagent_type": "research"},
        {"file_path": "/p", "pattern": "needle"},
        {},
        {},
    ]
    assert "SECRET" not in json.dumps([s.attributes for s in tools], default=str)


def test_steps_are_ordered_and_deduplicated_by_index():
    first = {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "input_tokens": 1, "output_tokens": 1}
    again = {**first, "input_tokens": 7}
    prompt = {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"}
    data = b"\n".join(json.dumps(r).encode() for r in (first, prompt, again))
    (turn,) = _turns(parse_antigravity(data, "t")[1])
    assert turn.attributes["input.value"] == "hi"  # no wrapper: the whole text
    assert turn.attributes["llm.token_count.prompt"] == 7


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json at all\n",
        b"[1, 2]\nnull\n",
        _fixture("edit_and_run")[:-40],  # last line cut mid-record
        b'{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT"',
        _log({"type": "USER_INPUT", "content": 5, "created_at": "nope"}),
        _log(_prompt(), {"type": "PLANNER_RESPONSE", "tool_calls": "x", "input_tokens": "many"}),
        _log(_prompt(), _planner({"name": None, "args": []}, "not a dict"), {"type": "GENERIC", "content": None}),
        _log({"type": "GENERIC", "status": "ERROR"}),
    ],
)
def test_malformed_or_partial_input_does_not_crash(data):
    trace, spans = parse_antigravity(data, "t")
    assert trace.trace_id == "t"
    assert isinstance(spans, list)


def test_partial_last_line_keeps_the_earlier_steps():
    data = _fixture("edit_and_run")
    cut = data[: data.rindex(b"\n", 0, len(data) - 1) + 20]  # last step half written
    assert [s.name for s in _tools(parse_antigravity(cut, "t")[1])] == ["Read", "Edit", "Read", "Bash"]


def test_empty_input_yields_no_spans():
    assert parse_antigravity(b"", "t")[1] == []
    assert parse_antigravity(b'{"step_index": 0}\n', "t")[1] == []


# ---------------------------------------------------------------- file URIs


@pytest.mark.parametrize(
    ("uri", "path"),
    [
        ("file:///Users/dev/project", "/Users/dev/project"),
        ("file:///C:/Users/dev/project", "C:/Users/dev/project"),
        ("file:///c%3A/Users/dev/my%20project", "c:/Users/dev/my project"),
        ("file://server/share/project", "//server/share/project"),
        ("file://localhost/Users/dev/project", "/Users/dev/project"),
        ("https://example.com/x", None),
        ("", None),
        (None, None),
        (5, None),
    ],
)
def test_path_from_file_uri(uri, path):
    assert _path_from_file_uri(uri) == path


# ---------------------------------------------------------------- discovery


def _write_summaries(home: Path, rows: list[tuple[str, str, str, int]]) -> None:
    """Rows of (conversation_id, workspace_uris, parent_conversation_id, nesting_depth)."""
    conn = sqlite3.connect(home / "conversation_summaries.db")
    try:
        conn.execute(SUMMARIES_SCHEMA)
        conn.executemany(
            "INSERT INTO conversation_summaries (conversation_id, title, preview, last_modified_time,"
            " workspace_uris, parent_conversation_id, nesting_depth, last_user_input_time, raw_summary)"
            " VALUES (?, 'TITLE-SECRET', 'PREVIEW-SECRET', '2026-10-23 10:00:00+00:00', ?, ?, ?,"
            " '2026-10-23 10:00:00+00:00', X'00')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _antigravity_home(tmp_path: Path, uri: str = f"file://{FIXTURE_ROOT}", names=NAMES) -> Path:
    home = tmp_path / ".gemini" / "antigravity-cli"
    for name in names:
        logs = home / "brain" / IDS[name] / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript_full.jsonl").write_bytes(_fixture(name))
        # The display-form log sits next to it and must not be indexed.
        (logs / "transcript.jsonl").write_bytes(_fixture(name))
    rows = [
        (
            IDS[name],
            json.dumps([uri]),
            IDS["subagent_parent"] if name == "subagent_child" else "",
            1 if name == "subagent_child" else 0,
        )
        for name in names
    ]
    _write_summaries(home, rows)
    return home


def test_local_source_indexes_antigravity_sessions(tmp_path):
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[_antigravity_home(tmp_path)])
    index = source._build_index()
    assert sorted(index) == sorted(f"local:antigravity-cli:{IDS[n]}" for n in NAMES)
    assert source.discovery_stats()["antigravity-cli"] == {"seen": len(NAMES), "indexed": len(NAMES), "skipped": {}}
    sid = f"local:antigravity-cli:{IDS['edit_and_run']}"
    assert source.get_cwd(sid) == FIXTURE_ROOT
    assert source.get_project_name(sid) == "project"
    spans = source.get_spans_by_session(sid)
    assert {s.attributes["cwd"] for s in spans if s.parent_id is not None} == {FIXTURE_ROOT}
    assert "harness.session_role" not in spans[0].attributes


def test_subagent_conversation_links_to_its_parent(tmp_path):
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[_antigravity_home(tmp_path)])
    child = source.get_spans_by_session(f"local:antigravity-cli:{IDS['subagent_child']}")[0]
    assert child.attributes["harness.session_role"] == "subagent"
    assert child.attributes["harness.parent_session_id"] == f"local:antigravity-cli:{IDS['subagent_parent']}"
    parent = source.get_spans_by_session(f"local:antigravity-cli:{IDS['subagent_parent']}")[0]
    assert "harness.session_role" not in parent.attributes


def test_windows_workspace_uri_gives_a_drive_path(tmp_path):
    home = _antigravity_home(tmp_path, uri="file:///C:/Users/dev/project", names=("read_explain",))
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[home])
    sid = f"local:antigravity-cli:{IDS['read_explain']}"
    source._build_index()
    assert source.get_cwd(sid) == "C:/Users/dev/project"
    assert source.get_project_name(sid) == "project"


def test_malformed_workspace_uri_skips_that_row_only(tmp_path):
    """``file://[bad`` makes urlsplit raise. That row is dropped; the index continues."""
    home = _antigravity_home(tmp_path, names=("read_explain", "failed_edit", "subagent_child"))
    (home / "conversation_summaries.db").unlink()
    _write_summaries(
        home,
        [
            (IDS["read_explain"], json.dumps(["file://[bad", "file:///Users/dev/kept"]), IDS["subagent_parent"], 1),
            (IDS["failed_edit"], json.dumps(["file:///Users/dev/project"]), "", 0),
            (IDS["subagent_child"], json.dumps(["file:///Users/dev/child"]), IDS["subagent_parent"], 1),
        ],
    )
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[home])
    index = source._build_index()
    assert len(index) == 3
    bad = f"local:antigravity-cli:{IDS['read_explain']}"
    good = f"local:antigravity-cli:{IDS['failed_edit']}"
    child = f"local:antigravity-cli:{IDS['subagent_child']}"
    assert source.get_cwd(bad) is None
    assert "harness.parent_session_id" not in source.get_spans_by_session(bad)[0].attributes
    assert source.get_cwd(good) == "/Users/dev/project"
    assert source.get_cwd(child) == "/Users/dev/child"
    assert source.get_spans_by_session(child)[0].attributes["harness.parent_session_id"].endswith(IDS["subagent_parent"])


def test_deeply_nested_workspace_uris_do_not_abort_discovery(tmp_path, monkeypatch):
    nested = '{"a":' * 1200 + "0" + "}" * 1200
    try:
        json.loads(nested)
    except RecursionError:
        pass
    else:
        real_loads = antigravity_index.json.loads

        def loads(raw, *args, **kwargs):
            text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
            if isinstance(text, str) and text.startswith('{"a":' * 40):
                raise RecursionError("maximum recursion depth exceeded")
            return real_loads(raw, *args, **kwargs)

        monkeypatch.setattr(antigravity_index.json, "loads", loads)

    home = _antigravity_home(tmp_path, names=("read_explain", "failed_edit"))
    (home / "conversation_summaries.db").unlink()
    _write_summaries(
        home,
        [
            (IDS["read_explain"], nested, "", 0),
            (IDS["failed_edit"], json.dumps(["file:///Users/dev/project"]), "", 0),
        ],
    )
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[home])
    index = source._build_index()
    assert len(index) == 2
    bad = f"local:antigravity-cli:{IDS['read_explain']}"
    good = f"local:antigravity-cli:{IDS['failed_edit']}"
    assert source.get_cwd(bad) is None
    assert source.get_cwd(good) == "/Users/dev/project"


def test_summaries_copy_stops_after_three_changing_snapshots(tmp_path, monkeypatch, caplog):
    home = _antigravity_home(tmp_path, names=("read_explain",))
    db = home / "conversation_summaries.db"
    original = db.read_bytes()
    wal = db.with_name(db.name + "-wal")
    shm = db.with_name(db.name + "-shm")
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")
    copies: list[str] = []
    real_copy = antigravity_index.shutil.copyfile

    def copyfile(src, dst, *args, **kwargs):
        real_copy(src, dst, *args, **kwargs)
        name = Path(src).name
        copies.append(name)
        # A writer grows the wal on every copy, so the before/after stamps
        # never match. The scan must stop at three attempts and keep the copy.
        if name == db.name:
            wal.write_bytes(b"wal" + b"x" * copies.count(name))

    monkeypatch.setattr(antigravity_index.shutil, "copyfile", copyfile)
    caplog.set_level(logging.DEBUG, logger=antigravity_index.logger.name)
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[home])
    source._build_index()
    assert source.get_cwd(f"local:antigravity-cli:{IDS['read_explain']}") == "/Users/dev/project"
    assert copies.count(db.name) == 3
    assert copies.count(wal.name) == 3
    assert copies.count(shm.name) == 3
    assert "using the last copy" in caplog.text
    assert db.read_bytes() == original


def test_missing_or_unusable_summaries_leave_cwd_empty(tmp_path):
    home = _antigravity_home(tmp_path, names=("read_explain", "failed_edit"))
    (home / "conversation_summaries.db").unlink()
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[home])
    assert len(source._build_index()) == 2
    assert source.get_cwd(f"local:antigravity-cli:{IDS['read_explain']}") is None

    _write_summaries(home, [(IDS["read_explain"], "not json", "", 0), (IDS["failed_edit"], "[]", "", 0)])
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[home])
    source._build_index()
    assert source.get_cwd(f"local:antigravity-cli:{IDS['read_explain']}") is None
    assert source.get_cwd(f"local:antigravity-cli:{IDS['failed_edit']}") is None

    (home / "conversation_summaries.db").unlink()
    (home / "conversation_summaries.db").write_bytes(b"not a database")
    source = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[home])
    assert len(source._build_index()) == 2


def test_summaries_db_is_never_written(tmp_path):
    home = _antigravity_home(tmp_path, names=("read_explain",))
    db = home / "conversation_summaries.db"
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[home])._build_index()
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in home.iterdir()) == ["brain", "conversation_summaries.db"]


def test_discovery_lists_antigravity_cli_with_env_override(tmp_path, monkeypatch):
    assert "antigravity-cli" in discover.AGENTS
    monkeypatch.setenv("AGENTPLAY_ANTIGRAVITY_HOMES", str(tmp_path))
    homes = discover.Homes.default()
    assert homes.antigravity_cli == [tmp_path]
    assert homes.present()["antigravity-cli"] == [tmp_path]
    monkeypatch.delenv("AGENTPLAY_ANTIGRAVITY_HOMES")
    assert discover.Homes.default().antigravity_cli == [Path.home() / ".gemini" / "antigravity-cli"]


def test_discovery_through_env_override_finds_the_sessions(tmp_path, monkeypatch):
    home = _antigravity_home(tmp_path, names=("read_explain", "subagent_parent", "subagent_child"))
    monkeypatch.setenv("AGENTPLAY_ANTIGRAVITY_HOMES", str(home))
    source = discover.local_source(discover.Homes.default(), {"antigravity-cli"})
    found = discover.candidates(source)
    assert {c.agent for c in found} == {"antigravity-cli"}
    assert len(found) == 3
    child = next(c for c in found if c.session_id.endswith(IDS["subagent_child"]))
    doc = discover.build_map(child, source)
    assert doc["session"]["role"] == "subagent"
    assert doc["session"]["parentSessionId"] == f"local:antigravity-cli:{IDS['subagent_parent']}"
    parent = next(c for c in found if c.session_id.endswith(IDS["subagent_parent"]))
    assert discover.build_map(parent, source)["session"]["role"] is None


def _gemini_tree(gemini_home: Path) -> None:
    """A Gemini CLI project folder next to the Antigravity CLI home, as on disk."""
    chats = gemini_home / "tmp" / "project" / "chats"
    chats.mkdir(parents=True)
    (chats.parent / ".project_root").write_text(FIXTURE_ROOT, encoding="utf-8")
    (chats / "session-2026-10-23T07-00-read.jsonl").write_bytes((GEMINI_FIXTURES / "read_explain.jsonl").read_bytes())


def test_gemini_and_antigravity_homes_never_pick_up_each_other(tmp_path):
    antigravity = _antigravity_home(tmp_path, names=("read_explain", "failed_edit"))
    gemini = antigravity.parent  # ~/.gemini: Gemini's home is Antigravity's parent
    _gemini_tree(gemini)
    # The Antigravity IDE's sibling folder has brain/ too but no step logs.
    (gemini / "antigravity" / "brain" / IDS["read_explain"]).mkdir(parents=True)

    both = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[gemini], antigravity_homes=[antigravity])
    agents = {sid: ref.agent for sid, ref in both._build_index().items()}
    assert sorted(agents.values()) == ["antigravity-cli", "antigravity-cli", "gemini-cli"]

    gemini_only = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[gemini])
    assert {r.agent for r in gemini_only._build_index().values()} == {AgentType.GEMINI_CLI}

    antigravity_only = LocalSource(claude_code_homes=[], codex_homes=[], antigravity_homes=[antigravity])
    assert {r.agent for r in antigravity_only._build_index().values()} == {AgentType.ANTIGRAVITY_CLI}

    # Homes pointed at the wrong tree find nothing rather than misreading it.
    swapped = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[antigravity], antigravity_homes=[gemini])
    assert swapped._build_index() == {}


def test_a_file_from_the_other_agent_in_the_right_place_is_rejected(tmp_path):
    antigravity = _antigravity_home(tmp_path, names=("read_explain",))
    gemini = antigravity.parent
    _gemini_tree(gemini)
    stray = gemini / "tmp" / "project" / "chats" / "session-2026-10-23T08-00-stray.jsonl"
    stray.write_bytes(_fixture("failed_edit"))
    logs = antigravity / "brain" / "00000000-0000-4000-8000-000000000099" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript_full.jsonl").write_bytes((GEMINI_FIXTURES / "failed_shell.jsonl").read_bytes())

    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[gemini], antigravity_homes=[antigravity])
    index = source._build_index()
    assert len(index) == 2
    stats = source.discovery_stats()
    assert stats["gemini-cli"]["skipped"] == {"different_agent": 1}
    assert stats["antigravity-cli"]["skipped"] == {"different_agent": 1}


# ---------------------------------------------------------------- built maps


def _map(name: str, **kwargs) -> dict:
    sid = f"local:antigravity-cli:{IDS[name]}"
    _, spans = parse_antigravity(_fixture(name), sid)
    for span in spans:
        if span.parent_id is not None:
            span.attributes["cwd"] = FIXTURE_ROOT
    return builder.build_map_from_spans(spans, session_id=sid, agent_tool="antigravity-cli", **kwargs)


def _fixture_strings(name: str) -> set[str]:
    out: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str):
            out.add(value)

    for rec in _records(name):
        walk(rec)
    return out


@pytest.mark.parametrize("name", NAMES)
def test_built_map_never_leaks_fixture_text(name):
    doc = _map(name)
    raw = json.dumps(doc)
    for text in _fixture_strings(name):
        if "PLACEHOLDER" in text or "ARG-" in text:
            # Prompts, metadata, thoughts, replies, file contents, tool
            # summaries, tool output, error text and command arguments.
            assert text not in raw, text
    # Outside a git repository a path collapses to a bucket label, so neither
    # the absolute path nor a file name may appear.
    for part in ("PLACEHOLDER", "ARG-", "USER_REQUEST", "ADDITIONAL_METADATA", FIXTURE_ROOT, "file_1", "file_2"):
        assert part not in raw, part
    assert doc["session"]["agentTool"] == "antigravity-cli"


def test_built_map_classifies_reads_edits_shell_failures_and_subagents():
    def kinds(name: str) -> list[tuple]:
        return [(e["kind"], e.get("verb"), e.get("err")) for e in _map(name)["events"] if e.get("tool")]

    assert kinds("read_explain") == [("read", None, False), ("read", None, False)]
    assert kinds("edit_and_run") == [
        ("read", None, False),
        ("edit", None, False),
        ("read", None, False),
        ("exec", "node", False),
    ]
    assert kinds("failed_command") == [("exec", "node", True), ("exec", "node", False)]
    assert kinds("failed_edit") == [("edit", None, True)]
    assert kinds("denied_command") == [("exec", "touch", True)]
    assert kinds("parallel_reads") == [("read", None, False)] * 3
    launch = next(e for e in _map("subagent_parent")["events"] if e.get("tool"))
    assert (launch["kind"], launch["subagentType"], launch["model"]) == ("subagent", "research", "inherit")
    assert _map("failed_command")["totals"]["errors"] == 1


def test_built_map_of_a_subagent_records_its_parent():
    parent = f"local:antigravity-cli:{IDS['subagent_parent']}"
    doc = _map("subagent_child", role="subagent", parent_session_id=parent)
    assert doc["session"]["role"] == "subagent"
    assert doc["session"]["parentSessionId"] == parent


# ---------------------------------------------------------------- shared sweeps and alias table
# The structural-damage and wrong-type sweeps from test_parser_robustness.py, run
# on a minimal agy step log, and the alias-table checks from test_tool_taxonomy.py.

ROBUSTNESS_TS = "2026-09-14T10:00:0{}Z"
DAMAGE_CASES = (
    "truncated_last_line",
    "invalid_json_line",
    "non_dict_json",
    "unknown_record_type",
    "bom_prefix",
    "non_utf8_bytes",
    "blank_lines",
)


def _robustness_records() -> list[dict]:
    step = {"source": "MODEL", "status": "DONE", "created_at": ROBUSTNESS_TS.format(1)}
    return [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
         "created_at": ROBUSTNESS_TS.format(0), "content": "<USER_REQUEST>\nlist files\n</USER_REQUEST>"},
        {**step, "step_index": 1, "type": "PLANNER_RESPONSE", "input_tokens": 10, "cache_read_tokens": 2,
         "output_tokens": 5, "thinking": "t",
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls", "WaitMsBeforeAsync": 5000}}]},
        {**step, "step_index": 2, "type": "GENERIC",
         "content": "Created At: 2026-09-14T10:00:02Z\nCompleted At: 2026-09-14T10:00:02Z\n"
                    "Command exited with code 0.\n\nCommand output:\nx"},
        {**step, "step_index": 3, "type": "PLANNER_RESPONSE", "input_tokens": 12, "output_tokens": 3,
         "content": "done"},
    ]


def _turn_count(data: bytes) -> int:
    return len(_turns(parse_antigravity(data, "t")[1]))


def test_robustness_clean_log_yields_a_turn():
    assert _turn_count(_encode(_robustness_records())) >= 1


@pytest.mark.parametrize("case", DAMAGE_CASES)
def test_robustness_structural_damage_keeps_the_session(case, caplog):
    caplog.set_level(logging.CRITICAL)
    clean = _encode(_robustness_records())
    assert _turn_count(_damage_cases(clean)[case]) == _turn_count(clean)


def test_robustness_wrong_typed_fields_never_raise(caplog):
    caplog.set_level(logging.CRITICAL)
    base = _robustness_records()
    failures = []
    for index, record in enumerate(base):
        for path in _paths(record):
            for bad in WRONG_TYPES:
                records = copy.deepcopy(base)
                _assign(records[index], path, bad)
                try:
                    parse_antigravity(_encode(records), "t")
                except Exception as exc:
                    failures.append(f"record {index} {path} = {bad!r}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures[:25])


def test_robustness_wrong_typed_record_keeps_other_records():
    """A record whose every field is junk costs only itself."""
    base = _robustness_records()
    junk = {"type": 123, "timestamp": 123, "message": "str", "payload": None, "usage": [], "content": None}
    assert _turn_count(_encode([*base, junk])) == _turn_count(_encode(base))


def test_parser_reads_the_shared_alias_table():
    # Same object, not a copy: a new row in tool_taxonomy reaches the parser.
    assert antigravity._TOOL_ALIASES is tt.aliases_for("antigravity-cli")


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("run_command", "Bash"),
        ("read_file", "Read"),
        ("view_file", "Read"),
        ("edit_file", "Edit"),
        ("write_file", "Write"),
        ("invoke_subagent", "Task"),
    ],
)
def test_known_raw_names_resolve(raw, canonical):
    assert tt.aliases_for("antigravity-cli")[raw] == canonical


def test_alias_table_size_is_pinned():
    # 14 rows: names seen in real agy 1.4.6 sessions plus tool names in the 1.4.6
    # binary. A changed count means a row was added or dropped on purpose.
    assert len(tt.aliases_for("antigravity-cli")) == 14
