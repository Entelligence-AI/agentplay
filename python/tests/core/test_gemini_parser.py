"""Gemini CLI parser, detection and discovery.

The fixtures under ``fixtures/gemini/`` are three real Gemini CLI 0.65 sessions
with every prompt, reply, thought, path, file content, description, tool
output and command argument replaced by a placeholder. What survives is the
structure: record shapes, ids (renumbered), timestamps, token counts, model,
tool names and statuses, and the shell result labels with their exit code.

* ``read_explain``: reads two files and explains the project.
* ``edit_and_run``: reads a file, rewrites it, runs it.
* ``failed_shell``: runs a missing file (exit code 1), then recovers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentplay import builder, discover
from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._sources.local import source as local_source
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    AgentType,
    detect_agent,
    parse_gemini,
)
from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource
from agentplay._core.ingestion.coding_agent.file_paths import extract_file_path

FIXTURES = Path(__file__).parent / "fixtures" / "gemini"
FIXTURE_ROOT = "/Users/dev/project"
# The fixtures carry a zeroed `projectHash`; tests that need the indexer to
# accept `.project_root` stamp in the hash of FIXTURE_ROOT.
ZERO_HASH = "0" * 64


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.jsonl").read_bytes()


def _parse(name: str):
    _, spans = parse_gemini(_fixture(name), f"local:gemini-cli:{name}")
    return spans


def _turns(spans):
    return [s for s in spans if s.name.startswith("Turn ")]


def _tools(spans):
    return [s for s in spans if ":tool:" in s.span_id]


def _records(name: str) -> list[dict]:
    return [json.loads(line) for line in _fixture(name).splitlines() if line.strip()]


def _unique_gemini_tokens(name: str) -> list[dict]:
    """The token record of each distinct `gemini` message id (last copy wins)."""
    latest: dict[str, dict] = {}
    for rec in _records(name):
        if rec.get("type") == "gemini":
            latest[rec["id"]] = rec
    return [m["tokens"] for m in latest.values() if "tokens" in m]


@pytest.mark.parametrize("name", ["read_explain", "edit_and_run", "failed_shell"])
def test_fixture_is_detected_as_gemini_cli(name):
    assert detect_agent(_fixture(name)) == AgentType.GEMINI_CLI


def test_claude_code_records_are_not_claimed_as_gemini():
    line = {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "hi"}}
    assert detect_agent(json.dumps(line).encode()) == AgentType.CLAUDE_CODE


def test_message_without_header_is_still_detected():
    line = {"id": "g1", "timestamp": "2026-10-23T07:00:00Z", "type": "gemini", "content": ""}
    assert detect_agent(json.dumps(line).encode()) == AgentType.GEMINI_CLI


@pytest.mark.parametrize(
    ("name", "tools"),
    [
        ("read_explain", ["update_topic", "Read", "Read"]),
        ("edit_and_run", ["update_topic", "Read", "Write", "Bash"]),
        ("failed_shell", ["Bash", "Bash"]),
    ],
)
def test_turn_and_tool_counts(name, tools):
    spans = _parse(name)
    assert spans[0].name == "Session" and spans[0].parent_id is None
    turns = _turns(spans)
    # One prompt per session; the CLI's `<session_context>` message is not a turn.
    assert len(turns) == 1
    assert turns[0].attributes["input.value"] == "PROMPT-PLACEHOLDER-1"
    assert [s.name for s in _tools(spans)] == tools
    assert all(s.parent_id == turns[0].span_id for s in _tools(spans))
    assert all(s.attributes["tool.name"] == s.name for s in _tools(spans))


def test_failed_shell_call_is_an_error_and_the_retry_is_not():
    failed, retry = _tools(_parse("failed_shell"))
    # The CLI records `status: success` for both; the exit code is the signal.
    assert failed.status_code == "ERROR"
    assert retry.status_code == "OK"
    assert failed.attributes["code.edit_evidence"]["status"] == "not_successful"


def test_successful_sessions_have_no_errors():
    for name in ("read_explain", "edit_and_run"):
        assert {s.status_code for s in _tools(_parse(name))} == {"OK"}


def test_write_records_edit_evidence():
    write = next(s for s in _tools(_parse("edit_and_run")) if s.name == "Write")
    assert json.loads(write.attributes["input.value"])["file_path"] == f"{FIXTURE_ROOT}/file_1.js"
    assert write.attributes["code.edit_evidence"]["status"] != "not_successful"


def test_path_keys_survive_truncation_and_edit_evidence_keeps_the_full_write():
    # Gemini records `content` before `file_path`. A write larger than the
    # 2,000-character input cap used to shear the path off the retained prefix.
    content = "kept-line\n" + ("x" * 4000)
    file_path = f"{FIXTURE_ROOT}/file_1.js"
    dir_path = FIXTURE_ROOT
    args = {
        "content": content,
        "instruction": "y" * 500,
        "dir_path": dir_path,
        "file_path": file_path,
    }
    prompt = {"id": "u1", "timestamp": "2026-10-23T07:00:00Z", "type": "user", "content": [{"text": "go"}]}
    reply = {
        "id": "g1",
        "timestamp": "2026-10-23T07:00:01Z",
        "type": "gemini",
        "toolCalls": [
            {
                "id": "c-write",
                "name": "write_file",
                "args": args,
                "result": [{"functionResponse": {"id": "x", "name": "write_file", "response": {"output": "ok"}}}],
                "status": "success",
                "timestamp": "2026-10-23T07:00:02Z",
            }
        ],
    }
    (tool,) = _tools(parse_gemini(_session(prompt, reply), "t")[1])
    recorded = tool.attributes["input.value"]
    assert len(recorded) <= 2000
    assert recorded.startswith('{"file_path":')
    assert extract_file_path(recorded) == file_path
    assert f'"dir_path": {json.dumps(dir_path)}' in recorded
    assert tool.attributes["code.edit_evidence"] == extract_edit_evidence("Write", args, successful=True)


@pytest.mark.parametrize("name", ["read_explain", "edit_and_run", "failed_shell"])
def test_tokens_are_counted_once_per_request(name):
    turn = _turns(_parse(name))[0].attributes
    tokens = _unique_gemini_tokens(name)
    prompt = sum(t["input"] + t["tool"] for t in tokens)
    cached = sum(t["cached"] for t in tokens)
    completion = sum(t["output"] + t["thoughts"] for t in tokens)
    assert turn["llm.token_count.prompt"] == prompt
    assert turn["llm.token_count.cache_read"] == cached
    assert turn["llm.token_count.input_fresh"] == prompt - cached
    assert turn["llm.token_count.completion"] == completion
    assert turn["llm.token_count.reasoning"] == sum(t["thoughts"] for t in tokens)
    assert len(turn["llm.cost_events"]) == len(tokens)


def test_model_and_session_id():
    root = _parse("failed_shell")[0].attributes
    assert root["llm.model_name"] == "gemini-3.6-flash"
    assert root["harness.agent_session_id"] == "00000000-0000-4000-8000-000000000001"
    assert _parse("read_explain")[0].attributes["llm.model_name"] == "gemini-3-flash-preview"


def _session(*records: dict, session_id: str = "s1") -> bytes:
    header = {
        "sessionId": session_id,
        "projectHash": "h",
        "startTime": "2026-10-23T07:00:00Z",
        "kind": "main",
    }
    return b"".join(json.dumps(r).encode() + b"\n" for r in (header, *records))


def _call(name: str, status: str, response: dict | None = None, **args) -> dict:
    return {
        "id": f"c-{name}-{status}",
        "name": name,
        "args": args,
        "result": [{"functionResponse": {"id": "x", "name": name, "response": response or {"output": "ok"}}}],
        "status": status,
        "timestamp": "2026-10-23T07:00:02Z",
    }


def test_status_mapping_for_other_tools():
    prompt = {"id": "u1", "timestamp": "2026-10-23T07:00:00Z", "type": "user", "content": [{"text": "go"}]}
    reply = {
        "id": "g1",
        "timestamp": "2026-10-23T07:00:01Z",
        "type": "gemini",
        "content": "",
        "toolCalls": [
            _call("replace", "success", file_path="/p/a.py", old_string="a\n", new_string="b\n"),
            _call("read_file", "error", file_path="/p/missing.py"),
            _call("write_file", "cancelled", file_path="/p/b.py", content="x"),
            _call("grep_search", "success", {"error": "bad pattern"}, pattern="x"),
            _call("run_shell_command", "success", {"output": "Output: (empty)\nSignal: 15"}, command="sleep 9"),
            _call("mcp_server_tool", "success"),
        ],
    }
    tools = _tools(parse_gemini(_session(prompt, reply), "t")[1])
    assert [(s.name, s.status_code) for s in tools] == [
        ("Edit", "OK"),
        ("Read", "ERROR"),
        ("Write", "ERROR"),
        ("Grep", "ERROR"),
        ("Bash", "ERROR"),
        ("mcp_server_tool", "OK"),
    ]


def test_exit_code_printed_by_the_program_is_not_the_verdict():
    output = "Output: Exit Code: 3\nProcess Group PGID: 1"
    prompt = {"id": "u1", "timestamp": "2026-10-23T07:00:00Z", "type": "user", "content": [{"text": "go"}]}
    reply = {
        "id": "g1",
        "timestamp": "2026-10-23T07:00:01Z",
        "type": "gemini",
        "toolCalls": [_call("run_shell_command", "success", {"output": output}, command="cat log")],
    }
    (tool,) = _tools(parse_gemini(_session(prompt, reply), "t")[1])
    assert tool.status_code == "OK"


def _shell_status(output: str) -> str:
    prompt = {"id": "u1", "timestamp": "2026-10-23T07:00:00Z", "type": "user", "content": [{"text": "go"}]}
    reply = {
        "id": "g1",
        "timestamp": "2026-10-23T07:00:01Z",
        "type": "gemini",
        "toolCalls": [_call("run_shell_command", "success", {"output": output}, command="cat log")],
    }
    (tool,) = _tools(parse_gemini(_session(prompt, reply), "t")[1])
    return tool.status_code


@pytest.mark.parametrize(
    "output",
    [
        # The program prints a standalone label line, the command succeeds, and
        # the CLI appends no failure label of its own.
        "Output: Exit Code: 3\nExit Code: 3\ndone\nProcess Group PGID: 1",
        "Output: start\nSignal: SIGTERM\ndone\nProcess Group PGID: 1",
        "Output: Exit Code: 3\nSignal: SIGTERM\ndone",
        # The label sits before the CLI's own `Error:` line, so it is output.
        "Output: Exit Code: 3\nError: spawn failed\nProcess Group PGID: 1",
    ],
)
def test_label_lines_in_program_output_are_not_the_verdict(output):
    assert _shell_status(output) == "OK"


@pytest.mark.parametrize(
    "output",
    [
        "Output: x\nExit Code: 1\nProcess Group PGID: 1",
        "Output: x\nError: boom\nExit Code: 127",
        "Output: (empty)\nSignal: SIGTERM\nBackground PIDs: 7, 8\nProcess Group PGID: 1",
        "Output: Exit Code: 0\nExit Code: 2\nSignal: 15\nProcess Group PGID: 1",
    ],
)
def test_the_clis_trailing_verdict_marks_failure(output):
    assert _shell_status(output) == "ERROR"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json at all\n",
        b"[1, 2]\nnull\n",
        _session(),  # header only: a session opened and closed with no prompt
        _session({"$set": {"lastUpdated": "2026-10-23T07:00:09Z"}}),
        _session({"id": "u1", "type": "user", "content": 5, "timestamp": "nope"}),
        _session({"id": "g1", "type": "gemini", "toolCalls": None, "tokens": "many", "model": 3}),
        _session({"id": "g1", "type": "gemini", "toolCalls": [{"name": None, "args": [], "result": "x"}]}),
    ],
)
def test_malformed_or_empty_input_does_not_crash(data):
    trace, spans = parse_gemini(data, "t")
    assert trace.trace_id == "t"
    assert isinstance(spans, list)


def test_empty_input_yields_no_spans():
    assert parse_gemini(b"", "t")[1] == []
    assert parse_gemini(_session(), "t")[1] == []


def _map(name: str) -> dict:
    _, spans = parse_gemini(_fixture(name), f"local:gemini-cli:{name}")
    for span in spans:
        if span.parent_id is not None:
            span.attributes["cwd"] = FIXTURE_ROOT
    return builder.build_map_from_spans(spans, session_id=f"local:gemini-cli:{name}", agent_tool="gemini-cli")


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


# Strings the map may carry: identifiers the redaction contract allows (model
# and tool names), the CLI's own labels, and values with no free text.
_ALLOWED = {"main", "user", "gemini", "success", "", "gemini-3.6-flash", "gemini-3-flash-preview"}


@pytest.mark.parametrize("name", ["read_explain", "edit_and_run", "failed_shell"])
def test_built_map_never_leaks_fixture_text(name):
    doc = _map(name)
    raw = json.dumps(doc)
    for text in _fixture_strings(name):
        if text in _ALLOWED or "PLACEHOLDER" not in text and "ARG-" not in text:
            continue
        # Every placeholder stands for prompt text, model output, a thought, a
        # file's contents, a tool description or output, or a command argument.
        assert text not in raw, text
    # Outside a git repository a path collapses to a bucket label, so neither
    # the absolute path nor the file name may appear.
    for part in ("PLACEHOLDER", "ARG-1", "ARG-2", "session_context", FIXTURE_ROOT, "file_1", "file_2"):
        assert part not in raw, part
    assert doc["session"]["agentTool"] == "gemini-cli"


def test_built_map_classifies_reads_edits_shell_and_the_failure():
    def kinds(name: str) -> list[tuple]:
        return [(e["kind"], e.get("verb"), e.get("err")) for e in _map(name)["events"] if e.get("tool")]

    assert kinds("read_explain") == [("tool", None, False), ("read", None, False), ("read", None, False)]
    assert kinds("edit_and_run")[1:] == [("read", None, False), ("edit", None, False), ("exec", "node", False)]
    assert kinds("failed_shell") == [("exec", "node", True), ("exec", "node", False)]
    failed = _map("failed_shell")
    assert failed["totals"]["errors"] == 1
    assert failed["session"]["models"] == ["gemini-3.6-flash"]


def _with_project_hash(data: bytes, root: str) -> bytes:
    """A fixture whose header hashes `root`, as the CLI writes it."""
    real = hashlib.sha256(root.encode("utf-8")).hexdigest()
    return data.replace(ZERO_HASH.encode(), real.encode(), 1)


def _gemini_home(tmp_path: Path, root: str = FIXTURE_ROOT) -> Path:
    home = tmp_path / ".gemini"
    project = home / "tmp" / "project"
    (project / "chats").mkdir(parents=True)
    (project / ".project_root").write_text(root, encoding="utf-8")
    for name in ("read_explain", "edit_and_run", "failed_shell"):
        (project / "chats" / f"session-2026-10-23T07-00-{name}.jsonl").write_bytes(
            _with_project_hash(_fixture(name), FIXTURE_ROOT)
        )
    (project / "chats" / "session-2026-10-23T07-01-empty.jsonl").write_bytes(_session())
    (project / "logs.json").write_text("[]", encoding="utf-8")
    return home


def _context_user(msg_id: str, *texts: str) -> dict:
    return {
        "id": msg_id,
        "timestamp": "2026-10-23T07:02:00Z",
        "type": "user",
        "content": [{"text": text} for text in texts],
    }


def test_context_only_session_is_skipped_as_empty_events(tmp_path):
    home = _gemini_home(tmp_path)
    chats = home / "tmp" / "project" / "chats"
    context = "<session_context>\nCONTEXT-PLACEHOLDER\n</session_context>"
    (chats / "session-context-only.jsonl").write_bytes(
        _session(_context_user("u0", context), session_id="context-only")
    )
    # A context block plus a real prompt is a session. So is model output
    # that arrived with no prompt of its own.
    (chats / "session-context-plus-prompt.jsonl").write_bytes(
        _session(
            _context_user("u1", context, "PROMPT-PLACEHOLDER-1"),
            session_id="context-plus-prompt",
        )
    )
    (chats / "session-context-and-model.jsonl").write_bytes(
        _session(
            _context_user("u2", context),
            {
                "id": "g1",
                "timestamp": "2026-10-23T07:03:01Z",
                "type": "gemini",
                "content": "MODEL-TEXT-PLACEHOLDER",
                "model": "gemini-3-flash-preview",
            },
            session_id="context-and-model",
        )
    )
    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[home])
    index = source._build_index()
    assert "local:gemini-cli:context-only" not in index
    assert "local:gemini-cli:context-plus-prompt" in index
    assert "local:gemini-cli:context-and-model" in index
    # Header-only file from `_gemini_home`, plus the context-only session.
    assert source.discovery_stats()["gemini-cli"]["skipped"]["empty_events"] == 2


def test_empty_text_user_record_is_skipped_as_empty_events(tmp_path):
    home = _gemini_home(tmp_path)
    chats = home / "tmp" / "project" / "chats"
    # `""` is dropped by `_user_prompt` (`if text`). Whitespace is not.
    (chats / "session-empty-text.jsonl").write_bytes(
        _session(_context_user("u0", "", ""), session_id="empty-text")
    )
    (chats / "session-whitespace-prompt.jsonl").write_bytes(
        _session(_context_user("u1", "   "), session_id="whitespace-prompt")
    )
    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[home])
    index = source._build_index()
    assert "local:gemini-cli:empty-text" not in index
    assert "local:gemini-cli:whitespace-prompt" in index
    # Header-only file from `_gemini_home`, plus the empty-text session.
    assert source.discovery_stats()["gemini-cli"]["skipped"]["empty_events"] == 2


def test_prompt_past_the_head_is_indexed_and_empty_sessions_are_skipped(tmp_path):
    # `_read_head` returns at most 5MB plus one 64KB chunk once the header has
    # identified the agent. A context record larger than that hides the first
    # real prompt from a head-only check.
    home = tmp_path / ".gemini"
    chats = home / "tmp" / "project" / "chats"
    chats.mkdir(parents=True)
    (chats.parent / ".project_root").write_text(FIXTURE_ROOT, encoding="utf-8")
    past_head = "<session_context>\n" + ("x" * (5_000_000 + 65_536)) + "\n</session_context>"
    prompt = {
        "id": "u1",
        "timestamp": "2026-10-23T07:04:00Z",
        "type": "user",
        "content": [{"text": "PROMPT-PLACEHOLDER-1"}],
    }
    model = {
        "id": "g1",
        "timestamp": "2026-10-23T07:04:01Z",
        "type": "gemini",
        "content": "MODEL-TEXT-PLACEHOLDER",
        "model": "gemini-3-flash-preview",
    }
    context = "<session_context>\nCONTEXT-PLACEHOLDER\n</session_context>"
    files = {
        "session-prompt-past-head.jsonl": (_context_user("u0", past_head), prompt, "prompt-past-head"),
        "session-model-past-head.jsonl": (_context_user("u0", past_head), model, "model-past-head"),
        "session-context-only.jsonl": (
            _context_user("u0", context),
            None,
            "context-only",
        ),
        "session-empty-text.jsonl": (_context_user("u0", ""), None, "empty-text"),
    }
    for name, (first, second, session_id) in files.items():
        records = (first,) if second is None else (first, second)
        (chats / name).write_bytes(_session(*records, session_id=session_id))

    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[home])
    index = source._build_index()
    assert "local:gemini-cli:prompt-past-head" in index
    assert "local:gemini-cli:model-past-head" in index
    assert "local:gemini-cli:context-only" not in index
    assert "local:gemini-cli:empty-text" not in index
    assert source.discovery_stats()["gemini-cli"]["skipped"]["empty_events"] == 2


def _chats(tmp_path: Path) -> Path:
    home = tmp_path / ".gemini"
    chats = home / "tmp" / "project" / "chats"
    chats.mkdir(parents=True)
    (chats.parent / ".project_root").write_text(FIXTURE_ROOT, encoding="utf-8")
    return chats


def _user_record(msg_id: str, content: object) -> dict:
    return {"id": msg_id, "timestamp": "2026-10-23T07:02:00Z", "type": "user", "content": content}


def test_non_prompt_user_content_is_skipped_as_empty_events(tmp_path):
    # Same shapes `_user_prompt` refuses: a non-text value, empty text, and
    # the CLI's context block. Each file is only that record.
    chats = _chats(tmp_path)
    context = "<session_context>\nCONTEXT-PLACEHOLDER\n</session_context>"
    cases = {
        "number": 5,
        "blank": "",
        "empty-part": [{"text": ""}],
        "context": [{"text": context}],
    }
    for session_id, content in cases.items():
        (chats / f"session-{session_id}.jsonl").write_bytes(
            _session(_user_record("u0", content), session_id=session_id)
        )
    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[chats.parents[2]])
    index = source._build_index()
    assert set(index) == set()
    assert source.discovery_stats()["gemini-cli"]["skipped"]["empty_events"] == len(cases)


def test_deeply_nested_json_line_does_not_abort_discovery(tmp_path, monkeypatch):
    nested = b'{"a":' * 1200 + b"0" + b"}" * 1200
    try:
        json.loads(nested)
    except RecursionError:
        pass
    else:
        # Python 3.15 parses this depth. Older interpreters raise here, which
        # is the failure the scan has to survive, so reproduce it.
        real_loads = local_source.json.loads

        def loads(raw, *args, **kwargs):
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
            if isinstance(text, str) and text.startswith('{"a":' * 40):
                raise RecursionError("maximum recursion depth exceeded")
            return real_loads(raw, *args, **kwargs)

        monkeypatch.setattr(local_source.json, "loads", loads)

    chats = _chats(tmp_path)
    prompt = _user_record("u1", [{"text": "PROMPT-PLACEHOLDER-1"}])
    prompt_line = json.dumps(prompt).encode() + b"\n"
    header = json.dumps(
        {"sessionId": "nested-then-prompt", "projectHash": "h", "startTime": "2026-10-23T07:00:00Z", "kind": "main"}
    ).encode() + b"\n"
    (chats / "session-nested-then-prompt.jsonl").write_bytes(header + nested + b"\n" + prompt_line)
    only = header.replace(b"nested-then-prompt", b"nested-only")
    (chats / "session-nested-only.jsonl").write_bytes(only + nested + b"\n")

    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[chats.parents[2]])
    index = source._build_index()
    assert "local:gemini-cli:nested-then-prompt" in index
    assert "local:gemini-cli:nested-only" not in index
    assert source.discovery_stats()["gemini-cli"]["skipped"]["empty_events"] == 1


def test_oversized_transcript_is_skipped_as_too_large(tmp_path, monkeypatch):
    # The real cap is 512MB. Lower it so the test file is over the cap without
    # writing one. The prompt sits at the start, so a scan that ignored the
    # size and stopped at the cap would still index this session.
    monkeypatch.setattr(local_source, "MAX_SOURCE_BYTES", 800)
    chats = _chats(tmp_path)
    prompt = _user_record("u1", [{"text": "PROMPT-PLACEHOLDER-1"}])
    (chats / "session-small.jsonl").write_bytes(_session(prompt, session_id="small"))
    huge = _session(prompt, {"id": "pad", "type": "info", "content": "x" * 2000}, session_id="huge")
    assert len(huge) > 800
    (chats / "session-huge.jsonl").write_bytes(huge)

    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[chats.parents[2]])
    index = source._build_index()
    assert "local:gemini-cli:small" in index
    assert "local:gemini-cli:huge" not in index
    assert source.discovery_stats()["gemini-cli"]["skipped"]["too_large"] == 1


def test_complete_final_line_at_the_byte_cap_is_a_prompt(tmp_path, monkeypatch):
    # The only real prompt is the last line, it has no trailing newline, and
    # the file length is exactly the cap. That line is a whole record.
    chats = _chats(tmp_path)
    header = json.dumps(
        {
            "sessionId": "at-cap",
            "projectHash": "h",
            "startTime": "2026-10-23T07:00:00Z",
            "kind": "main",
        }
    ).encode() + b"\n"
    prompt = json.dumps(_user_record("u1", [{"text": "PROMPT-PLACEHOLDER-1"}])).encode()
    data = header + prompt
    assert not data.endswith(b"\n")
    monkeypatch.setattr(local_source, "MAX_SOURCE_BYTES", len(data))
    (chats / "session-at-cap.jsonl").write_bytes(data)

    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[chats.parents[2]])
    index = source._build_index()
    assert "local:gemini-cli:at-cap" in index


def test_local_source_indexes_gemini_sessions(tmp_path):
    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[_gemini_home(tmp_path)])
    index = source._build_index()
    # The three fixtures share one renumbered session id; the largest copy wins.
    assert list(index) == ["local:gemini-cli:00000000-0000-4000-8000-000000000001"]
    stats = source.discovery_stats()["gemini-cli"]
    assert stats["skipped"] == {"empty_events": 1}
    sid = next(iter(index))
    assert source.get_cwd(sid) == FIXTURE_ROOT
    assert source.get_project_name(sid) == "project"
    spans = source.get_spans_by_session(sid)
    assert {s.attributes["cwd"] for s in spans if s.parent_id is not None} == {FIXTURE_ROOT}


def test_project_root_that_does_not_match_the_hash_is_ignored(tmp_path):
    home = _gemini_home(tmp_path, root="/Users/dev/other")
    source = LocalSource(claude_code_homes=[], codex_homes=[], gemini_homes=[home])
    sid = next(iter(source._build_index()))
    assert source.get_cwd(sid) is None
    # The header still hashes FIXTURE_ROOT, so the mismatch is what rejects it.
    header = json.loads(_with_project_hash(_fixture("read_explain"), FIXTURE_ROOT).splitlines()[0])
    assert header["projectHash"] == hashlib.sha256(FIXTURE_ROOT.encode()).hexdigest()


@pytest.mark.parametrize("name", ["read_explain", "edit_and_run", "failed_shell"])
def test_fixtures_carry_no_real_project_hash(name):
    assert _records(name)[0]["projectHash"] == ZERO_HASH


def test_discovery_lists_gemini_cli_with_env_override(tmp_path, monkeypatch):
    assert "gemini-cli" in discover.AGENTS
    monkeypatch.setenv("AGENTPLAY_GEMINI_HOMES", str(tmp_path))
    homes = discover.Homes.default()
    assert homes.gemini_cli == [tmp_path]
    assert homes.present()["gemini-cli"] == [tmp_path]
    monkeypatch.delenv("AGENTPLAY_GEMINI_HOMES")
    assert discover.Homes.default().gemini_cli == [Path.home() / ".gemini"]
