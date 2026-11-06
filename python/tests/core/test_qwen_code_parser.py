"""Qwen Code parser, detection and discovery.

The fixtures under ``fixtures/qwen_code/`` are real Qwen Code 0.26.5 sessions
(OpenAI-compatible auth) with every prompt, reply, thought, argument value,
tool output, error message and telemetry string replaced by a placeholder, ids
renumbered and the project root rewritten to ``/Users/dev/project``. What survives is the structure:
record types and subtypes, timestamps, usage, model, tool names, argument keys,
statuses and error types.

* ``failed_read``: one prompt; two ``read_file`` calls that fail with
  ``invalid_tool_params`` (the model passed relative paths), then a reply.
* ``edit_then_read``: one prompt; a ``write_file`` that fails with
  ``edit_requires_prior_read``, a ``read_file`` that succeeds, then a reply.

The shell path (exit codes, verbs) is covered by the synthetic records below;
no captured session ran a shell command.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentplay import builder, discover
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    AgentType,
    detect_agent,
    parse_qwen_code,
)
from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource
from agentplay._core.ingestion.coding_agent.file_paths import extract_file_path

ROOT = "/Users/dev/project"


def _rec(uid: str, kind: str, message: dict | None = None, *, ts: int = 1, **extra) -> dict:
    rec = {
        "uuid": uid,
        "parentUuid": None,
        "sessionId": "s1",
        "timestamp": f"2026-11-02T10:00:{ts:02d}.000Z",
        "type": kind,
        "cwd": ROOT,
        "version": "0.26.5",
        **extra,
    }
    if message is not None:
        rec["message"] = message
    return rec


def _prompt(uid: str, text: str = "go", **extra) -> dict:
    return _rec(uid, "user", {"role": "user", "parts": [{"text": text}]}, **extra)


def _reply(uid: str, *parts: dict, usage: dict | None = None, **extra) -> dict:
    rec = _rec(uid, "assistant", {"role": "model", "parts": list(parts)}, model="qwen3:8b", **extra)
    if usage is not None:
        rec["usageMetadata"] = usage
    return rec


def _call(call_id: str, name: str, **args) -> dict:
    return {"functionCall": {"id": call_id, "name": name, "args": args}}


def _result(uid: str, call_id: str, name: str, output: str, status: str | None = "success", **extra) -> dict:
    info: dict = {"callId": call_id}
    if status is not None:
        info["status"] = status
    response = {"functionResponse": {"id": call_id, "name": name, "response": {"output": output}}}
    return _rec(uid, "tool_result", {"role": "user", "parts": [response]}, toolCallResult=info, **extra)


def _session(*records: dict) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


def _turns(spans):
    return [s for s in spans if s.name.startswith("Turn ")]


def _tools(spans):
    return [s for s in spans if ":tool:" in s.span_id]


# --- detection ---------------------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        _prompt("u1"),
        _reply("a1", {"text": "hi"}),
        _result("r1", "c1", "read_file", "x"),
        _rec("s1", "system", subtype="slash_command", systemPayload={"phase": "invocation"}),
    ],
)
def test_every_record_type_is_detected_as_qwen_code(record):
    assert detect_agent(json.dumps(record).encode()) == AgentType.QWEN_CODE


def test_claude_code_records_are_not_claimed_as_qwen_code():
    line = {"type": "user", "sessionId": "s1", "uuid": "u1", "parentUuid": None,
            "message": {"role": "user", "content": "hi"}}
    assert detect_agent(json.dumps(line).encode()) == AgentType.CLAUDE_CODE


def test_opencode_records_are_not_claimed_as_qwen_code():
    line = {"message": {"role": "user", "sessionID": "s1"}, "parts": [{"type": "text", "text": "hi"}]}
    assert detect_agent(json.dumps(line).encode()) == AgentType.OPENCODE


# --- turns, tools, status ----------------------------------------------------


def test_prompt_reply_tools_and_results():
    data = _session(
        _prompt("u1", "first", ts=0),
        _reply("a1", {"text": "thinking", "thought": True}, {"text": "reading"},
               _call("c1", "read_file", absolute_path=f"{ROOT}/src/a.py"), ts=1),
        _result("r1", "c1", "read_file", "contents", ts=2),
        _reply("a2", _call("c2", "run_shell_command", command="pytest -q"), ts=3),
        _result("r2", "c2", "run_shell_command", "Exit Code: 1", status="error", ts=4),
        _reply("a3", {"text": "done"}, ts=5),
        _prompt("u2", "second", ts=6),
        _reply("a4", {"text": "ok"}, ts=7),
    )
    spans = parse_qwen_code(data, "local:qwen-code:s1")[1]
    root = spans[0]
    assert root.name == "Session" and root.parent_id is None
    assert root.attributes["harness.agent_session_id"] == "s1"
    assert root.attributes["harness.agent_version"] == "0.26.5"
    assert root.attributes["llm.model_name"] == "qwen3:8b"
    turns = _turns(spans)
    assert [t.attributes["input.value"] for t in turns] == ["first", "second"]
    # A thought is not output.
    assert turns[0].attributes["output.value"] == "reading\ndone"
    read, shell = _tools(spans)
    assert (read.name, read.status_code) == ("Read", "OK")
    assert (shell.name, shell.status_code) == ("Bash", "ERROR")
    assert read.attributes["output.value"] == "contents"
    assert {s.attributes["cwd"] for s in spans if s.parent_id is not None} == {ROOT}
    assert turns[0].end_time.isoformat().startswith("2026-11-02T10:00:05")


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("success", None, "OK"),
        ("error", None, "ERROR"),
        ("cancelled", None, "ERROR"),
        (None, "boom", "ERROR"),
        (None, None, "OK"),
    ],
)
def test_status_comes_from_the_tool_call_result(status, error, expected):
    result = _result("r1", "c1", "edit", "x", status=status)
    if error is not None:
        result["toolCallResult"]["error"] = {"message": error}
    data = _session(_prompt("u1"), _reply("a1", _call("c1", "edit", file_path=f"{ROOT}/a.py")), result)
    (tool,) = _tools(parse_qwen_code(data, "t")[1])
    assert tool.status_code == expected


def test_call_whose_result_is_missing_is_unset():
    data = _session(_prompt("u1"), _reply("a1", _call("c1", "glob", pattern="*.py")))
    (tool,) = _tools(parse_qwen_code(data, "t")[1])
    assert (tool.name, tool.status_code, tool.attributes["output.value"]) == ("Glob", "UNSET", "")


def test_result_is_matched_through_the_function_response_id():
    result = _result("r1", "c1", "read_file", "contents")
    del result["toolCallResult"]
    data = _session(_prompt("u1"), _reply("a1", _call("c1", "read_file", absolute_path=f"{ROOT}/a")), result)
    (tool,) = _tools(parse_qwen_code(data, "t")[1])
    assert (tool.status_code, tool.attributes["output.value"]) == ("OK", "contents")


def test_mid_turn_message_joins_the_running_turn_and_other_subtypes_open_none():
    data = _session(
        _prompt("u1", "first"),
        _reply("a1", {"text": "one"}),
        _prompt("u2", "steer", subtype="mid_turn_user_message"),
        _prompt("u3", "goal", subtype="goal_runtime"),
        _prompt("u4", "peer", subtype="agent_message"),
        _reply("a2", {"text": "two"}),
    )
    turns = _turns(parse_qwen_code(data, "t")[1])
    assert [t.attributes["input.value"] for t in turns] == ["first"]
    assert turns[0].attributes["output.value"] == "one\ntwo"


def test_sidechain_records_are_skipped():
    data = _session(
        _prompt("u1"),
        _reply("a1", _call("c9", "read_file", absolute_path=f"{ROOT}/x"), isSidechain=True),
        _reply("a2", {"text": "done"}),
    )
    assert _tools(parse_qwen_code(data, "t")[1]) == []


def test_path_keys_come_first_so_truncation_keeps_them():
    call = _call("c1", "write_file", content="x" * 4000, file_path=f"{ROOT}/a.txt")
    data = _session(_prompt("u1"), _reply("a1", call), _result("r1", "c1", "write_file", "ok"))
    (tool,) = _tools(parse_qwen_code(data, "t")[1])
    recorded = tool.attributes["input.value"]
    assert len(recorded) <= 2000
    assert recorded.startswith('{"file_path":')
    assert extract_file_path(recorded) == f"{ROOT}/a.txt"
    assert tool.name == "Write"
    assert tool.attributes["code.edit_evidence"]["status"] != "not_successful"


def test_tokens_are_counted_once_per_request():
    usage = {"promptTokenCount": 100, "cachedContentTokenCount": 40, "candidatesTokenCount": 20,
             "thoughtsTokenCount": 5, "totalTokenCount": 125}
    data = _session(_prompt("u1"), _reply("a1", {"text": "a"}, usage=usage), _reply("a2", {"text": "b"}, usage=usage))
    (turn,) = _turns(parse_qwen_code(data, "t")[1])
    a = turn.attributes
    assert a["llm.token_count.prompt"] == 200
    assert a["llm.token_count.cache_read"] == 80
    assert a["llm.token_count.input_fresh"] == 120
    assert a["llm.token_count.completion"] == 50
    assert a["llm.token_count.reasoning"] == 10
    assert len(a["llm.cost_events"]) == 2


def test_compression_becomes_a_compaction():
    compression = _rec("c1", "system", subtype="chat_compression",
                       systemPayload={"info": {"originalTokenCount": 9000, "newTokenCount": 1200},
                                      "compressedHistory": []})
    data = _session(_prompt("u1"), _reply("a1", {"text": "a"}), compression, _reply("a2", {"text": "b"}))
    root = parse_qwen_code(data, "t")[1][0]
    assert root.attributes["harness.compactions"] == [{"timestamp": compression["timestamp"], "pre_tokens": 9000}]
    doc = builder.build_map_from_spans(parse_qwen_code(data, "t")[1], session_id="t", agent_tool="qwen-code")
    assert doc["totals"]["compactions"] == 1


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json at all\n",
        b"[1, 2]\nnull\n",
        _session(_rec("s1", "system", subtype="slash_command", systemPayload={})),
        _session(_rec("u1", "user", None)),
        _session(_rec("u1", "user", {"role": "user", "parts": "x"}, timestamp="nope")),
        _session(_rec("a1", "assistant", {"role": "model", "parts": [{"functionCall": {"name": None, "args": []}}]},
                      model=3, usageMetadata="many")),
        _session(_rec("r1", "tool_result", {"parts": [{"functionResponse": {"id": 7}}]}, toolCallResult=[])),
    ],
)
def test_malformed_or_empty_input_does_not_crash(data):
    trace, spans = parse_qwen_code(data, "t")
    assert trace.trace_id == "t"
    assert isinstance(spans, list)


def test_empty_input_yields_no_spans():
    assert parse_qwen_code(b"", "t")[1] == []
    slash_only = _session(_rec("s1", "system", subtype="slash_command", systemPayload={}))
    assert parse_qwen_code(slash_only, "t")[1] == []


# --- discovery ---------------------------------------------------------------


def _qwen_home(tmp_path: Path, *sessions: tuple[str, bytes]) -> Path:
    home = tmp_path / ".qwen"
    chats = home / "projects" / "-Users-dev-project" / "chats"
    chats.mkdir(parents=True)
    for native_id, data in sessions:
        (chats / f"{native_id}.jsonl").write_bytes(data)
        # A sibling the indexer must not read as a transcript.
        (chats / f"{native_id}.runtime.json").write_text('{"pid": 1}', encoding="utf-8")
    # Subagent transcripts live outside `chats/` and are not walked.
    sub = home / "projects" / "-Users-dev-project" / "subagents" / "s1"
    sub.mkdir(parents=True)
    (sub / "agent-explore.jsonl").write_bytes(_session({**_prompt("x1"), "sessionId": "sub", "isSidechain": True}))
    return home


def _one(session_id: str) -> bytes:
    records = [_prompt("u1"), _reply("a1", _call("c1", "read_file", absolute_path=f"{ROOT}/a")),
               _result("r1", "c1", "read_file", "x"), _reply("a2", {"text": "done"})]
    return _session(*({**r, "sessionId": session_id} for r in records))


def test_local_source_indexes_qwen_code_sessions(tmp_path):
    slash_only = _session(_rec("s9", "system", subtype="slash_command", systemPayload={}))
    home = _qwen_home(tmp_path, ("sess-1", _one("sess-1")), ("slash-only", slash_only))
    source = LocalSource(claude_code_homes=[], codex_homes=[], qwen_code_homes=[home])
    index = source._build_index()
    assert sorted(index) == ["local:qwen-code:sess-1"]
    assert source.discovery_stats()["qwen-code"]["skipped"] == {"empty_events": 1}
    sid = "local:qwen-code:sess-1"
    assert source.get_cwd(sid) == ROOT
    assert source.get_project_name(sid) == "project"
    assert [s.name for s in _tools(source.get_spans_by_session(sid))] == ["Read"]


def test_a_claude_code_transcript_in_the_home_is_not_claimed(tmp_path):
    home = _qwen_home(tmp_path, ("sess-1", _one("sess-1")))
    line = {"type": "user", "sessionId": "c1", "message": {"role": "user", "content": "hi"}}
    chats = home / "projects" / "-Users-dev-project" / "chats"
    (chats / "claude.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    source = LocalSource(claude_code_homes=[], codex_homes=[], qwen_code_homes=[home])
    source._build_index()
    assert source.discovery_stats()["qwen-code"]["skipped"] == {"different_agent": 1}


def test_discovery_lists_qwen_code_with_env_override(tmp_path, monkeypatch):
    assert "qwen-code" in discover.AGENTS
    monkeypatch.setenv("AGENTPLAY_QWEN_HOMES", str(tmp_path))
    homes = discover.Homes.default()
    assert homes.qwen_code == [tmp_path]
    assert homes.present()["qwen-code"] == [tmp_path]
    monkeypatch.delenv("AGENTPLAY_QWEN_HOMES")
    assert discover.Homes.default().qwen_code == [Path.home() / ".qwen"]


def test_built_map_never_leaks_free_text():
    secret_prompt = "SECRET-PROMPT-TEXT"
    data = _session(
        _prompt("u1", secret_prompt),
        _reply("a1", {"text": "SECRET-THOUGHT", "thought": True}, {"text": "SECRET-REPLY"},
               _call("c1", "run_shell_command", command="git log --author=SECRET-ARG"),
               _call("c2", "write_file", file_path=f"{ROOT}/a.py", content="SECRET-CONTENT")),
        _result("r1", "c1", "run_shell_command", "SECRET-OUTPUT", status="error"),
        _result("r2", "c2", "write_file", "SECRET-WRITE-RESULT"),
    )
    spans = parse_qwen_code(data, "local:qwen-code:s1")[1]
    doc = builder.build_map_from_spans(spans, session_id="local:qwen-code:s1", agent_tool="qwen-code")
    raw = json.dumps(doc)
    assert "SECRET" not in raw
    assert ROOT not in raw
    kinds = [(e["kind"], e.get("verb"), e.get("err")) for e in doc["events"] if e.get("tool")]
    assert kinds == [("exec", "git log", True), ("edit", None, False)]
    assert doc["session"]["agentTool"] == "qwen-code"


def test_thinking_inside_candidates_is_not_counted_twice():
    # OpenAI-compatible providers report thoughts as a subset of candidates
    # (total = prompt + candidates); seen in the real fixtures.
    usage = {"promptTokenCount": 2050, "candidatesTokenCount": 758, "thoughtsTokenCount": 758,
             "totalTokenCount": 2808, "cachedContentTokenCount": 0}
    data = _session(_prompt("u1"), _reply("a1", {"text": "a"}, usage=usage))
    (turn,) = _turns(parse_qwen_code(data, "t")[1])
    assert turn.attributes["llm.token_count.completion"] == 758
    assert turn.attributes["llm.token_count.reasoning"] == 758


def test_without_a_total_thoughts_are_added_to_candidates():
    usage = {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 5}
    data = _session(_prompt("u1"), _reply("a1", {"text": "a"}, usage=usage))
    (turn,) = _turns(parse_qwen_code(data, "t")[1])
    assert turn.attributes["llm.token_count.completion"] == 25


# --- real-session fixtures ---------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "qwen_code"
NAMES = ("failed_read", "edit_then_read")


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.jsonl").read_bytes()


def _records(name: str) -> list[dict]:
    return [json.loads(line) for line in _fixture(name).splitlines() if line.strip()]


def _fixture_spans(name: str):
    return parse_qwen_code(_fixture(name), f"local:qwen-code:{name}")[1]


@pytest.mark.parametrize("name", NAMES)
def test_fixture_is_detected_as_qwen_code(name):
    assert detect_agent(_fixture(name)) == AgentType.QWEN_CODE


@pytest.mark.parametrize(
    ("name", "tools"),
    [
        ("failed_read", [("Read", "ERROR"), ("Read", "ERROR")]),
        ("edit_then_read", [("Write", "ERROR"), ("Read", "OK")]),
    ],
)
def test_fixture_turns_tools_and_status(name, tools):
    spans = _fixture_spans(name)
    (turn,) = _turns(spans)
    assert turn.attributes["input.value"] == "PROMPT-PLACEHOLDER-1"
    assert turn.attributes["output.value"] == "REPLY-PLACEHOLDER-1"
    assert [(s.name, s.status_code) for s in _tools(spans)] == tools
    assert {s.parent_id for s in _tools(spans)} == {turn.span_id}
    root = spans[0].attributes
    assert root["llm.model_name"] == "qwen3:8b"
    assert root["harness.agent_version"] == "0.26.5"
    assert root["harness.agent_session_id"] == _records(name)[0]["sessionId"]
    assert {s.attributes["cwd"] for s in spans if s.parent_id is not None} == {"/Users/dev/project"}


@pytest.mark.parametrize("name", NAMES)
def test_fixture_tokens_are_counted_once_per_request(name):
    usages = [r["usageMetadata"] for r in _records(name) if "usageMetadata" in r]
    (turn,) = _turns(_fixture_spans(name))
    a = turn.attributes
    assert a["llm.token_count.prompt"] == sum(u["promptTokenCount"] for u in usages)
    assert a["llm.token_count.completion"] == sum(u["totalTokenCount"] - u["promptTokenCount"] for u in usages)
    assert len(a["llm.cost_events"]) == len(usages)


def test_failed_write_records_unsuccessful_edit_evidence():
    write, read = _tools(_fixture_spans("edit_then_read"))
    assert extract_file_path(write.attributes["input.value"]) == "/Users/dev/project/src/file-1.py"
    assert write.attributes["code.edit_evidence"]["status"] == "not_successful"
    assert read.attributes["output.value"] == "OUT-PLACEHOLDER-3"


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
    doc = builder.build_map_from_spans(
        _fixture_spans(name), session_id=f"local:qwen-code:{name}", agent_tool="qwen-code"
    )
    raw = json.dumps(doc)
    # Every placeholder stands for prompt text, model output, a thought, an
    # argument, a tool output or an error message.
    for text in _fixture_strings(name):
        if "PLACEHOLDER" in text:
            assert text not in raw, text
    # Outside a git repository a path collapses to a bucket label.
    for part in ("/Users/dev/project", "file-1", "file-2"):
        assert part not in raw, part
    kinds = [(e["kind"], e.get("err")) for e in doc["events"] if e.get("tool")]
    expected = {
        "failed_read": [("read", True), ("read", True)],
        "edit_then_read": [("edit", True), ("read", False)],
    }[name]
    assert kinds == expected
    assert doc["totals"]["errors"] == sum(err for _, err in expected)
    assert doc["session"]["models"] == ["qwen3:8b"]


def test_local_source_indexes_the_fixtures(tmp_path):
    home = tmp_path / ".qwen"
    chats = home / "projects" / "-Users-dev-project" / "chats"
    chats.mkdir(parents=True)
    for name in NAMES:
        (chats / f"{_records(name)[0]['sessionId']}.jsonl").write_bytes(_fixture(name))
    source = LocalSource(claude_code_homes=[], codex_homes=[], qwen_code_homes=[home])
    index = source._build_index()
    assert sorted(index) == sorted(f"local:qwen-code:{_records(n)[0]['sessionId']}" for n in NAMES)
    sid = f"local:qwen-code:{_records('edit_then_read')[0]['sessionId']}"
    assert source.get_cwd(sid) == "/Users/dev/project"
    assert [s.name for s in _tools(source.get_spans_by_session(sid))] == ["Write", "Read"]
