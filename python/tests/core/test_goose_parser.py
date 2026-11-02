"""Goose decoder, parser, detection and discovery.

Goose keeps sessions in SQLite (``<data-dir>/sessions/sessions.db``). These
tests build that db with the DDL Goose (v1.57) creates on a fresh install, fill
it with sessions shaped like Goose's own serialization (``content_json`` is the
message's content-block array; tool calls and results use Goose's
``{"status": "success", "value": ...}`` envelope), then decode it with
``materialize.find_goose_candidates`` and parse the result.

Every prompt, reply, path, file content, argument value and tool output is a
placeholder; the built map must contain none of them.

* ``s-edit``: one prompt; `shell` (ok), `write`, `edit`, and a `shell` that
  exits non-zero (`isError`), with two usage rows.
* ``s-legacy``: an older session: `developer__text_editor` view and
  str_replace, a result stored as a bare content list, a malformed call
  (`toolCall.status` error), a call with no result, a compaction summary the
  user never saw, and a second prompt.
* ``s-hidden`` / ``s-sub``: hidden and subagent sessions, which are skipped.
* ``s-empty``: no messages, so nothing is written.

The fixtures under ``fixtures/goose/`` are real Goose 1.57 sessions (Windows,
`openai` provider), decoded by ``find_goose_candidates`` and then redacted:
every prompt, reply, thought, path, argument value and tool output is a
placeholder, and provider ids are renumbered. What survives is the structure:
record shapes, timestamps, usage, model, metadata flags, tool names, argument
keys, shell program names and Goose's own `Command exited with code <n>` lines.

* ``list_and_create``: `load_skill`, a PowerShell command that `cmd` rejects
  (exit code 255), then two `dir` calls.
* ``edit_file``: `load_skill`, a failing `Get-Content`, `type`, then `edit`.
* ``failed_shell``: one `python` run that exits 3.

Each opens with a hidden `turnContext` message after the prompt.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentplay import builder, discover, materialize
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    AgentType,
    detect_agent,
    parse_goose,
)
from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

FIXTURE_ROOT = "/Users/dev/project"
FILE_1 = f"{FIXTURE_ROOT}/file-1.py"
FILE_2 = f"{FIXTURE_ROOT}/file-2.py"
T0 = 1789380000  # 2026-09-14T10:00:00Z

SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
INSERT INTO schema_version (version) VALUES (17);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    user_set_name BOOLEAN DEFAULT FALSE, session_type TEXT NOT NULL DEFAULT 'user',
    working_dir TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, extension_data TEXT DEFAULT '{}',
    total_tokens INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_write_tokens INTEGER, accumulated_total_tokens INTEGER, accumulated_input_tokens INTEGER,
    accumulated_output_tokens INTEGER, accumulated_cache_read_tokens INTEGER,
    accumulated_cache_write_tokens INTEGER, accumulated_cost REAL, schedule_id TEXT, recipe_json TEXT,
    user_recipe_values_json TEXT, provider_name TEXT, model_config_json TEXT,
    goose_mode TEXT NOT NULL DEFAULT 'auto', archived_at TIMESTAMP, project_id TEXT,
    parent_session_id TEXT, system_prompt_override TEXT, system_prompt_extras_json TEXT, container_id TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT,
    session_id TEXT NOT NULL REFERENCES sessions(id), role TEXT NOT NULL, content_json TEXT NOT NULL,
    created_timestamp INTEGER NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tokens INTEGER, metadata_json TEXT
);
CREATE TABLE usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_timestamp INTEGER NOT NULL, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    total_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost REAL,
    cost_source TEXT, is_compaction INTEGER DEFAULT 0
);
"""

VISIBLE = {"userVisible": True, "agentVisible": True}
MODEL = "claude-sonnet-5-5"


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "type": "toolRequest",
        "id": call_id,
        "toolCall": {"status": "success", "value": {"name": name, "arguments": arguments}},
    }


def _result(call_id: str, text: str, *, is_error: bool = False) -> dict:
    block = {"type": "text", "text": text, "annotations": {"priority": 0.0}}
    return {
        "type": "toolResponse",
        "id": call_id,
        "toolResult": {"status": "success", "value": {"content": [block], "isError": is_error}},
    }


