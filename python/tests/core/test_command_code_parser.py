"""Command Code parser, detection and discovery.

The fixtures under ``fixtures/command_code/`` are real Command Code sessions
(format version 3) with every prompt, reply, thought, path, file content,
argument value and tool output replaced by a placeholder. What survives is the
structure: record shapes, ids (renumbered), timestamps, usage, model,
`meta.source`, tool names, argument keys, shell program names, and the CLI's
own `Exit code: <n>` labels.

* ``read_and_search``: two prompts; reads, a grep, a failing `python` run
  (exit code 255) and its retry under `powershell`.
* ``edit_and_steer``: a window of a longer session (its header, first prompt,
  and records 105-116): writes and edits a file, runs `npm` / `node`, and takes
  a `steering` message mid-turn. The window ends before the last call's result.
* ``failed_shell``: two failing `git` commands (exit code 128), then one that
  succeeds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentplay import builder, discover
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    AgentType,
    detect_agent,
    parse_command_code,
)
from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource
from agentplay._core.ingestion.coding_agent.file_paths import extract_file_path

FIXTURES = Path(__file__).parent / "fixtures" / "command_code"
FIXTURE_ROOT = "/Users/dev/project"
NAMES = ("read_and_search", "edit_and_steer", "failed_shell")


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.jsonl").read_bytes()


def _parse(name: str):
    _, spans = parse_command_code(_fixture(name), f"local:command-code:{name}")
    return spans


def _turns(spans):
    return [s for s in spans if s.name.startswith("Turn ")]


def _tools(spans):
    return [s for s in spans if ":tool:" in s.span_id]


def _records(name: str) -> list[dict]:
    return [json.loads(line) for line in _fixture(name).splitlines() if line.strip()]


@pytest.mark.parametrize("name", NAMES)
def test_fixture_is_detected_as_command_code(name):
    assert detect_agent(_fixture(name)) == AgentType.COMMAND_CODE


def test_claude_code_records_are_not_claimed_as_command_code():
    line = {"type": "user", "sessionId": "s1", "parentUuid": None, "message": {"role": "user", "content": "hi"}}
    assert detect_agent(json.dumps(line).encode()) == AgentType.CLAUDE_CODE


def test_message_without_header_is_still_detected():
    line = _records("failed_shell")[1]
    assert line["type"] == "message"
    assert detect_agent(json.dumps(line).encode()) == AgentType.COMMAND_CODE


def test_session_record_without_the_header_fields_is_not_claimed():
    line = {"type": "session", "id": "s1"}
    assert detect_agent(json.dumps(line).encode()) is None


@pytest.mark.parametrize(
    ("name", "prompts", "tools"),
    [
        ("read_and_search", 2, ["Read", "Read", "Grep", "Read", "Bash", "Bash"]),
        ("edit_and_steer", 1, ["Bash", "Write", "Bash", "Read", "Grep", "Bash", "Edit", "kill_shell"]),
        ("failed_shell", 1, ["Bash", "ListDir", "Bash", "Bash"]),
    ],
)
def test_turn_and_tool_counts(name, prompts, tools):
    spans = _parse(name)
    assert spans[0].name == "Session" and spans[0].parent_id is None
    turns = _turns(spans)
    assert [t.attributes["input.value"] for t in turns] == [
        f"PROMPT-PLACEHOLDER-{n}" for n in range(1, prompts + 1)
    ]
    assert [s.name for s in _tools(spans)] == tools
    assert all(s.attributes["tool.name"] == s.name for s in _tools(spans))
    assert {s.parent_id for s in _tools(spans)} <= {t.span_id for t in turns}


def test_steering_message_joins_the_running_turn():
    records = _records("edit_and_steer")
    assert [r["message"]["meta"].get("source") for r in records[1:] if r["message"]["role"] == "user"].count(
        "steering"
    ) == 1
    (turn,) = _turns(_parse("edit_and_steer"))
    # The steering text is not the turn's prompt; the turn still ends at its last record.
    assert turn.attributes["input.value"] == "PROMPT-PLACEHOLDER-1"
    assert turn.end_time.isoformat().startswith(records[-1]["timestamp"][:19])


def test_failed_shell_calls_are_errors_and_the_retry_is_not():
    failed_1, listdir, failed_2, retry = _tools(_parse("failed_shell"))
    # The CLI records no error flag; `Exit code: <n>` leading the result is the signal.
    assert (failed_1.status_code, failed_2.status_code) == ("ERROR", "ERROR")
    assert (listdir.status_code, retry.status_code) == ("OK", "OK")
    assert failed_1.attributes["code.edit_evidence"]["status"] == "not_successful"


def test_failed_python_run_and_its_powershell_retry():
    *_, failed, retry = _tools(_parse("read_and_search"))
    assert failed.status_code == "ERROR"
    assert retry.status_code == "OK"


def test_call_whose_result_is_missing_is_unset():
    last = _tools(_parse("edit_and_steer"))[-1]
    assert last.name == "kill_shell"
    assert last.status_code == "UNSET"
    assert last.attributes["output.value"] == ""


def test_write_and_edit_record_edit_evidence():
    tools = _tools(_parse("edit_and_steer"))
    write = next(s for s in tools if s.name == "Write")
    edit = next(s for s in tools if s.name == "Edit")
    for span in (write, edit):
        assert extract_file_path(span.attributes["input.value"]).startswith(f"{FIXTURE_ROOT}/src/")
        assert span.attributes["code.edit_evidence"]["status"] != "not_successful"


def test_path_keys_come_first_so_truncation_keeps_them():
    content = "x" * 4000
    reply = _assistant("a1", _use("c-write", "write_file", content=content, file_path=f"{FIXTURE_ROOT}/a.txt"))
    data = _session(_prompt("u1"), reply, _result("r1", "c-write", "File created successfully"))
    (tool,) = _tools(parse_command_code(data, "t")[1])
    recorded = tool.attributes["input.value"]
    assert len(recorded) <= 2000
    assert recorded.startswith('{"file_path":')
    assert extract_file_path(recorded) == f"{FIXTURE_ROOT}/a.txt"


@pytest.mark.parametrize("name", NAMES)
def test_tokens_are_counted_once_per_request(name):
    usages = [r["usage"] for r in _records(name) if "usage" in r]
    turns = [t.attributes for t in _turns(_parse(name))]
    prompt = sum(u["inputTokens"] for u in usages)
    cached = sum(u["cacheReadTokens"] for u in usages)
    assert sum(t["llm.token_count.prompt"] for t in turns) == prompt
    assert sum(t["llm.token_count.cache_read"] for t in turns) == cached
    assert sum(t["llm.token_count.input_fresh"] for t in turns) == prompt - cached
    assert sum(t["llm.token_count.completion"] for t in turns) == sum(u["outputTokens"] for u in usages)
    assert sum(len(t["llm.cost_events"]) for t in turns) == len(usages)


def test_model_session_id_and_cwd():
    spans = _parse("failed_shell")
    root = spans[0].attributes
    assert root["llm.model_name"] == "deepseek/deepseek-v4-flash"
    assert root["harness.agent_session_id"] == "00000000-0000-4000-8000-000000000003"
    # The header records the cwd, so every turn and call carries it.
    assert {s.attributes["cwd"] for s in spans if s.parent_id is not None} == {FIXTURE_ROOT}


def _session(*records: dict, session_id: str = "s1") -> bytes:
    header = {"type": "session", "id": session_id, "cwd": FIXTURE_ROOT, "version": 3,
              "timestamp": "2026-10-23T07:00:00.000Z"}
    return b"".join(json.dumps(r).encode() + b"\n" for r in (header, *records))


def _message(msg_id: str, role: str, source: str, content: list, **extra) -> dict:
    return {
        "type": "message",
        "id": msg_id,
        "parentId": None,
        "timestamp": "2026-10-23T07:00:01.000Z",
        **extra,
        "message": {"role": role, "content": content, "meta": {"source": source, "messageId": msg_id}},
    }


def _prompt(msg_id: str, text: str = "go", source: str = "user") -> dict:
    return _message(msg_id, "user", source, [{"type": "text", "text": text}])


def _use(call_id: str, name: str, **args) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def _assistant(msg_id: str, *blocks: dict) -> dict:
    return _message(msg_id, "assistant", "model", list(blocks), model="m")


def _result(msg_id: str, call_id: str, text: str) -> dict:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": [{"type": "text", "text": text}]}
    return _message(msg_id, "user", "tool", [block])


@pytest.mark.parametrize(
    ("name", "output", "status"),
    [
        ("shell_command", "Started shell command", "OK"),
        ("shell_command", "Exit code: 1\nTraceback", "ERROR"),
        ("powershell", "Exit code: 0\nok", "OK"),
        # A program printing the label mid-output is not the verdict.
        ("powershell", "build log\nExit code: 2", "OK"),
        ("shell_output", "waited 5s; the command finished\nExit code: 0", "OK"),
        ("shell_output", "waited 5s; the command finished\nExit code: 1", "ERROR"),
        ("edit_file", "File has been modified since it was last read. Read it again.", "ERROR"),
        ("write_file", "File has not been read yet. Read it first.", "ERROR"),
        ("write_file", "File created successfully", "OK"),
        ("read_file", "Path must be absolute (start with /)", "ERROR"),
        ("kill_shell", "No running shell task found with id s1", "ERROR"),
        ("exit_plan_mode", "Plan not approved, staying in plan mode", "ERROR"),
        ("ask_user_question", "User declined to answer questions", "ERROR"),
        ("agent", "[sub-agent interrupted before finishing]", "ERROR"),
        ("agent", "[sub-agent stopped early: a required tool permission was denied]", "ERROR"),
        ("agent", "I have a full summary", "OK"),
        ("vision", "image_index 2 is out of range", "ERROR"),
        ("web_search", "502 upstream error: Web search is temporarily unavailable", "ERROR"),
        ("web_search", "Search results for x", "OK"),
    ],
)
def test_status_comes_from_the_result_text(name, output, status):
    data = _session(_prompt("u1"), _assistant("a1", _use("c1", name)), _result("r1", "c1", output))
    (tool,) = _tools(parse_command_code(data, "t")[1])
    assert tool.status_code == status


def test_followup_opens_a_turn_and_injected_or_steering_text_does_not():
    data = _session(
        _prompt("u1", "first"),
        _assistant("a1", {"type": "text", "text": "one"}),
        _prompt("u2", "steer", source="steering"),
        _prompt("u3", "second", source="followup"),
        {**_prompt("u4", "injected"), "message": {**_prompt("u4", "injected")["message"],
                                                  "meta": {"source": "user", "injected": True}}},
        _assistant("a2", {"type": "text", "text": "two"}),
    )
    turns = _turns(parse_command_code(data, "t")[1])
    assert [t.attributes["input.value"] for t in turns] == ["first", "second"]
    assert [t.attributes["output.value"] for t in turns] == ["one", "two"]


def test_image_blocks_in_a_result_carry_no_text():
    block = {
        "type": "tool_result",
        "tool_use_id": "c1",
        "content": [{"type": "image", "source": {"type": "base64", "data": "AAAA"}}, {"type": "text", "text": "Read image"}],
    }
    data = _session(
        _prompt("u1"),
        _assistant("a1", _use("c1", "read_file", file_path=f"{FIXTURE_ROOT}/a.png")),
        _message("r1", "user", "tool", [block]),
    )
    (tool,) = _tools(parse_command_code(data, "t")[1])
    assert tool.attributes["output.value"] == "Read image"
    assert tool.status_code == "OK"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json at all\n",
        b"[1, 2]\nnull\n",
        _session(),  # header only: a session opened and closed with no prompt
        _session({"type": "message", "id": "u1", "message": None}),
        _session({"type": "message", "id": "u1", "timestamp": "nope", "message": {"role": "user", "content": 5}}),
        _session({"type": "message", "id": "a1", "model": 3, "usage": "many",
                  "message": {"role": "assistant", "content": [{"type": "tool_use", "name": None, "input": []}]}}),
        _session({"type": "message", "id": "r1", "message": {"role": "user", "meta": [],
                  "content": [{"type": "tool_result", "tool_use_id": 7, "content": None}]}}),
    ],
)
def test_malformed_or_empty_input_does_not_crash(data):
    trace, spans = parse_command_code(data, "t")
    assert trace.trace_id == "t"
    assert isinstance(spans, list)


def test_empty_input_yields_no_spans():
    assert parse_command_code(b"", "t")[1] == []
    assert parse_command_code(_session(), "t")[1] == []


def _map(name: str) -> dict:
    _, spans = parse_command_code(_fixture(name), f"local:command-code:{name}")
    return builder.build_map_from_spans(spans, session_id=f"local:command-code:{name}", agent_tool="command-code")


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
        if "PLACEHOLDER" not in text and "ARG-" not in text:
            continue
        # Every placeholder stands for prompt text, model output, a thought, a
        # file's contents, a tool description or output, or a command argument.
        assert text not in raw, text
    # Outside a git repository a path collapses to a bucket label, so neither
    # the absolute path nor the file name may appear.
    for part in ("PLACEHOLDER", "ARG-1", FIXTURE_ROOT, "file-1", "file-2"):
        assert part not in raw, part
    assert doc["session"]["agentTool"] == "command-code"


def test_built_map_classifies_reads_edits_shell_and_the_failures():
    def kinds(name: str) -> list[tuple]:
        return [(e["kind"], e.get("verb"), e.get("err")) for e in _map(name)["events"] if e.get("tool")]

    assert kinds("read_and_search") == [
        ("read", None, False),
        ("read", None, False),
        ("search", None, False),
        ("read", None, False),
        ("exec", "python", True),
        ("exec", "python", False),
    ]
    assert [k for k in kinds("edit_and_steer") if k[0] in ("edit", "exec")] == [
        ("exec", "npm", False),
        ("edit", None, False),
        ("exec", "node", False),
        ("exec", "npm", False),
        ("edit", None, False),
    ]
    failed = _map("failed_shell")
    assert failed["totals"]["errors"] == 2
    assert failed["session"]["models"] == ["deepseek/deepseek-v4-flash"]


def _command_code_home(tmp_path: Path) -> Path:
    home = tmp_path / ".commandcode"
    project = home / "projects" / "users-dev-project"
    project.mkdir(parents=True)
    for name in NAMES:
        native_id = _records(name)[0]["id"]
        (project / f"{native_id}.jsonl").write_bytes(_fixture(name))
        # Siblings the indexer must not read as transcripts.
        (project / f"{native_id}.checkpoints.jsonl").write_text(
            json.dumps({"id": "cp1", "messageId": "m1", "turnNumber": 1, "prompt": "PROMPT", "files": []}) + "\n",
            encoding="utf-8",
        )
        (project / f"{native_id}.meta.json").write_text('{"title": "TITLE"}', encoding="utf-8")
    (project / "empty-session.jsonl").write_bytes(_session(session_id="empty-session"))
    (home / "history.jsonl").write_text('{"p": "PROMPT", "t": 1}\n', encoding="utf-8")
    return home


def test_local_source_indexes_command_code_sessions(tmp_path):
    source = LocalSource(claude_code_homes=[], codex_homes=[], command_code_homes=[_command_code_home(tmp_path)])
    index = source._build_index()
    assert sorted(index) == [
        f"local:command-code:00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3)
    ]
    stats = source.discovery_stats()["command-code"]
    assert stats["skipped"] == {"empty_events": 1}
    sid = "local:command-code:00000000-0000-4000-8000-000000000003"
    assert source.get_cwd(sid) == FIXTURE_ROOT
    assert source.get_project_name(sid) == "project"
    spans = source.get_spans_by_session(sid)
    assert [s.name for s in _tools(spans)] == ["Bash", "ListDir", "Bash", "Bash"]


def test_a_claude_code_transcript_in_the_home_is_not_claimed(tmp_path):
    home = _command_code_home(tmp_path)
    line = {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "hi"}}
    (home / "projects" / "users-dev-project" / "claude.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    source = LocalSource(claude_code_homes=[], codex_homes=[], command_code_homes=[home])
    source._build_index()
    assert source.discovery_stats()["command-code"]["skipped"] == {"empty_events": 1, "different_agent": 1}


def test_discovery_lists_command_code_with_env_override(tmp_path, monkeypatch):
    assert "command-code" in discover.AGENTS
    monkeypatch.setenv("AGENTPLAY_COMMANDCODE_HOMES", str(tmp_path))
    homes = discover.Homes.default()
    assert homes.command_code == [tmp_path]
    assert homes.present()["command-code"] == [tmp_path]
    monkeypatch.delenv("AGENTPLAY_COMMANDCODE_HOMES")
    assert discover.Homes.default().command_code == [Path.home() / ".commandcode"]
