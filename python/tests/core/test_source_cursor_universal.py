"""— one source-cursor contract, every agent parser.

Every turn and tool span carries ``source.kind`` and ``source.content_hash``;
append-only JSONL parsers also carry ``source.byte_offset``. The hash is the
universal verifier (stable across a rewrite of the same content), the offset
the seek point (stable when the file grows). The contract is pinned here once
for every parser.
"""

from __future__ import annotations

import json

from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    SOURCE_HASH_KEY,
    SOURCE_OFFSET_KEY,
    parse_jsonl_records,
    record_hash,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    claude_code,
    codex,
    copilot,
    cursor_agent_transcript,
    cursor_store,
    kiro,
    opencode,
    windsurf,
)


def _jl(*objs: dict) -> bytes:
    return ("\n".join(json.dumps(o) for o in objs)).encode()


def _claude(n: int = 1) -> list[dict]:
    return [
        {
            "type": "user",
            "uuid": f"u{n}",
            "timestamp": "2026-09-01T00:00:01Z",
            "message": {"role": "user", "content": "hi"},
            "cwd": "/w",
            "sessionId": "s",
        },
        {
            "type": "assistant",
            "uuid": f"a{n}",
            "timestamp": "2026-09-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]


def _codex() -> list[dict]:
    return [
        {
            "type": "session_meta",
            "timestamp": "2026-09-01T00:00:00Z",
            "payload": {"cwd": "/w", "id": "s1"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-01T00:00:01Z",
            "payload": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-01T00:00:02Z",
            "payload": {
                "type": "function_call",
                "call_id": "c1",
                "name": "shell",
                "arguments": "{}",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-01T00:00:03Z",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        },
    ]


def _kiro() -> list[dict]:
    return [
        {"type": "kiro_session", "cwd": "/w"},
        {"timestamp": "2026-09-01T00:00:01Z", "payload": {"type": "user", "content": "hi"}},
        {
            "timestamp": "2026-09-01T00:00:02Z",
            "payload": {"type": "tool_call", "toolCallId": "t1", "toolName": "read", "args": {}},
        },
        {
            "timestamp": "2026-09-01T00:00:03Z",
            "payload": {
                "type": "tool_result",
                "toolCallId": "t1",
                "content": "ok",
                "success": True,
            },
        },
    ]


def _opencode() -> list[dict]:
    return [
        {
            "message": {"role": "user", "time": {"created": 1756684800000}, "path": {"cwd": "/w"}},
            "parts": [{"type": "text", "text": "hi"}],
        },
        {
            "message": {"role": "assistant", "time": {"created": 1756684801000}, "modelID": "m"},
            "parts": [
                {
                    "type": "tool",
                    "callID": "t1",
                    "tool": "read",
                    "state": {"input": {}, "output": "ok", "status": "completed"},
                }
            ],
        },
    ]


def _cursor_transcript() -> list[dict]:
    return [
        {"role": "user", "message": {"content": "hi"}},
        {
            "role": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}]},
        },
    ]


def _cursor_store() -> list[dict]:
    return [
        {
            "type": "cursor_store_session",
            "agentId": "a1",
            "name": "n",
            "createdAt": 1756684800000,
            "updatedAt": 1756684900000,
        },
        {
            "role": "user",
            "createdAt": "2026-09-01T00:00:01Z",
            "content": [{"type": "text", "text": "hi"}],
        },
        {
            "role": "assistant",
            "createdAt": "2026-09-01T00:00:02Z",
            "content": [{"type": "tool_call", "toolCallId": "t1", "name": "read", "args": {}}],
        },
        {"role": "tool", "createdAt": "2026-09-01T00:00:03Z", "toolCallId": "t1", "result": "ok"},
    ]


def _copilot() -> list[dict]:
    # Minimal Copilot CLI shape: session.start, a user message, a tool request + completion.
    return [
        {
            "type": "session.start",
            "timestamp": "2026-09-01T00:00:00Z",
            "data": {"sessionId": "s", "cwd": "/w", "context": {"cwd": "/w"}},
        },
        {"type": "user.message", "timestamp": "2026-09-01T00:00:01Z", "data": {"content": "hi"}},
        {"type": "assistant.turn_start", "timestamp": "2026-09-01T00:00:02Z", "data": {}},
        {
            "type": "tool.execution_start",
            "timestamp": "2026-09-01T00:00:03Z",
            "data": {"toolCallId": "t1", "toolName": "read", "arguments": {}},
        },
        {
            "type": "tool.execution_complete",
            "timestamp": "2026-09-01T00:00:04Z",
            "data": {"toolCallId": "t1", "result": {"content": "ok"}, "success": True},
        },
    ]


def _windsurf() -> list[dict]:
    return [
        {"type": "windsurf_session", "cwd": "/w"},
        {"step_id": 1, "step_type": 19, "timestamp": "2026-09-01T00:00:01Z", "user_input": "hi"},
        {
            "step_id": 2,
            "step_type": 3,
            "timestamp": "2026-09-01T00:00:02Z",
            "tool_calls": [{"tool_id": "t1", "tool_name": "read_file", "params": {}}],
        },
        {
            "step_id": 3,
            "step_type": 21,
            "timestamp": "2026-09-01T00:00:03Z",
            "tool_id": "t1",
            "tool_result": "ok",
        },
    ]


_PARSERS = {
    # name: (parser, fixture, expected kind, (turn line index, tool line index))
    # — the fixture line each anchor MUST come from: the line that opened the
    # turn, and the line that requested the tool call ("some
    # parsed line" is not the contract). None ⇒ a rewritten store: hash only.
    "claude_code": (claude_code.parse_claude_code, _claude, "jsonl", (0, 1)),
    "codex": (codex.parse_codex, _codex, "jsonl", (1, 2)),
    "kiro": (kiro.parse_kiro, _kiro, "jsonl", (1, 2)),
    # Review-caught: opencode has no per-session file — it's
    # re-materialized from a SQLite db into NDJSON fresh on every parse, so
    # the NDJSON byte offset is a position in a throwaway intermediate, not
    # a stable transcript location. Rewritten store ⇒ hash only, like
    # cursor_store.
    "opencode": (opencode.parse_opencode, _opencode, "sqlite", None),
    "cursor_agent_transcript": (
        cursor_agent_transcript.parse_cursor_agent_transcript,
        _cursor_transcript,
        "jsonl",
        (0, 1),
    ),
    "cursor_store": (cursor_store.parse_cursor_store, _cursor_store, "sqlite", None),
    "copilot": (copilot.parse_copilot, _copilot, "jsonl", (1, 3)),
    "windsurf": (windsurf.parse_windsurf, _windsurf, "jsonl", (1, 2)),
}


def _anchored(spans):
    return [s for s in spans if s.name.startswith("Turn ") or s.attributes.get("tool.name")]


def _parse(name):
    parser, fixture, _kind, _lines = _PARSERS[name]
    return parser(_jl(*fixture()), "trace-1")[1]


def test_every_parser_stamps_its_own_kind_and_a_hash_on_every_turn_and_tool_span():
    for name, (_parser, _fixture, kind, lines) in _PARSERS.items():
        spans = _anchored(_parse(name))
        assert spans, name
        for span in spans:
            attrs = span.attributes
            # The parser-specific kind, not "some kind".
            assert attrs.get("source.kind") == kind, (name, span.name, attrs)
            assert (
                isinstance(attrs.get("source.content_hash"), str)
                and len(attrs["source.content_hash"]) == 16
            ), (name, span.name)
            if lines is not None:
                assert isinstance(attrs.get("source.byte_offset"), int), (name, span.name, attrs)
            else:
                assert "source.byte_offset" not in attrs, (name, span.name)


def test_jsonl_parsers_anchor_the_turn_to_its_opening_line_and_the_tool_to_the_request_line():
    for name, (_parser, fixture, _kind, lines) in _PARSERS.items():
        if lines is None:
            continue
        records = parse_jsonl_records(_jl(*fixture()))
        turn_line, tool_line = lines
        expected_turn = (
            records[turn_line][SOURCE_OFFSET_KEY],
            records[turn_line][SOURCE_HASH_KEY],
        )
        expected_tool = (
            records[tool_line][SOURCE_OFFSET_KEY],
            records[tool_line][SOURCE_HASH_KEY],
        )
        spans = _anchored(_parse(name))
        turn = next(s for s in spans if s.name.startswith("Turn "))
        tool = next(s for s in spans if s.attributes.get("tool.name"))
        # Exactly the fixture line that opened the turn / requested the call
        # (not merely "one of the parsed lines").
        assert (
            turn.attributes["source.byte_offset"],
            turn.attributes["source.content_hash"],
        ) == expected_turn, (name, "turn")
        assert (
            tool.attributes["source.byte_offset"],
            tool.attributes["source.content_hash"],
        ) == expected_tool, (name, "tool")


def test_hashes_survive_a_rewrite_and_offsets_survive_growth():
    for name, (parser, fixture, _kind, lines) in _PARSERS.items():
        before = {s.name: s.attributes for s in _anchored(_parse(name))}
        # Rewrite: same content, different serialization (key order + spacing).
        rewritten = (
            "\n".join(json.dumps(o, sort_keys=True, separators=(", ", ": ")) for o in fixture())
        ).encode()
        after = {s.name: s.attributes for s in _anchored(parser(rewritten, "trace-1")[1])}
        for span_name, attrs in before.items():
            assert after[span_name]["source.content_hash"] == attrs["source.content_hash"], (
                name,
                span_name,
            )
        if lines is not None:
            grown = _jl(*fixture(), {"type": "trailing", "noise": True})
            grown_attrs = {s.name: s.attributes for s in _anchored(parser(grown, "trace-1")[1])}
            for span_name, attrs in before.items():
                assert (
                    grown_attrs[span_name]["source.byte_offset"] == attrs["source.byte_offset"]
                ), (
                    name,
                    span_name,
                )


def test_codex_web_search_calls_are_anchored_too():
    # Review-caught: a synthetic web_search tool record must carry its line.
    lines = [
        {
            "type": "session_meta",
            "timestamp": "2026-09-01T00:00:00Z",
            "payload": {"cwd": "/w", "id": "s1"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-01T00:00:01Z",
            "payload": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-01T00:00:02Z",
            "payload": {"type": "web_search_call", "action": {"query": "x"}, "status": "completed"},
        },
    ]
    records = parse_jsonl_records(_jl(*lines))
    spans = codex.parse_codex(_jl(*lines), "trace-1")[1]
    tools = [s for s in spans if s.attributes.get("tool.name")]
    assert tools, "web_search should produce a tool span"
    assert tools[0].attributes["source.byte_offset"] == records[2][SOURCE_OFFSET_KEY]
    assert tools[0].attributes["source.content_hash"] == records[2][SOURCE_HASH_KEY]


def test_record_hash_ignores_the_parsers_own_stamps():
    rec = {"a": 1, "b": [1, 2]}
    stamped = {**rec, SOURCE_OFFSET_KEY: 42, SOURCE_HASH_KEY: "x"}
    assert record_hash(rec) == record_hash(stamped)
    assert record_hash("not a dict") is None
    assert source_attrs(None) == {"source.kind": "jsonl"}
    assert source_attrs(stamped)["source.byte_offset"] == 42


def test_parse_jsonl_records_keeps_every_dict_line_with_anchors():
    data = b'{"a": 1}\n\nnot json\n[1, 2]\n{"b": 2}\n'
    records = parse_jsonl_records(data)
    assert [r["a"] if "a" in r else r["b"] for r in records] == [1, 2]
    assert records[0][SOURCE_OFFSET_KEY] == 0
    assert records[1][SOURCE_OFFSET_KEY] == data.index(b'{"b": 2}')
    assert all(len(r[SOURCE_HASH_KEY]) == 16 for r in records)


# ── row cursors for rewritten sources ────────────────────


def test_rewritten_store_parsers_carry_a_row_cursor_when_the_record_names_one():
    from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import SOURCE_ROW_CURSOR_KEY

    # cursor_store: the CLI synthesizer stamps each message with its bubble id.
    # Messages in the parser's real wire shape (`tool-call` / `tool-result`
    # parts) so the tool span is actually built.
    msgs = [
        _cursor_store()[0],
        {
            "role": "user",
            "createdAt": "2026-09-01T00:00:01Z",
            "content": [{"type": "text", "text": "hi"}],
            SOURCE_ROW_CURSOR_KEY: "bubble-u1",
        },
        {
            "role": "assistant",
            "createdAt": "2026-09-01T00:00:02Z",
            "content": [{"type": "tool-call", "toolCallId": "t1", "toolName": "read", "args": {}}],
            SOURCE_ROW_CURSOR_KEY: "bubble-a1",
        },
        {
            "role": "tool",
            "createdAt": "2026-09-01T00:00:03Z",
            "content": [{"type": "tool-result", "toolCallId": "t1", "result": "ok"}],
            SOURCE_ROW_CURSOR_KEY: "bubble-t1",
        },
    ]
    spans = _anchored(cursor_store.parse_cursor_store(_jl(*msgs), "trace-1")[1])
    assert any(s.attributes.get("tool.name") for s in spans), [s.name for s in spans]
    turn = next(s for s in spans if s.name.startswith("Turn "))
    tool = next(s for s in spans if s.attributes.get("tool.name"))
    assert turn.attributes["source.row_cursor"] == "bubble-u1"
    # The requesting message anchors the call, not the result's message.
    assert tool.attributes["source.row_cursor"] == "bubble-a1"
    assert "source.byte_offset" not in turn.attributes
    assert turn.attributes["source.kind"] == "sqlite"

    # opencode: the message id is the key.
    recs = _opencode()
    recs[0]["id"] = "msg-1"
    recs[1]["id"] = "msg-2"
    spans = _anchored(opencode.parse_opencode(_jl(*recs), "trace-1")[1])
    turn = next(s for s in spans if s.name.startswith("Turn "))
    tool = next(s for s in spans if s.attributes.get("tool.name"))
    assert turn.attributes["source.row_cursor"] == "msg-1"
    assert tool.attributes["source.row_cursor"] == "msg-2"


def test_cursor_store_reads_the_bubble_id_the_synthesizer_already_writes():
    # The materializer writes each bubble's id as the message `id`, so the
    # cursor is available with no new stamp and no hash churn.
    msgs = _cursor_store()
    msgs[1]["id"] = "bubble-u1"
    before = cursor_store._event_hash({k: v for k, v in msgs[1].items()})
    spans = _anchored(cursor_store.parse_cursor_store(_jl(*msgs), "trace-1")[1])
    turn = next(s for s in spans if s.name.startswith("Turn "))
    assert turn.attributes["source.row_cursor"] == "bubble-u1"
    assert turn.attributes["source.content_hash"] == before


def test_older_uploads_without_a_row_cursor_are_unchanged():
    for name in ("cursor_store", "opencode"):
        for span in _anchored(_parse(name)):
            assert "source.row_cursor" not in span.attributes, (name, span.name)


def test_the_row_cursor_stamp_never_changes_the_content_hash():
    from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import SOURCE_ROW_CURSOR_KEY

    msg = {"role": "user", "content": "hi"}
    assert cursor_store._event_hash(msg) == cursor_store._event_hash(
        {**msg, SOURCE_ROW_CURSOR_KEY: "bubble-1"}
    )
    rec = {"a": 1}
    assert record_hash(rec) == record_hash({**rec, SOURCE_ROW_CURSOR_KEY: "x"})
    assert (
        source_attrs({"a": 1, SOURCE_ROW_CURSOR_KEY: "r"}, kind="sqlite")["source.row_cursor"]
        == "r"
    )
    assert source_attrs(None, row_cursor="r") == {"source.kind": "jsonl", "source.row_cursor": "r"}
    assert "source.row_cursor" not in source_attrs({"a": 1, SOURCE_ROW_CURSOR_KEY: ""})