def _model_config() -> str:
    return json.dumps({"model_name": MODEL, "context_limit": 200000})


# (session row, [(role, content, offset seconds, metadata)], [usage rows])
SESSIONS = {
    "s-edit": (
        {"session_type": "user", "created_at": "2026-09-14 10:00:00", "updated_at": "2026-09-14 10:00:09"},
        [
            ("user", [_text("PLACEHOLDER-PROMPT-1")], 0, VISIBLE),
            ("assistant", [_text("PLACEHOLDER-REPLY-1"), _call("c1", "shell", {"command": "ls ARG-1"})], 1, VISIBLE),
            ("user", [_result("c1", "PLACEHOLDER-OUT-1")], 2, VISIBLE),
            ("assistant", [_call("c2", "write", {"path": FILE_1, "content": "PLACEHOLDER-BODY-1\n"})], 3, VISIBLE),
            ("user", [_result("c2", "PLACEHOLDER-OUT-2")], 4, VISIBLE),
            (
                "assistant",
                [
                    {"type": "thinking", "thinking": "PLACEHOLDER-THOUGHT-1", "signature": "sig"},
                    _call("c3", "edit", {"path": FILE_1, "before": "PLACEHOLDER-OLD", "after": "PLACEHOLDER-NEW"}),
                ],
                5,
                VISIBLE,
            ),
            ("user", [_result("c3", "PLACEHOLDER-OUT-3")], 6, VISIBLE),
            ("assistant", [_call("c4", "shell", {"command": "python ARG-1"})], 7, VISIBLE),
            (
                "user",
                [_result("c4", "PLACEHOLDER-OUT-4\n\nCommand exited with code 2", is_error=True)],
                8,
                VISIBLE,
            ),
            ("assistant", [_text("PLACEHOLDER-REPLY-2")], 9, VISIBLE),
        ],
        [
            {"created_timestamp": T0 + 2, "model": MODEL, "input_tokens": 1000, "output_tokens": 50,
             "cache_read_tokens": 800, "cache_write_tokens": 100, "cost": 0.01},
            {"created_timestamp": T0 + 9, "model": MODEL, "input_tokens": 2000, "output_tokens": 70,
             "cache_read_tokens": 1500, "cache_write_tokens": 0, "cost": 0.02},
        ],
    ),
    "s-legacy": (
        {"session_type": "user", "created_at": T0 + 100, "updated_at": T0 + 120},
        [
            ("user", [_text("PLACEHOLDER-PROMPT-2")], 100, VISIBLE),
            (
                "assistant",
                [_call("c5", "developer__text_editor", {"command": "view", "path": FILE_2})],
                101,
                VISIBLE,
            ),
            (
                "user",
                [
                    {
                        "type": "toolResponse",
                        "id": "c5",
                        # Older Goose stored the content list itself as the value,
                        # once for the model and once for the UI.
                        "toolResult": {
                            "status": "success",
                            "value": [
                                {"type": "text", "text": "PLACEHOLDER-OUT-5",
                                 "annotations": {"audience": ["assistant"]}},
                                {"type": "text", "text": "PLACEHOLDER-UI-5", "annotations": {"audience": ["user"]}},
                            ],
                        },
                    }
                ],
                102,
                VISIBLE,
            ),
            (
                "assistant",
                [
                    _call(
                        "c6",
                        "developer__text_editor",
                        {"command": "str_replace", "path": FILE_2, "old_str": "PLACEHOLDER-OLD-2",
                         "new_str": "PLACEHOLDER-NEW-2"},
                    ),
                    {"type": "toolRequest", "id": "c7", "toolCall": {"status": "error", "error": "PLACEHOLDER-ERR-7"}},
                ],
                103,
                VISIBLE,
            ),
            (
                "user",
                [
                    _result("c6", "PLACEHOLDER-OUT-6"),
                    {"type": "toolResponse", "id": "c7", "toolResult": {"status": "error", "error": "PLACEHOLDER-ERR-7"}},
                ],
                104,
                VISIBLE,
            ),
            ("user", [_text("PLACEHOLDER-SUMMARY")], 105, {"userVisible": False, "agentVisible": True}),
            ("user", [_text("PLACEHOLDER-PROMPT-3")], 110, VISIBLE),
            ("assistant", [_call("c8", "tree", {"path": FIXTURE_ROOT})], 111, VISIBLE),
        ],
        [],
    ),
    "s-hidden": (
        {"session_type": "hidden", "created_at": T0, "updated_at": T0},
        [("user", [_text("PLACEHOLDER-HIDDEN")], 0, VISIBLE)],
        [],
    ),
    "s-sub": (
        {"session_type": "sub_agent", "created_at": T0, "updated_at": T0, "parent_session_id": "s-edit"},
        [("user", [_text("PLACEHOLDER-SUB")], 0, VISIBLE)],
        [],
    ),
    "s-empty": ({"session_type": "user", "created_at": T0, "updated_at": T0}, [], []),
}


