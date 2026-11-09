"""Regression tests for discovery, outcome classification and export edge cases."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3

from agentplay import builder as sm
from agentplay import cli, materialize
from agentplay.materialize import AgentKind, HomeSpec
from agentplay.store import Store


def test_classify_outcome_uses_seq_on_a_filtered_event_list():
    # A subagent's events are a filtered slice of the session, so their `seq`
    # values are global positions, not indexes into the slice.
    events = [
        {"kind": "verify", "seq": 10},
        {"kind": "edit", "seq": 11},
    ]
    assert sm.classify_outcome(events, "done") != "verified"


def test_classify_outcome_still_verifies_when_no_edit_follows():
    events = [
        {"kind": "edit", "seq": 10},
        {"kind": "verify", "seq": 11},
    ]
    assert sm.classify_outcome(events, "done") == "verified"


def test_opencode_session_with_out_of_range_time_does_not_abort_discovery(tmp_path, monkeypatch):
    home = tmp_path / "opencode"
    home.mkdir()
    con = sqlite3.connect(home / "opencode.db")
    con.execute("CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, time_updated INTEGER)")
    con.executemany(
        "INSERT INTO session VALUES (?, ?, ?, ?)",
        [("bad", "p", "/w", 10**18), ("good", "p", "/w", 1_700_000_000_000)],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(materialize, "_opencode_synthesize", lambda _con, _sid: b"x\n")

    spec = HomeSpec(AgentKind.OPENCODE, home)
    sessions = list(materialize.find_opencode_candidates(spec, cache_root=tmp_path / "c"))

    assert sorted(s.rel_path.name for s in sessions) == ["bad.jsonl", "good.jsonl"]


def test_opencode_sessions_still_in_the_wal_are_read(tmp_path, monkeypatch):
    # opencode keeps opencode.db in WAL mode; while it runs, new commits live only
    # in opencode.db-wal until a checkpoint.
    home = tmp_path / "opencode"
    home.mkdir()
    db = home / "opencode.db"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, time_updated INTEGER)")
        writer.execute("INSERT INTO session VALUES ('old', 'p', '/w', 1700000000000)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO session VALUES ('new', 'p', '/w', 1700000001000)")
        writer.commit()
        assert (home / "opencode.db-wal").stat().st_size > 0
        monkeypatch.setattr(materialize, "_opencode_synthesize", lambda _con, _sid: b"x\n")

        spec = HomeSpec(AgentKind.OPENCODE, home)
        sessions = list(materialize.find_opencode_candidates(spec, cache_root=tmp_path / "c"))
    finally:
        writer.close()

    assert sorted(s.rel_path.name for s in sessions) == ["new.jsonl", "old.jsonl"]


def test_kiro_cache_survives_a_failed_rewrite(tmp_path, monkeypatch):
    home = tmp_path / "kiro"
    session_dir = home / "sessions" / "ws" / "s1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(json.dumps({"id": "s1"}))
    messages = session_dir / "messages.jsonl"
    messages.write_text("{}\n")
    cache = tmp_path / "c"
    cached = cache / "ws" / "s1.jsonl"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"OLD\n")
    os.utime(cached, (1_000_000, 1_000_000))  # older than messages.jsonl: forces a rewrite

    monkeypatch.setattr(materialize, "_kiro_synthesize", lambda *_a: b"NEW CONTENT\n")
    real_write_bytes = pathlib.Path.write_bytes

    def torn_write(self, data):
        if self.parent == cached.parent:
            real_write_bytes(self, data[:3])
            raise OSError("disk full")
        return real_write_bytes(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", torn_write)
    list(materialize.find_kiro_candidates(HomeSpec(AgentKind.KIRO, home), cache_root=cache))

    assert cached.read_bytes() == b"OLD\n"
    assert [p.name for p in cached.parent.iterdir()] == ["s1.jsonl"]


def test_export_with_an_unnamed_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTPLAY_HOME", str(tmp_path / "home"))
    st = Store()
    doc = {
        "schema": sm.SCHEMA,
        "session": {"id": "sess-1", "pathMode": "relative"},
        "repos": [{"id": "r0", "name": None, "remote": None}, {"id": "r1", "name": "app", "remote": None}],
        "files": [],
    }
    st.put(doc, {"sessionId": "sess-1"})
    st.save_index()

    out = tmp_path / "out.json"
    assert cli.cmd_export(argparse.Namespace(session="sess-1", output=str(out))) == 0
    assert out.is_file()
    assert "repo names (app)" in capsys.readouterr().out
