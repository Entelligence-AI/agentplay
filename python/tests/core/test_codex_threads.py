"""Codex Desktop subagents, compactions and interrupts, read from synthetic rollouts.

Codex Desktop writes each subagent as its own rollout. `session_meta` names
who started it (`thread_source`), its parent (`parent_thread_id`), and, for a
forked subagent, where its own lines start after the copied parent history
(`subagent_history_start_ordinal`). A subagent's input is the parent's
`agent_message`, never a user message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agentplay import builder, discover
from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex

PARENT = "01a0a028-0000-7000-8000-0000000000b1"
CHILD = "01a0a028-0000-7000-8000-0000000000b2"
GUARDIAN = "01a0a028-0000-7000-8000-0000000000b3"
WINDOW = "01a0a028-0000-7000-8000-0000000000b4"


def _line(kind, payload, second=0, minute=0):
    return {"type": kind, "timestamp": f"2026-09-08T10:{minute:02}:{second:02}Z", "payload": payload}


def _meta(thread_id, source="user", parent=None, start=None, path=None):
    payload = {
        "id": thread_id,
        "originator": "codex_cli_rs",
        "cwd": "/repo",
        "thread_source": source,
        "parent_thread_id": parent,
    }
    if start is not None:
        payload["subagent_history_start_ordinal"] = start
    if path:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "agent_path": path}}}
    return _line("session_meta", payload)


def _user(text, second=1):
    return _line("response_item", {"role": "user", "content": [{"type": "input_text", "text": text}]}, second)


def _agent_message(second=1):
    return _line(
        "response_item", {"type": "agent_message", "content": [{"type": "input_text", "text": "TASK-SECRET"}]}, second
    )


def _call(call_id, name="exec_command", second=2, arguments=None, output="Process exited with code 0"):
    args = json.dumps(arguments or {"cmd": "ls"})
    return [
        _line("response_item", {"type": "function_call", "call_id": call_id, "name": name, "arguments": args}, second),
        _line("response_item", {"type": "function_call_output", "call_id": call_id, "output": output}, second),
    ]


def _usage(inp, cached, out, total_before, second):
    last = dict(input_tokens=inp, cached_input_tokens=cached, output_tokens=out, total_tokens=inp + out)
    total = total_before + inp + out
    cum = dict(input_tokens=total - out, cached_input_tokens=0, output_tokens=out, total_tokens=total)
    return _line(
        "event_msg", {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": cum}}, second
    )


def _bytes(records):
    return b"\n".join(json.dumps(r).encode() for r in records) + b"\n"


def _parse(records):
    return parse_codex(_bytes(records), "local:codex:t")[1]


def _turns(spans):
    return [s for s in spans if s.name.startswith("Turn ")]


def _tools(spans):
    turns = {s.span_id for s in _turns(spans)}
    return [s for s in spans if s.parent_id in turns]


# ------------------------------------------------------------------ bug A
def test_a_subagent_driven_by_agent_message_keeps_its_tool_calls():
    spans = _parse(
        [
            _meta(CHILD, "subagent", PARENT),
            _user("<environment_context>cwd</environment_context>"),
            _agent_message(),
            *_call("c1"),
            *_call("c2", second=3),
            _agent_message(second=4),
            *_call("c3", second=5),
        ]
    )
    assert len(_turns(spans)) == 1
    assert len(_tools(spans)) == 3
    # The parent's instructions are not a user prompt and never reach a span.
    assert _turns(spans)[0].attributes["input.value"] == ""
    assert "TASK-SECRET" not in json.dumps([s.attributes for s in spans], default=str)


def test_agent_message_inside_an_open_turn_does_not_split_it():
    spans = _parse([_meta(PARENT), _user("Fix it"), *_call("c1"), _agent_message(3), *_call("c2", second=4)])
    assert len(_turns(spans)) == 1
    assert len(_tools(spans)) == 2


# ------------------------------------------------------------------ bug B
def test_a_prompt_written_as_a_markdown_heading_opens_a_turn():
    spans = _parse(
        [
            _meta(PARENT),
            _user("<environment_context>cwd</environment_context>"),
            _user("# Plan\nrefactor the parser", second=1),
            *_call("c1"),
        ]
    )
    assert [t.attributes["input.value"] for t in _turns(spans)] == ["# Plan\nrefactor the parser"]
    assert len(_tools(spans)) == 1


def test_the_injected_agents_md_block_is_still_not_a_prompt():
    spans = _parse(
        [
            _meta(PARENT),
            _user("# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>x</INSTRUCTIONS>"),
            _user("Fix it", second=2),
            *_call("c1", second=3),
        ]
    )
    assert [t.attributes["input.value"] for t in _turns(spans)] == ["Fix it"]


# ------------------------------------------------------------------ bug C (parser)
def _forked_child():
    history = [
        _meta(PARENT),  # the parent's own session_meta, copied
        _user("PARENT-PROMPT"),
        *_call("p1"),
        _usage(1000, 0, 10, 0, 3),
        _line("response_item", {"role": "developer", "content": [{"type": "input_text", "text": "You are an agent"}]}),
    ]
    head = _meta(CHILD, "subagent", PARENT, start=1 + len(history), path="/root/fix_parser")
    own = [_agent_message(10), *_call("c1", second=11), _usage(2000, 500, 20, 1010, 12)]
    return [head, *history, *own]


def test_a_forked_subagent_drops_the_copied_parent_history():
    spans = _parse(_forked_child())
    assert [t.attributes["input.value"] for t in _turns(spans)] == [""]
    assert [s.attributes["tool.name"] for s in _tools(spans)] == ["Bash"]
    # The parent's 1,010 tokens stay billed on the parent only.
    (turn,) = _turns(spans)
    assert turn.attributes["llm.token_count.prompt"] == 2000
    root = next(s for s in spans if s.parent_id is None)
    assert root.attributes["harness.agent_session_id"] == CHILD
    assert root.attributes["harness.thread_source"] == "subagent"
    assert root.attributes["harness.parent_thread_id"] == PARENT
    assert root.attributes["harness.agent_path"] == "/root/fix_parser"


# ------------------------------------------------------------------ #47
def test_compactions_and_interrupts_are_recorded_on_the_root():
    spans = _parse(
        [
            _meta(PARENT),
            _user("Fix it"),
            *_call("c1"),
            _usage(180_000, 0, 10, 0, 3),
            _line("compacted", {"message": "", "replacement_history": [], "latest_token_usage_record": None}, 4),
            _line("event_msg", {"type": "turn_aborted", "reason": "interrupted"}, 5),
            _line("event_msg", {"type": "turn_aborted", "reason": "replaced"}, 6),
        ]
    )
    root = next(s for s in spans if s.parent_id is None)
    assert root.attributes["harness.compactions"] == [{"timestamp": "2026-09-08T10:00:04Z", "pre_tokens": 180_000}]
    assert root.attributes["harness.interruptions"] == ["2026-09-08T10:00:05Z"]


def test_context_is_the_largest_request_not_the_turn_sum():
    records = [_meta(PARENT), _user("Fix it")]
    total = 0
    for i in range(5):
        records.append(_usage(200_000, 150_000, 100, total, i + 2))
        total += 200_100
    records += [
        _line("compacted", {"message": "", "replacement_history": []}, 9),
        _line("event_msg", {"type": "turn_aborted", "reason": "interrupted"}, 10),
    ]
    spans = _parse(records)
    doc = builder.build_map_from_spans(spans, session_id="local:codex:t", agent_tool="codex")
    assert doc["totals"]["maxContext"] == 200_000
    assert doc["totals"]["requests"] == 5
    assert doc["totals"]["tokens"] == 5 * 200_100
    assert doc["totals"]["compactions"] == 1
    compaction = next(e for e in doc["events"] if e["kind"] == "compaction")
    assert compaction["preTokens"] == 200_000
    assert doc["totals"]["errorsByClass"] == {"interrupted": 1}
    assert doc["session"]["outcome"] == "interrupted"


def test_per_request_events_cost_the_same_as_the_turn():
    records = [_meta(PARENT), _line("turn_context", {"model": "gpt-5"}), _user("Fix it")]
    total = 0
    for i in range(3):
        records.append(_usage(50_000, 10_000, 500, total, i + 2))
        total += 50_500
    spans = _parse(records)
    per_request = builder.build_map_from_spans(spans, session_id="s", agent_tool="codex")
    per_turn = builder.build_map_from_spans(spans, session_id="s", agent_tool="kiro")
    assert per_request["totals"]["costUsd"] == per_turn["totals"]["costUsd"] > 0
    assert per_request["totals"]["tokens"] == per_turn["totals"]["tokens"]
    assert per_turn["totals"]["requests"] == 1


# ------------------------------------------------------------------ bug C (discovery + map)
def _write(path: Path, records, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_bytes(records))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _name(stamp, thread_id, window=""):
    return f"rollout-2026-09-08T{stamp}-{thread_id}{'_' + window if window else ''}.jsonl"


def _family(tmp_path):
    day = tmp_path / ".codex" / "sessions" / "2026" / "09" / "08"
    spawn = _call(
        "s1",
        name="spawn_agent",
        arguments={"task_name": "fix_parser", "message": "go"},
        output=json.dumps({"task_name": "/root/fix_parser"}),
    )
    _write(day / _name("10-00-00", PARENT), [_meta(PARENT), _user("Fix it"), *spawn, *_call("p2", second=3)], 1_000)
    _write(day / _name("12-00-00", PARENT, WINDOW), [_meta(PARENT), _user("Later"), *_call("w1")], 1_000)
    _write(day / _name("10-05-00", CHILD), _forked_child(), 2_000)
    # As Codex writes it: the guardian's own reviews (and their usage) sit
    # BEFORE its `subagent_history_start_ordinal`, which points past the end.
    guardian = [
        _user("Review the next action", second=5),
        *_call("g0", second=6),
        _usage(3000, 0, 30, 0, 7),
    ]
    guardian.insert(0, _meta(GUARDIAN, "guardian_review", PARENT, start=len(guardian) + 1))
    _write(day / _name("10-06-00", GUARDIAN), guardian, 1_000)
    return tmp_path / ".codex"


def _source(home, monkeypatch):
    monkeypatch.setenv("AGENTPLAY_CODEX_HOMES", str(home))
    source = discover.local_source(discover.Homes.default(), {"codex"})
    return source, {Path(c.path).name: c for c in discover.candidates(source)}


def test_children_link_to_the_window_they_ran_in(tmp_path, monkeypatch):
    source, found = _source(_family(tmp_path), monkeypatch)
    index = source._build_index()
    parent = found[_name("10-00-00", PARENT)]
    child = index[found[_name("10-05-00", CHILD)].session_id]
    guardian = index[found[_name("10-06-00", GUARDIAN)].session_id]
    assert child.parent_session_id == parent.session_id
    # A guardian review is not folded, so it stays a session and its cost stays counted.
    assert guardian.parent_session_id is None
    assert source.codex_subagents(parent.session_id) == [found[_name("10-05-00", CHILD)].session_id]
    assert source.codex_subagents(found[_name("12-00-00", PARENT, WINDOW)].session_id) == []
    # A subagent that grows makes its parent stale too.
    assert parent.mtime == 2_000


def test_the_parent_map_holds_the_subagent_lane(tmp_path, monkeypatch):
    source, found = _source(_family(tmp_path), monkeypatch)
    parent = found[_name("10-00-00", PARENT)]
    doc = discover.build_map(parent, source)
    lane = "sa-" + CHILD
    assert [a["id"] for a in doc["agents"]] == ["main", lane]
    assert doc["totals"]["subagents"] == 1
    assert doc["totals"]["calls"] == 3  # spawn_agent + p2 + the subagent's c1
    launch = next(e for e in doc["events"] if e.get("tool") == "spawn_agent")
    assert launch["child"] == lane
    assert doc["agents"][1]["launchSeq"] == launch["seq"]
    # Only the user's own prompt is a turn: the subagent's task message is not.
    assert [e["agent"] for e in doc["events"] if e["kind"] == "user"] == ["main"]
    assert doc["session"]["role"] is None

    child = discover.build_map(found[_name("10-05-00", CHILD)], source)
    assert (child["session"]["role"], child["session"]["parentSessionId"]) == ("subagent", parent.session_id)
    assert child["totals"]["calls"] == 1
    guardian = discover.build_map(found[_name("10-06-00", GUARDIAN)], source)
    assert guardian["session"]["role"] is None
    # Its own review is kept, usage included: the fork cut applies to subagents only.
    assert (guardian["totals"]["calls"], guardian["totals"]["tokens"]) == (1, 3030)


def test_a_child_whose_parent_is_gone_stays_a_session(tmp_path, monkeypatch):
    day = tmp_path / ".codex" / "sessions" / "2026" / "09" / "08"
    _write(day / _name("10-05-00", CHILD), _forked_child())
    source, found = _source(tmp_path / ".codex", monkeypatch)
    doc = discover.build_map(found[_name("10-05-00", CHILD)], source)
    assert doc["session"]["role"] is None
    assert doc["totals"]["calls"] == 1


def test_the_ide_extension_context_block_is_not_a_prompt():
    spans = _parse(
        [
            _meta(PARENT),
            _user("# Context from my IDE setup:\n\n## Active file: src/app.py\n\n## My request for Codex:\nFix it"),
            _line("event_msg", {"type": "user_message", "message": "Fix it"}, 1),
            *_call("c1"),
        ]
    )
    assert [t.attributes["input.value"] for t in _turns(spans)] == ["Fix it"]


def test_the_fork_cut_counts_physical_lines():
    records = _forked_child()
    start = records[0]["payload"]["subagent_history_start_ordinal"]
    # Two extra physical lines (blank + unreadable) sit inside the copied history.
    records[0]["payload"]["subagent_history_start_ordinal"] = start + 2
    data = _bytes(records[:2]) + b"\n{not json\n" + _bytes(records[2:])
    spans = parse_codex(data, "local:codex:t")[1]
    assert [t.attributes["input.value"] for t in _turns(spans)] == [""]
    assert len(_tools(spans)) == 1


def test_a_child_whose_parent_window_is_gone_is_not_folded_into_a_later_one(tmp_path, monkeypatch):
    day = tmp_path / ".codex" / "sessions" / "2026" / "09" / "08"
    # Only a later window of the parent thread is on disk; the one the child ran in is gone.
    _write(day / _name("12-00-00", PARENT, WINDOW), [_meta(PARENT), _user("Later"), *_call("w1")])
    _write(day / _name("10-05-00", CHILD), _forked_child())
    source, found = _source(tmp_path / ".codex", monkeypatch)
    window = found[_name("12-00-00", PARENT, WINDOW)]
    child = found[_name("10-05-00", CHILD)]
    assert source._build_index()[child.session_id].parent_session_id is None
    assert source.codex_subagents(window.session_id) == []
    assert discover.build_map(child, source)["session"]["role"] is None
    assert [a["id"] for a in discover.build_map(window, source)["agents"]] == ["main"]


def test_a_self_parented_or_cyclic_thread_is_never_folded(tmp_path, monkeypatch):
    day = tmp_path / ".codex" / "sessions" / "2026" / "09" / "08"
    loop = [_meta(CHILD, "subagent", CHILD), _agent_message(), *_call("c1")]
    _write(day / _name("10-05-00", CHILD), loop)
    a = [_meta(PARENT, "subagent", WINDOW), _agent_message(), *_call("a1")]
    b = [_meta(WINDOW, "subagent", PARENT), _agent_message(), *_call("b1")]
    _write(day / _name("10-06-00", PARENT), a)
    _write(day / _name("10-07-00", WINDOW), b)
    source, found = _source(tmp_path / ".codex", monkeypatch)
    for c in found.values():
        doc = discover.build_map(c, source)
        assert doc["session"]["role"] is None
        assert (doc["totals"]["calls"], len(doc["agents"])) == (1, 1)


def test_two_subagents_spawned_with_one_task_name_each_get_their_launch():
    def spawn(call_id, second):
        return _call(
            call_id,
            name="spawn_agent",
            second=second,
            arguments={"task_name": "fix"},
            output=json.dumps({"task_name": "/root/fix"}),
        )

    parent = _parse([_meta(PARENT), _user("Fix it"), *spawn("s1", 2), *spawn("s2", 3)])
    child = _parse([_meta(CHILD, "subagent", PARENT, path="/root/fix"), _agent_message(5), *_call("c1", second=6)])
    lanes = [{"id": f"sa-{i}", "parent": "main", "spans": child, "path": "/root/fix"} for i in (1, 2)]
    doc = builder.build_map_from_spans(parent, session_id="s", agent_tool="codex", subagents=lanes)
    launches = [e for e in doc["events"] if e.get("tool") == "spawn_agent"]
    assert [e["child"] for e in launches] == ["sa-1", "sa-2"]
    assert all("launchSeq" in a for a in doc["agents"][1:])


def test_a_subagent_of_a_subagent_gets_a_lane_under_main(tmp_path, monkeypatch):
    home = _family(tmp_path)
    day = home / "sessions" / "2026" / "09" / "08"
    grandchild = [_meta(GUARDIAN, "subagent", CHILD), _agent_message(), *_call("gc1")]
    _write(day / _name("10-06-00", GUARDIAN), grandchild, 3_000)
    source, found = _source(home, monkeypatch)
    parent = found[_name("10-00-00", PARENT)]
    doc = discover.build_map(parent, source)
    assert [(a["id"], a["parent"]) for a in doc["agents"]] == [
        ("main", None),
        ("sa-" + CHILD, "main"),
        ("sa-" + GUARDIAN, "main"),
    ]
    assert doc["totals"]["calls"] == 4
    assert parent.mtime == 3_000


def test_one_esc_is_one_interrupt_even_with_subagents_running():
    parent = _parse(
        [_meta(PARENT), _user("Fix it"), _line("event_msg", {"type": "turn_aborted", "reason": "interrupted"}, 9)]
    )
    child = _parse(
        [
            _meta(CHILD, "subagent", PARENT),
            _agent_message(),
            *_call("c1"),
            _line("event_msg", {"type": "turn_aborted", "reason": "interrupted"}, 9),
        ]
    )
    lanes = [{"id": "sa-1", "parent": "main", "spans": child, "path": ""}]
    doc = builder.build_map_from_spans(parent, session_id="s", agent_tool="codex", subagents=lanes)
    assert doc["totals"]["errorsByClass"] == {"interrupted": 1}


def test_a_spawn_output_cut_short_still_links_its_subagent():
    output = json.dumps({"task_name": "/root/fix", "note": "x" * 3000})
    parent = _parse([_meta(PARENT), _user("Fix it"), *_call("s1", name="spawn_agent", output=output)])
    child = _parse([_meta(CHILD, "subagent", PARENT, path="/root/fix"), _agent_message(5), *_call("c1", second=6)])
    lanes = [{"id": "sa-1", "parent": "main", "spans": child, "path": "/root/fix"}]
    doc = builder.build_map_from_spans(parent, session_id="s", agent_tool="codex", subagents=lanes)
    assert next(e for e in doc["events"] if e.get("tool") == "spawn_agent")["child"] == "sa-1"


def test_a_subagent_with_no_spans_still_yields_its_own_subagents_lanes(tmp_path, monkeypatch):
    home = _family(tmp_path)
    day = home / "sessions" / "2026" / "09" / "08"
    grandchild = [_meta(GUARDIAN, "subagent", CHILD), _agent_message(), *_call("gc1")]
    _write(day / _name("10-06-00", GUARDIAN), grandchild, 3_000)
    source, found = _source(home, monkeypatch)
    child = found[_name("10-05-00", CHILD)].session_id
    spans_of = source.get_spans_by_session
    # The middle subagent can't be read (no root span); its own subagent can.
    monkeypatch.setattr(source, "get_spans_by_session", lambda sid: [] if sid == child else spans_of(sid))
    doc = discover.build_map(found[_name("10-00-00", PARENT)], source)
    assert [a["id"] for a in doc["agents"]] == ["main", "sa-" + GUARDIAN]