def _make_db(data_dir: Path, sessions: dict = SESSIONS, *, schema: str = SCHEMA) -> Path:
    db = data_dir / "sessions" / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(schema)
    for sid, (row, messages, usage) in sessions.items():
        con.execute(
            "INSERT INTO sessions (id, name, description, session_type, working_dir, created_at, updated_at,"
            " provider_name, model_config_json, parent_session_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, "PLACEHOLDER-NAME", "PLACEHOLDER-DESC", row["session_type"], FIXTURE_ROOT, row["created_at"],
             row["updated_at"], "anthropic", _model_config(), row.get("parent_session_id")),
        )
        for n, (role, content, offset, metadata) in enumerate(messages):
            con.execute(
                "INSERT INTO messages (message_id, session_id, role, content_json, created_timestamp, metadata_json)"
                " VALUES (?,?,?,?,?,?)",
                (f"{sid}-m{n}", sid, role, json.dumps(content), T0 + offset, json.dumps(metadata)),
            )
        for u in usage:
            con.execute(
                "INSERT INTO usage_ledger (session_id, created_timestamp, model, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, cost) VALUES (?,?,?,?,?,?,?,?)",
                (sid, u["created_timestamp"], u["model"], u["input_tokens"], u["output_tokens"],
                 u["cache_read_tokens"], u["cache_write_tokens"], u["cost"]),
            )
    con.commit()
    con.close()
    return db


def _materialize(tmp_path: Path, **kwargs) -> dict[str, materialize.Session]:
    data_dir = tmp_path / "goose-data"
    if not (data_dir / "sessions" / "sessions.db").exists():
        _make_db(data_dir, **kwargs)
    home = materialize.HomeSpec(kind=materialize.AgentKind.GOOSE, path=data_dir)
    return {s.abs_path.stem: s for s in materialize.find_goose_candidates(home, cache_root=tmp_path / "cache")}


@pytest.fixture
def decoded(tmp_path) -> dict[str, bytes]:
    return {sid: s.abs_path.read_bytes() for sid, s in _materialize(tmp_path).items()}


def _parse(data: bytes, sid: str):
    return parse_goose(data, f"local:goose:{sid}", project_name="project")[1]


def _turns(spans):
    return [s for s in spans if s.name.startswith("Turn ")]


def _tools(spans):
    return [s for s in spans if ":tool:" in s.span_id]


# --- decoding ---------------------------------------------------------------


def test_decoder_writes_user_sessions_and_skips_the_rest(tmp_path):
    sessions = _materialize(tmp_path)
    assert sorted(sessions) == ["s-edit", "s-legacy"]
    edit = sessions["s-edit"]
    assert edit.agent == materialize.AgentKind.GOOSE
    assert edit.cwd == Path(FIXTURE_ROOT)
    # Dated by the session's last activity, not the time the cache was written.
    assert edit.mtime == datetime(2026, 9, 14, 10, 0, 9, tzinfo=UTC)
    assert edit.abs_path.stat().st_mtime == pytest.approx(T0 + 9, abs=1)


def test_decoded_transcript_shape(decoded):
    lines = [json.loads(line) for line in decoded["s-edit"].splitlines()]
    header = lines[0]
    assert header["type"] == "goose_session"
    assert header["working_dir"] == FIXTURE_ROOT
    assert header["model"] == MODEL
    # The session's name and description are free text and are not copied.
    assert "PLACEHOLDER-NAME" not in decoded["s-edit"].decode()
    assert "PLACEHOLDER-DESC" not in decoded["s-edit"].decode()
    kinds = [line["type"] for line in lines[1:]]
    assert kinds.count("goose_message") == 10
    assert kinds.count("goose_usage") == 2
    # A usage row sorts after the messages of the same second.
    assert kinds[3] == "goose_usage"
    assert detect_agent(decoded["s-edit"]) == AgentType.GOOSE


