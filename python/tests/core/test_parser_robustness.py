"""One malformed or unexpected record never drops a session or crashes a scan.

Real transcripts change schema often (new record types, partial usage blocks,
wrong-typed fields). Every parser must skip what it cannot read and still
produce the rest of the session. Fixtures are minimal and synthetic.

Two sweeps per parser:

* structural damage (truncated tail, bad JSON, non-dict JSON, BOM, non-UTF-8
  bytes, unknown record type) must leave the turn count unchanged;
* every field of every record replaced, one at a time, with a wrong-typed
  value must not raise.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    parse_claude_code,
    parse_codex,
    parse_command_code,
    parse_copilot,
    parse_copilot_vscode,
    parse_cursor_agent_transcript,
    parse_cursor_store,
    parse_gemini,
    parse_kiro,
    parse_opencode,
)

TS = "2026-09-14T10:00:0{}Z"
WRONG_TYPES = (None, 123, "str", [], {}, True)


def _claude() -> list[dict]:
    args = {"file_path": "/repo/app.py", "old_string": "old", "new_string": "new"}
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2}
    return [
        {
            "type": "user",
            "sessionId": "s1",
            "cwd": "/repo",
            "version": "2.1.0",
            "entrypoint": "cli",
            "timestamp": TS.format(0),
            "message": {"role": "user", "content": "fix it"},
        },
        {
            "type": "assistant",
            "requestId": "r1",
            "timestamp": TS.format(1),
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "usage": usage,
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "c1", "name": "Edit", "input": args},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": TS.format(2),
            "toolUseResult": {"filePath": "/repo/app.py"},
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "done"}],
            },
        },
    ]


def _codex() -> list[dict]:
    usage = {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5, "total_tokens": 15}

    def line(i: int, kind: str, payload: dict) -> dict:
        return {"timestamp": TS.format(i), "type": kind, "payload": payload}

    return [
        line(0, "session_meta", {"id": "s1", "cwd": "/repo", "originator": "codex-tui"}),
        line(1, "turn_context", {"model": "gpt-5", "cwd": "/repo"}),
        line(1, "event_msg", {"type": "user_message", "message": "fix it"}),
        line(
            2,
            "response_item",
            {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "c1"},
        ),
        line(3, "response_item", {"type": "function_call_output", "call_id": "c1", "output": "ok"}),
        line(
            4,
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        ),
        line(
            4,
            "event_msg",
            {
                "type": "token_count",
                "info": {"last_token_usage": usage, "total_token_usage": usage},
            },
        ),
    ]


def _copilot() -> list[dict]:
    def ev(i: int, kind: str, data: dict) -> dict:
        return {"type": kind, "data": data, "id": f"e{i}", "timestamp": TS.format(i)}

    return [
        ev(
            0,
            "session.start",
            {
                "sessionId": "s1",
                "producer": "copilot-agent",
                "startTime": TS.format(0),
                "context": {"cwd": "/repo"},
            },
        ),
        ev(1, "user.message", {"content": "fix it"}),
        ev(2, "assistant.turn_start", {"turnId": "0"}),
        ev(3, "assistant.message", {"messageId": "m1", "content": "ok", "outputTokens": 5}),
        ev(
            4,
            "tool.execution_start",
            {"toolCallId": "c1", "toolName": "bash", "arguments": {"command": "ls"}},
        ),
        ev(
            5,
            "tool.execution_complete",
            {
                "toolCallId": "c1",
                "model": "gpt-5",
                "success": True,
                "result": {"content": "ok"},
            },
        ),
        ev(6, "assistant.turn_end", {"turnId": "0"}),
    ]


def _copilot_vscode() -> list[dict]:
    request = {
        "requestId": "r1",
        "timestamp": 1_780_000_000_000,
        "elapsedMs": 1000,
        "isComplete": True,
        "modelId": "copilot/auto",
        "message": {"text": "fix it"},
        "response": [{"value": "ok"}],
        "result": {
            "usage": {"promptTokens": 10, "completionTokens": 5},
            "metadata": {
                "toolCallRounds": [
                    {"toolCalls": [{"id": "c1", "name": "run_in_terminal", "arguments": "{}"}]}
                ],
                "toolCallResults": {"c1": {"content": "ok"}},
            },
        },
    }
    return [{"kind": 0, "v": {"sessionId": "s1", "requests": [request]}}]


def _cursor_store() -> list[dict]:
    return [
        {
            "type": "cursor_store_session",
            "agentId": "a1",
            "name": "t",
            "createdAt": 1_780_000_000_000,
            "updatedAt": 1_780_000_060_000,
            "cwd": "/repo",
        },
        {"role": "user", "content": [{"type": "text", "text": "<user_query>fix it</user_query>"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool-call",
                    "toolCallId": "c1",
                    "toolName": "Read",
                    "args": {"path": "/repo/app.py"},
                },
            ],
        },
        {
            "role": "tool",
            "content": [
                {"type": "tool-result", "toolCallId": "c1", "toolName": "Read", "result": "x"}
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]


def _cursor_agent() -> list[dict]:
    return [
        {"role": "user", "message": {"content": [{"type": "text", "text": "fix it"}]}},
        {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "name": "Read", "input": {"path": "/repo/app.py"}},
                ]
            },
        },
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    ]


def _kiro() -> list[dict]:
    def ev(i: int, payload: dict) -> dict:
        return {"id": f"e{i}", "timestamp": TS.format(i), "payload": payload}

    return [
        {
            "type": "kiro_session",
            "id": "s1",
            "title": "t",
            "workspacePaths": ["/repo"],
            "createdAt": TS.format(0),
            "lastModifiedAt": TS.format(5),
        },
        ev(1, {"type": "user", "content": "fix it"}),
        ev(2, {"type": "assistant", "content": "ok"}),
        ev(
            3,
            {
                "type": "tool_call",
                "toolCallId": "c1",
                "toolName": "readFile",
                "args": {"path": "/repo/app.py"},
            },
        ),
        ev(4, {"type": "tool_result", "toolCallId": "c1", "content": "x", "success": True}),
        ev(5, {"type": "turn_end", "stopReason": "end_turn"}),
        ev(5, {"type": "usage_summary", "promptTurnSummaries": [{"usage": 1.5}]}),
    ]


def _opencode() -> list[dict]:
    created = 1_780_000_000_000
    return [
        {
            "id": "msg_1",
            "sessionID": "ses_1",
            "message": {"role": "user", "time": {"created": created}},
            "parts": [{"type": "text", "text": "fix it"}],
        },
        {
            "id": "msg_2",
            "sessionID": "ses_1",
            "message": {
                "role": "assistant",
                "modelID": "m",
                "path": {"cwd": "/repo"},
                "time": {"created": created + 1000},
                "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 1, "write": 2}},
            },
            "parts": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool",
                    "tool": "read",
                    "callID": "c1",
                    "state": {
                        "status": "completed",
                        "input": {"filePath": "/repo/app.py"},
                        "output": "x",
                        "time": {"start": created + 1000, "end": created + 1500},
                    },
                },
            ],
        },
    ]


def _gemini() -> list[dict]:
    shell = {
        "id": "c1",
        "name": "run_shell_command",
        "args": {"command": "ls", "description": "list"},
        "result": [{"functionResponse": {"id": "c1", "name": "run_shell_command", "response": {"output": "Output: x"}}}],
        "status": "success",
        "timestamp": TS.format(2),
    }
    reply = {
        "id": "g1",
        "timestamp": TS.format(1),
        "type": "gemini",
        "content": "",
        "thoughts": [{"subject": "s", "description": "d", "timestamp": TS.format(1)}],
        "tokens": {"input": 10, "output": 5, "cached": 2, "thoughts": 1, "tool": 0, "total": 16},
        "model": "gemini-3.6-flash",
    }
    return [
        {"sessionId": "s1", "projectHash": "h", "startTime": TS.format(0), "lastUpdated": TS.format(3), "kind": "main"},
        {"id": "u0", "timestamp": TS.format(0), "type": "user", "content": [{"text": "<session_context>x</session_context>"}]},
        {"id": "u1", "timestamp": TS.format(0), "type": "user", "content": [{"text": "list files"}]},
        {"$set": {"lastUpdated": TS.format(1)}},
        reply,
        {**reply, "toolCalls": [shell]},
        {"id": "u2", "timestamp": TS.format(2), "type": "user", "content": [shell["result"][0]]},
        {"id": "g2", "timestamp": TS.format(3), "type": "gemini", "content": "done", "model": "gemini-3.6-flash"},
    ]


def _command_code() -> list[dict]:
    def message(msg_id, role, source, content, ts, **extra):
        return {
            "type": "message",
            "id": msg_id,
            "parentId": None,
            "timestamp": TS.format(ts),
            **extra,
            "message": {"role": role, "content": content, "meta": {"source": source, "messageId": msg_id}},
        }

    usage = {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 2, "cacheWriteTokens": 0, "costUsd": 0.01}
    shell = {"type": "tool_use", "id": "c1", "name": "shell_command", "input": {"command": "ls", "cwd": "/repo"}}
    result = {"type": "tool_result", "tool_use_id": "c1", "content": [{"type": "text", "text": "Exit code: 1\nx"}]}
    return [
        {"type": "session", "id": "s1", "cwd": "/repo", "version": 3, "timestamp": TS.format(0)},
        message("u1", "user", "user", [{"type": "text", "text": "list files"}], 0),
        message("a1", "assistant", "model", [{"type": "thinking", "thinking": "t", "signature": ""}, shell], 1,
                model="m", usage=usage),
        message("r1", "user", "tool", [result], 2),
        message("a2", "assistant", "model", [{"type": "text", "text": "done"}], 3, model="m", usage=usage),
    ]


PARSERS = {
    "claude_code": (parse_claude_code, _claude),
    "codex": (parse_codex, _codex),
    "command_code": (parse_command_code, _command_code),
    "copilot": (parse_copilot, _copilot),
    "copilot_vscode": (parse_copilot_vscode, _copilot_vscode),
    "cursor_store": (parse_cursor_store, _cursor_store),
    "cursor_agent": (parse_cursor_agent_transcript, _cursor_agent),
    "gemini": (parse_gemini, _gemini),
    "kiro": (parse_kiro, _kiro),
    "opencode": (parse_opencode, _opencode),
}
UNKNOWN_RECORD = {"type": "agentplay-future-record", "payload": {"x": 1}}


def _encode(records: list[dict]) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


def _turns(parse, data: bytes) -> int:
    _, spans = parse(data, "t")
    return sum(1 for s in spans if s.name.startswith("Turn"))


def _paths(obj, prefix=()):
    """Every dict key / list index path inside ``obj`` (depth-first)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield (*prefix, key)
            yield from _paths(value, (*prefix, key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield (*prefix, i)
            yield from _paths(value, (*prefix, i))


def _assign(obj, path, value):
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


@pytest.fixture(autouse=True)
def _quiet(caplog):
    caplog.set_level(logging.CRITICAL)


@pytest.mark.parametrize("name", PARSERS)
def test_clean_fixture_yields_a_turn(name):
    parse, build = PARSERS[name]
    assert _turns(parse, _encode(build())) >= 1


def _damage_cases(clean: bytes) -> dict[str, bytes]:
    return {
        "truncated_last_line": clean + b'{"type": "user", "message": {"role": "us',
        "invalid_json_line": b"{not json}\n" + clean + b"{also not\n",
        "non_dict_json": b'[1, 2]\n42\n"str"\nnull\n' + clean,
        "unknown_record_type": clean + _encode([UNKNOWN_RECORD]),
        "bom_prefix": b"\xef\xbb\xbf" + clean,
        "non_utf8_bytes": clean + b'{"type": "x", "text": "\xff\xfe\xfa"}\n\xc3\x28\n',
        "blank_lines": b"\n\n" + clean.replace(b"\n", b"\n\n"),
    }


@pytest.mark.parametrize("name", PARSERS)
@pytest.mark.parametrize(
    "case",
    [
        "truncated_last_line",
        "invalid_json_line",
        "non_dict_json",
        "unknown_record_type",
        "bom_prefix",
        "non_utf8_bytes",
        "blank_lines",
    ],
)
def test_structural_damage_keeps_the_session(name, case):
    parse, build = PARSERS[name]
    clean = _encode(build())
    expected = _turns(parse, clean)
    assert _turns(parse, _damage_cases(clean)[case]) == expected


@pytest.mark.parametrize("name", PARSERS)
def test_wrong_typed_fields_never_raise(name):
    parse, build = PARSERS[name]
    base = build()
    failures = []
    for index, record in enumerate(base):
        for path in _paths(record):
            for bad in WRONG_TYPES:
                records = copy.deepcopy(base)
                _assign(records[index], path, bad)
                try:
                    parse(_encode(records), "t")
                except Exception as exc:
                    failures.append(f"record {index} {path} = {bad!r}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures[:25])


@pytest.mark.parametrize("name", PARSERS)
def test_wrong_typed_record_keeps_other_records(name):
    """A record whose every field is junk costs only itself."""
    parse, build = PARSERS[name]
    base = build()
    expected = _turns(parse, _encode(base))
    junk = {"type": 123, "timestamp": 123, "message": "str", "payload": None, "usage": [], "content": None}
    assert _turns(parse, _encode([*base, junk])) == expected


@pytest.mark.parametrize(
    "iterations",
    [
        "x",
        [None],
        [{"input_tokens": 1}],
        [{"type": "message", "input_tokens": "1", "output_tokens": 1}],
        [{"type": "message", "input_tokens": 1, "output_tokens": -1}],
        [{"type": "message", "input_tokens": 1, "output_tokens": 1, "model": ["x"]}],
    ],
)
def test_claude_malformed_usage_iterations_fall_back_to_top_level_usage(iterations):
    """Unusable per-attempt evidence must not abort a session whose top-level usage is fine."""
    records = _claude()
    records[1]["message"]["usage"]["iterations"] = iterations
    _, spans = parse_claude_code(_encode(records), "t")
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.attributes["llm.token_count.completion"] >= 5