def test_unchanged_session_is_not_rewritten(tmp_path):
    first = _materialize(tmp_path)["s-edit"].abs_path
    before = first.stat().st_mtime_ns
    second = _materialize(tmp_path)["s-edit"].abs_path
    assert second == first
    assert second.stat().st_mtime_ns == before


def test_rows_still_in_the_wal_are_read(tmp_path):
    """Goose's db is WAL-mode and live; uncheckpointed rows live in sessions.db-wal."""
    data_dir = tmp_path / "goose-data"
    db = _make_db(data_dir, {"s-edit": SESSIONS["s-edit"]})
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO messages (message_id, session_id, role, content_json, created_timestamp, metadata_json)"
            " VALUES ('late', 's-edit', 'user', ?, ?, ?)",
            (json.dumps([_text("PLACEHOLDER-LATE")]), T0 + 20, json.dumps(VISIBLE)),
        )
        writer.commit()
        assert Path(f"{db}-wal").stat().st_size > 0
        sessions = _materialize(tmp_path)
    finally:
        writer.close()
    _, spans = parse_goose(sessions["s-edit"].abs_path.read_bytes(), "t")
    assert len(_turns(spans)) == 2


def test_older_schema_without_optional_columns_or_usage(tmp_path):
    old_schema = """
    CREATE TABLE sessions (id TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', working_dir TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
        content_json TEXT NOT NULL, created_timestamp INTEGER NOT NULL);
    """
    data_dir = tmp_path / "goose-data"
    db = data_dir / "sessions" / "sessions.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript(old_schema)
    con.execute("INSERT INTO sessions (id, working_dir, updated_at) VALUES ('old', ?, '2026-09-14 10:00:05')",
                (FIXTURE_ROOT,))
    con.execute(
        "INSERT INTO messages (session_id, role, content_json, created_timestamp) VALUES ('old', 'user', ?, ?)",
        (json.dumps([_text("PLACEHOLDER-OLD-PROMPT")]), (T0 + 1) * 1000),  # milliseconds
    )
    con.commit()
    con.close()
    sessions = _materialize(tmp_path)
    assert list(sessions) == ["old"]
    _, spans = parse_goose(sessions["old"].abs_path.read_bytes(), "t")
    turn = _turns(spans)[0]
    assert turn.start_time == datetime(2026, 9, 14, 10, 0, 1, tzinfo=UTC)


def test_unreadable_db_warns_once_and_yields_nothing(tmp_path, caplog):
    db = tmp_path / "goose-data" / "sessions" / "sessions.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a sqlite database at all" * 100)
    materialize._warned.clear()
    with caplog.at_level(logging.WARNING, logger="agentplay.materialize"):
        assert _materialize(tmp_path) == {}
    assert any("goose: cannot read" in r.getMessage() for r in caplog.records)


def test_a_db_of_another_shape_is_skipped(tmp_path):
    db = tmp_path / "goose-data" / "sessions" / "sessions.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT)")
    con.commit()
    con.close()
    assert _materialize(tmp_path) == {}


def test_missing_db_yields_nothing(tmp_path):
    home = materialize.HomeSpec(kind=materialize.AgentKind.GOOSE, path=tmp_path)
    assert list(materialize.find_goose_candidates(home, cache_root=tmp_path / "cache")) == []


# --- parsing ----------------------------------------------------------------


def test_turns_and_tools(decoded):
    spans = _parse(decoded["s-edit"], "s-edit")
    turns = _turns(spans)
    assert len(turns) == 1
    assert turns[0].attributes["input.value"] == "PLACEHOLDER-PROMPT-1"
    assert turns[0].attributes["output.value"] == "PLACEHOLDER-REPLY-1\nPLACEHOLDER-REPLY-2"
    tools = _tools(spans)
    assert [t.name for t in tools] == ["Bash", "Write", "Edit", "Bash"]
    assert [t.status_code for t in tools] == ["OK", "OK", "OK", "ERROR"]
    assert all(t.parent_id == turns[0].span_id for t in tools)
    assert tools[3].attributes["output.value"].endswith("Command exited with code 2")


def test_file_arguments_are_normalized(decoded):
    tools = _tools(_parse(decoded["s-edit"], "s-edit"))
    write, edit = (json.loads(t.attributes["input.value"]) for t in tools[1:3])
    assert list(write) == ["file_path", "content"]
    assert write["file_path"] == FILE_1
    assert edit == {"file_path": FILE_1, "old_string": "PLACEHOLDER-OLD", "new_string": "PLACEHOLDER-NEW"}
    assert tools[1].attributes["code.edit_evidence"]["edits"][0]["path"] == FILE_1


def test_tokens_model_and_session_attributes(decoded):
    spans = _parse(decoded["s-edit"], "s-edit")
    root, turn = spans[0], _turns(spans)[0]
    assert root.name == "Session"
    assert root.attributes["harness.agent_session_id"] == "s-edit"
    assert root.attributes["llm.model_name"] == MODEL
    assert root.attributes["project.name"] == "project"
    attrs = turn.attributes
    assert attrs["cwd"] == FIXTURE_ROOT
    assert attrs["llm.model_name"] == MODEL
    assert attrs["llm.token_count.prompt"] == 3000
    assert attrs["llm.token_count.completion"] == 120
    assert attrs["llm.token_count.cache_read"] == 2300
    assert attrs["llm.token_count.cache_write"] == 100
    assert attrs["llm.token_count.input_fresh"] == 600
    assert [e["input_tokens"] for e in attrs["llm.cost_events"]] == [100, 500]
    assert turn.start_time == datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC)
    assert turn.end_time == datetime(2026, 9, 14, 10, 0, 9, tzinfo=UTC)


def test_legacy_text_editor_and_failures(decoded):
    spans = _parse(decoded["s-legacy"], "s-legacy")
    turns = _turns(spans)
    # The compaction summary the user never saw opens no turn.
    assert [t.attributes["input.value"] for t in turns] == ["PLACEHOLDER-PROMPT-2", "PLACEHOLDER-PROMPT-3"]
    tools = _tools(spans)
    assert [(t.name, t.status_code) for t in tools] == [
        ("Read", "OK"),
        ("Edit", "OK"),
        ("unknown", "ERROR"),
        ("ListDir", "UNSET"),
    ]
    # Only the model's copy of a result is kept.
    assert tools[0].attributes["output.value"] == "PLACEHOLDER-OUT-5"
    assert json.loads(tools[1].attributes["input.value"])["file_path"] == FILE_2
    assert tools[3].parent_id == turns[1].span_id
    # No usage rows: the model comes from the session's model config.
    assert turns[0].attributes["llm.model_name"] == MODEL
    assert turns[0].attributes["llm.token_count.prompt"] == 0


def test_text_editor_write_is_a_write():
    header = {"type": "goose_session", "id": "w", "working_dir": FIXTURE_ROOT}
    records = [
        header,
        {"type": "goose_message", "id": "1", "role": "user", "created": T0, "content": [_text("p")], "metadata": {}},
        {"type": "goose_message", "id": "2", "role": "assistant", "created": T0 + 1, "metadata": {},
         "content": [_call("w1", "developer__text_editor", {"command": "write", "path": FILE_1, "file_text": "x\n"})]},
        {"type": "goose_message", "id": "3", "role": "user", "created": T0 + 2, "metadata": {},
         "content": [_result("w1", "ok")]},
    ]
    data = b"".join(json.dumps(r).encode() + b"\n" for r in records)
    (tool,) = _tools(parse_goose(data, "t")[1])
    assert tool.name == "Write"
    assert tool.attributes["code.edit_evidence"]["edits"][0]["after"]


@pytest.mark.parametrize("data", [b"", b"\n\n", b"not json\n", b'{"type": "goose_session", "id": "x"}\n'])
def test_empty_or_malformed_input_yields_no_spans(data):
    assert parse_goose(data, "t")[1] == []


# --- the built map ----------------------------------------------------------


def _map(decoded: dict[str, bytes], sid: str) -> dict:
    spans = _parse(decoded[sid], sid)
    return builder.build_map_from_spans(spans, session_id=f"local:goose:{sid}", agent_tool="goose")


def _placeholders() -> set[str]:
    out: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list | tuple):
            for v in value:
                walk(v)
        elif isinstance(value, str) and ("PLACEHOLDER" in value or "ARG-" in value):
            out.add(value)

    walk(SESSIONS)
    return out


@pytest.mark.parametrize("sid", ["s-edit", "s-legacy"])
def test_built_map_never_leaks_fixture_text(decoded, sid):
    doc = _map(decoded, sid)
    raw = json.dumps(doc)
    for text in _placeholders():
        assert text not in raw, text
    # Outside a git repository a path collapses to a bucket label, so neither
    # the absolute path nor the file name may appear.
    for part in ("PLACEHOLDER", "ARG-1", FIXTURE_ROOT, "file-1", "file-2"):
        assert part not in raw, part
    assert doc["session"]["agentTool"] == "goose"


def test_built_map_classifies_reads_edits_shell_and_the_failures(decoded):
    def kinds(sid: str) -> list[tuple]:
        return [(e["kind"], e.get("verb"), e.get("err")) for e in _map(decoded, sid)["events"] if e.get("tool")]

    # The builder files a shell `ls` under search.
    assert kinds("s-edit") == [
        ("search", "ls", False),
        ("edit", None, False),
        ("edit", None, False),
        ("exec", "python", True),
    ]
    assert kinds("s-legacy") == [
        ("read", None, False),
        ("edit", None, False),
        ("tool", None, True),
        ("tool", None, False),
    ]
    doc = _map(decoded, "s-edit")
    assert doc["totals"]["errors"] == 1
    assert doc["session"]["models"] == [MODEL]


# --- discovery --------------------------------------------------------------


def test_local_source_indexes_decoded_sessions(tmp_path):
    sessions = _materialize(tmp_path)
    home = next(iter(sessions.values())).abs_path.parent
    (home / "stray.jsonl").write_text(json.dumps({"type": "user", "sessionId": "x"}) + "\n", encoding="utf-8")
    (home / "header-only.jsonl").write_text(
        json.dumps({"type": "goose_session", "id": "h", "working_dir": FIXTURE_ROOT}) + "\n", encoding="utf-8"
    )
    source = LocalSource(claude_code_homes=[], codex_homes=[], goose_homes=[home])
    index = source._build_index()
    assert sorted(index) == ["local:goose:s-edit", "local:goose:s-legacy"]
    assert source.discovery_stats()["goose"]["skipped"] == {"different_agent": 1, "empty_events": 1}
    assert source.get_cwd("local:goose:s-edit") == FIXTURE_ROOT
    assert source.get_project_name("local:goose:s-edit") == "project"
    spans = source.get_spans_by_session("local:goose:s-edit")
    assert [s.name for s in _tools(spans)] == ["Bash", "Write", "Edit", "Bash"]


def test_discovery_decodes_and_lists_goose(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTPLAY_HOME", str(tmp_path / "agentplay-home"))
    data_dir = tmp_path / "goose-data"
    _make_db(data_dir)
    monkeypatch.setenv("AGENTPLAY_GOOSE_HOMES", str(data_dir))
    homes = discover.Homes.default()
    assert homes.goose == [data_dir]
    assert homes.present()["goose"] == [data_dir]
    assert discover.materialize_all(homes, {"goose"}) == {"goose": 2}
    found = discover.candidates(discover.local_source(homes, {"goose"}))
    assert sorted(c.session_id for c in found) == ["local:goose:s-edit", "local:goose:s-legacy"]
    assert {c.agent for c in found} == {"goose"}


def test_default_goose_home(monkeypatch, tmp_path):
    assert "goose" in discover.AGENTS
    monkeypatch.delenv("AGENTPLAY_GOOSE_HOMES", raising=False)
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path))
    assert discover.Homes.default().goose == [tmp_path / "data"]
    monkeypatch.delenv("GOOSE_PATH_ROOT")
    monkeypatch.setattr(discover.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert discover.Homes.default().goose == [tmp_path / "Roaming" / "Block" / "goose" / "data"]
    monkeypatch.setattr(discover.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert discover.Homes.default().goose == [tmp_path / "share" / "goose"]


# --- real sessions -----------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "goose"
REAL = ("list_and_create", "edit_file", "failed_shell")


def _real(name: str) -> bytes:
    return (FIXTURES / f"{name}.jsonl").read_bytes()


def _real_map(name: str) -> dict:
    spans = parse_goose(_real(name), f"local:goose:{name}")[1]
    return builder.build_map_from_spans(spans, session_id=f"local:goose:{name}", agent_tool="goose")


@pytest.mark.parametrize("name", REAL)
def test_real_session_is_detected(name):
    assert detect_agent(_real(name)) == AgentType.GOOSE


@pytest.mark.parametrize(
    ("name", "tools"),
    [
        ("list_and_create", [("load_skill", "OK"), ("Bash", "ERROR"), ("Bash", "OK"), ("Bash", "OK")]),
        ("edit_file", [("load_skill", "OK"), ("Bash", "ERROR"), ("Bash", "OK"), ("Edit", "OK")]),
        ("failed_shell", [("Bash", "ERROR")]),
    ],
)
def test_real_session_turns_and_tools(name, tools):
    spans = parse_goose(_real(name), "t")[1]
    # The hidden `turnContext` message after the prompt opens no turn.
    assert len(_turns(spans)) == 1
    assert [(t.name, t.status_code) for t in _tools(spans)] == tools


def test_real_session_tokens():
    turn = _turns(parse_goose(_real("list_and_create"), "t")[1])[0]
    attrs = turn.attributes
    # Goose's `input_tokens` already counts cache reads and writes.
    assert attrs["llm.token_count.prompt"] == 32513
    assert attrs["llm.token_count.completion"] == 282
    assert attrs["llm.token_count.cache_read"] == 25492
    assert attrs["llm.token_count.cache_write"] == 7006
    assert attrs["llm.token_count.input_fresh"] == 15
    assert attrs["llm.model_name"] == "gpt-6-luna"
    assert len(attrs["llm.cost_events"]) == 5


@pytest.mark.parametrize("name", REAL)
def test_real_map_never_leaks_fixture_text(name):
    raw = json.dumps(_real_map(name))
    for part in ("PLACEHOLDER", "ARG-1", "ARG-2", "file-1", "C:\\Users\\dev"):
        assert part not in raw, part


def test_real_maps_classify_windows_shell_calls_and_the_edit():
    def kinds(name: str) -> list[tuple]:
        return [(e["kind"], e.get("verb"), e.get("err")) for e in _real_map(name)["events"] if e.get("tool")]

    assert kinds("list_and_create") == [
        ("tool", None, False),
        ("exec", "Get-ChildItem", True),
        ("exec", "dir", False),
        ("exec", "dir", False),
    ]
    assert kinds("edit_file") == [
        ("tool", None, False),
        ("exec", "Get-Content", True),
        ("exec", "type", False),
        ("edit", None, False),
    ]
    assert kinds("failed_shell") == [("exec", "python", True)]
    assert all(_real_map(name)["totals"]["errors"] == 1 for name in REAL)


def test_delegate_is_a_subagent_and_its_instructions_stay_out_of_the_map():
    records = [
        {"type": "goose_session", "id": "d", "working_dir": FIXTURE_ROOT, "model": MODEL},
        {"type": "goose_message", "id": "1", "role": "user", "created": T0, "metadata": {},
         "content": [_text("PLACEHOLDER-PROMPT")]},
        {"type": "goose_message", "id": "2", "role": "assistant", "created": T0 + 1, "metadata": {},
         "content": [_call("d1", "delegate", {"instructions": "PLACEHOLDER-TASK", "working_dir": FIXTURE_ROOT})]},
        {"type": "goose_message", "id": "3", "role": "user", "created": T0 + 5, "metadata": {},
         "content": [_result("d1", "PLACEHOLDER-FINDINGS")]},
    ]
    data = b"".join(json.dumps(r).encode() + b"\n" for r in records)
    spans = parse_goose(data, "local:goose:d")[1]
    assert [t.name for t in _tools(spans)] == ["Task"]
    doc = builder.build_map_from_spans(spans, session_id="local:goose:d", agent_tool="goose")
    raw = json.dumps(doc)
    for part in ("PLACEHOLDER", FIXTURE_ROOT):
        assert part not in raw, part
    assert [e["kind"] for e in doc["events"] if e.get("tool")] == ["subagent"]
