
"""The companion snapshot: what it derives, and what it must never carry."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import socket
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agentplay import builder, cli, companion, discover, pipeline
from agentplay.store import INDEX_VERSION, Store

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "agentplay-companion-v1.schema.json"
FIXTURES = Path(__file__).resolve().parents[2] / "apps" / "macos" / "AgentPlayCompanion" / "Fixtures"


# ---------------------------------------------------------------- helpers
def _doc(sid: str, events: list[dict], *, agent: str = "claude-code", started: datetime = NOW - timedelta(hours=1),
         outcome: str = "completed", repos=("payments-api",), files=()) -> dict:
    evs = []
    for i, e in enumerate(events):
        evs.append({"agent": "main", "err": False, **e, "seq": i})
    return {
        "schema": "agentplay/v1",
        "session": {"id": sid, "agentTool": agent, "startedAt": companion.iso(started),
                    "durationMs": evs[-1]["t"] if evs else 0, "models": ["claude-sonnet-5"], "pathMode": "relative",
                    "outcome": outcome, "role": "root", "parentSessionId": None, "truncated": False},
        "totals": {"calls": sum(1 for e in evs if e["kind"] not in ("llm", "user", "error", "delay", "compaction")),
                   "requests": sum(1 for e in evs if e["kind"] == "llm"), "subagents": 0, "errors": sum(1 for e in evs if e.get("err")), "filesTouched": 0,
                   "costUsd": 0, "tokens": 0, "userTurns": 1, "retries": 0, "compactions": 0},
        "repos": [{"id": f"r{i}", "name": n, "remote": f"acme/{n}-SECRET-REMOTE"} for i, n in enumerate(repos)],
        "files": [{"id": i, "repo": "r0", "path": p} for i, p in enumerate(files)],
        "agents": [{"id": "main", "kind": "main"}],
        "prs": [{"provider": "github", "repo": "acme/x", "number": 1, "url": "https://github.com/acme/SECRET-PR/pull/1",
                 "seq": 0, "state": "open"}],
        "events": evs,
    }


def _put(store: Store, doc: dict, *, source: Path | None = None, mtime: float | None = None, title="SECRET-TITLE"):
    s = builder.summary(doc)
    started = datetime.fromisoformat(doc["session"]["startedAt"].replace("Z", "+00:00"))
    ended = started + timedelta(milliseconds=doc["session"]["durationMs"])
    row = {**s, "sessionId": doc["session"]["id"], "sessionDate": doc["session"]["startedAt"][:10],
           "endedAt": companion.iso(ended), "sessionTitle": title, "taskTitles": ["SECRET-TASK"],
           "contentHash": builder.content_hash(doc), "sourcePath": str(source) if source else None,
           "sourceMtime": mtime}
    store.put(doc, row)
    store.save_index()


def _exec(t: int, verb: str, err: bool = False, cls: str | None = "exit_code", agent: str = "main", kind="verify"):
    e = {"t": t, "kind": kind, "cat": "test", "tool": "Bash", "verb": verb, "err": err, "agent": agent}
    if err:
        e["errClass"] = cls
    return e


def _edit(t: int):
    return {"t": t, "kind": "edit", "cat": "file", "tool": "Edit", "targets": [{"file": 0, "touch": "edit"}]}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTPLAY_HOME", str(tmp_path / "ap"))
    monkeypatch.setattr(companion, "process_evidence", lambda paths: {})
    return Store(tmp_path / "ap")


@pytest.fixture()
def empty_homes(tmp_path):
    return discover.Homes(*([tmp_path / "none"] for _ in range(len(dataclasses.fields(discover.Homes)))))


def _snap(store, homes, **kw):
    return companion.snapshot(store, now=kw.pop("now", NOW), homes=homes, **kw)


def _walk_points(obj):
    if isinstance(obj, dict):
        if {"value", "kind", "source", "observedAt", "window", "confidence", "explanation"} <= obj.keys():
            yield obj
        for v in obj.values():
            yield from _walk_points(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_points(v)


# ---------------------------------------------------------------- shape
def test_empty_store(store, empty_homes):
    snap = _snap(store, empty_homes)
    assert snap["schema"] == "agentplay.companion/v1" and snap["schemaVersion"] == 1
    assert snap["store"]["available"] is False and snap["runs"] == [] and snap["learn"] is None
    assert {s["status"] for s in snap["sources"]} == {"not_found"}


def test_corrupt_index_is_reported_not_hidden(store, empty_homes):
    store.index_path.parent.mkdir(parents=True)
    store.index_path.write_text("{not json")
    snap = _snap(store, empty_homes)
    assert snap["store"]["indexError"]
    assert all(s["status"] == "error" and s["remedy"] for s in snap["sources"])


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs admin rights on Windows")
def test_planted_symlink_in_maps_is_unreadable_not_followed(store, empty_homes, tmp_path):
    doc = _doc("local:claude-code:a", [_exec(0, "pytest")])
    _put(store, doc)
    key = store.index["local:claude-code:a"]["key"]
    target = store.maps_dir / f"{key}.json"
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(doc))
    target.unlink()
    target.symlink_to(outside)
    snap = _snap(Store(store.base), empty_homes)
    assert snap["runs"] == [] and snap["unreadable"][0]["sessionId"] == "local:claude-code:a"


def test_every_datapoint_is_honest(store, empty_homes):
    _put(store, _doc("local:claude-code:a", [_exec(0, "pytest", err=True)]))
    snap = _snap(store, empty_homes)
    points = list(_walk_points(snap))
    assert points
    for p in points:
        assert p["kind"] in ("recorded", "derived", "estimated", "provider_reported", "unavailable")
        assert p["source"] in ("agentplay_map", "local_index", "approved_provider_cli", "official_provider_api", "none")
        assert p["confidence"] in ("high", "medium", "low")
        assert p["explanation"]
        if p["kind"] == "unavailable":
            assert p["value"] is None and p["source"] == "none"


def test_snapshot_matches_json_schema(store, empty_homes):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    _put(store, _doc("local:claude-code:a", [_exec(0, "pytest", err=True)] * 3 + [_edit(10)],
                     files=["src/cart.ts"]))
    jsonschema.validate(_snap(store, empty_homes), schema)


def test_swift_fixtures_match_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    fixtures = sorted(FIXTURES.glob("*.json"))
    assert len(fixtures) >= 5
    for f in fixtures:
        jsonschema.validate(json.loads(f.read_text()), schema)


# ---------------------------------------------------------------- privacy
@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for var in ("AGENTPLAY_CLAUDE_HOMES", "AGENTPLAY_CODEX_HOMES"):
        monkeypatch.delenv(var, raising=False)
    return home


def _line(**d):
    return json.dumps({"sessionId": "11111111-2222-3333-4444-555555555555", "uuid": os.urandom(8).hex(), **d}) + "\n"


@pytest.mark.skipif(sys.platform == "win32", reason="the companion is macOS-only")
def test_snapshot_never_leaks_prompt_args_paths_or_remotes(fake_home, tmp_path, monkeypatch):
    """End to end from a real-shaped transcript: build, then snapshot, then grep for every secret."""
    repo = tmp_path / "work" / "LEAKDIR" / "backend"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:acme/SECRET-REMOTE.git\n')
    cwd = str(repo)
    ts = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    lines = [_line(type="user", timestamp=f"{ts}.000Z", cwd=cwd,
                   message={"role": "user", "content": "please fix SECRET-PROMPT-TEXT now"})]
    for i in range(4):
        lines.append(_line(type="assistant", timestamp=f"{ts}.{i + 1}00Z", cwd=cwd, message={
            "id": f"m{i}", "model": "claude-sonnet-5",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 5},
            "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                         "input": {"command": f"TOKEN=SECRET-ENV pytest -k SECRET-ARG {cwd}/tests"}}]}))
        lines.append(_line(type="user", timestamp=f"{ts}.{i + 1}50Z", cwd=cwd, message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "SECRET-OUTPUT failed", "is_error": True}]}))
    proj = fake_home / ".claude" / "projects" / "-x-backend"
    proj.mkdir(parents=True)
    (proj / "11111111-2222-3333-4444-555555555555.jsonl").write_text("".join(lines))
    store = Store(tmp_path / "ap")
    monkeypatch.setattr(companion, "process_evidence", lambda paths: {})
    rep = pipeline.build_all(store=store, agents={"claude-code"})
    assert rep.built == 1
    assert "SECRET-PROMPT-TEXT" in json.dumps(store.index)  # the title is in the index ...
    snap = companion.snapshot(store, now=NOW)
    raw = json.dumps(snap)
    for secret in ("SECRET-PROMPT-TEXT", "SECRET-ARG", "SECRET-ENV", "SECRET-OUTPUT", "acme/SECRET-REMOTE", "github.com", "LEAKDIR",
                   "11111111-2222-3333-4444-555555555555.jsonl", str(tmp_path)):
        assert secret not in raw, secret  # ... and never in the snapshot
    # The repo label is the remote's name, a sanctioned identifier (docs/format.md); the owner and host are not.
    assert snap["runs"][0]["repo"] == "SECRET-REMOTE"
    assert snap["runs"][0]["recentEvents"][-1]["label"] == "pytest"
    assert snap["alerts"] and snap["alerts"][0]["command"] == "pytest"


def test_synthetic_index_fields_never_reach_snapshot(store, empty_homes):
    _put(store, _doc("local:claude-code:a", [_exec(0, "pytest", err=True)] * 3), source=Path("/Users/me/SECRET-USER-DIR/x.jsonl"))
    raw = json.dumps(_snap(store, empty_homes))
    for secret in ("SECRET-TITLE", "SECRET-TASK", "SECRET-REMOTE", "SECRET-PR", "SECRET-USER-DIR"):
        assert secret not in raw, secret


def test_snapshot_makes_no_network_calls(store, empty_homes, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    _put(store, _doc("local:claude-code:a", [_exec(0, "pytest", err=True)] * 3))
    assert _snap(store, empty_homes)["runs"]


# ---------------------------------------------------------------- repeated failure
def _alerts(store, homes, events, **kw):
    _put(store, _doc("local:claude-code:a", events, **kw))
    return [a for a in _snap(store, homes)["alerts"] if a["rule"] == "repeated_failure"]


def test_three_consecutive_failures_alert_once(store, empty_homes):
    a = _alerts(store, empty_homes, [_exec(i * 1000, "pnpm test", err=True) for i in range(5)])
    assert len(a) == 1
    assert a[0]["count"] == 5 and a[0]["triggerSeq"] == 2 and a[0]["firstSeq"] == 0 and a[0]["resolvedSeq"] is None
    assert a[0]["firstAt"] < a[0]["triggerAt"]


def test_edits_between_failures_do_not_reset_the_streak(store, empty_homes):
    evs = [_exec(0, "pnpm test", True), _edit(1), _exec(2, "pnpm test", True), _edit(3), _exec(4, "pnpm test", True)]
    assert len(_alerts(store, empty_homes, evs, files=["src/a.ts"])) == 1


def test_a_success_in_between_resets_the_streak(store, empty_homes):
    evs = [_exec(0, "pytest", True), _exec(1, "pytest", True), _exec(2, "pytest"), _exec(3, "pytest", True)]
    assert _alerts(store, empty_homes, evs) == []


def test_eventual_success_marks_the_alert_resolved(store, empty_homes):
    evs = [_exec(i, "pytest", True) for i in range(3)] + [_exec(9, "pytest")]
    a = _alerts(store, empty_homes, evs)
    assert a[0]["resolvedSeq"] == 3


def test_failures_outside_the_rolling_window_do_not_alert(store, empty_homes):
    gap = companion.REPEAT_WINDOW_MS
    evs = [_exec(0, "pytest", True), _exec(gap, "pytest", True), _exec(2 * gap + 1, "pytest", True)]
    assert _alerts(store, empty_homes, evs) == []


def test_different_error_classes_and_lanes_do_not_combine(store, empty_homes):
    evs = [_exec(0, "pytest", True, "exit_code"), _exec(1, "pytest", True, "timeout"),
           _exec(2, "pytest", True, "exit_code", agent="sa-1"), _exec(3, "pytest", True, "exit_code")]
    assert _alerts(store, empty_homes, evs) == []


def test_same_command_in_two_repos_gives_two_signatures(store, empty_homes):
    fails = [_exec(i, "pnpm test", True) for i in range(3)]
    _put(store, _doc("local:claude-code:a", fails, repos=("web",)))
    _put(store, _doc("local:codex:b", fails, agent="codex", repos=("api",)))
    a = [x for x in _snap(store, empty_homes)["alerts"] if x["rule"] == "repeated_failure"]
    assert len({x["signature"] for x in a}) == 2
    assert len({x["patternKey"] for x in a}) == 1  # one mute covers the pattern


def test_signature_is_stable_across_rescans(store, empty_homes):
    fails = [_exec(i, "pytest", True) for i in range(3)]
    _put(store, _doc("local:claude-code:a", fails))
    s1 = _snap(store, empty_homes)["alerts"][0]["signature"]
    _put(store, _doc("local:claude-code:a", [*fails, _exec(9, "pytest", True)]))
    assert _snap(store, empty_homes)["alerts"][0]["signature"] == s1


# ---------------------------------------------------------------- activity and pending calls
def test_mtime_alone_is_recent_not_active(store, empty_homes, tmp_path):
    src = tmp_path / "s.jsonl"
    src.write_text("x")
    os.utime(src, (NOW.timestamp() - 20, NOW.timestamp() - 20))
    _put(store, _doc("local:claude-code:a", [_exec(0, "ls", kind="exec")]), source=src, mtime=NOW.timestamp() - 20)
    run = _snap(store, empty_homes)["runs"][0]
    assert run["activity"]["value"] == "recent" and "not proof" in run["activity"]["explanation"]


def test_shared_database_agents_get_low_confidence(store, empty_homes, tmp_path):
    src = tmp_path / "cursor.db"
    src.write_text("x")
    os.utime(src, (NOW.timestamp() - 20, NOW.timestamp() - 20))
    _put(store, _doc("local:cursor:a", [_exec(0, "ls", kind="exec")], agent="cursor"), source=src)
    act = _snap(store, empty_homes)["runs"][0]["activity"]
    assert act["value"] == "recent" and act["confidence"] == "low"


def test_pending_call_on_an_active_run_is_not_a_failure(store, empty_homes, tmp_path, monkeypatch):
    src = tmp_path / "s.jsonl"
    src.write_text("x")
    os.utime(src, (NOW.timestamp(), NOW.timestamp()))
    evs = [_exec(0, "pytest", True), _exec(1, "pytest", True), _exec(2, "pytest", True, "no_result")]
    _put(store, _doc("local:claude-code:a", evs), source=src, mtime=NOW.timestamp())
    monkeypatch.setattr(companion, "process_evidence", lambda paths: {k: "a claude process is running in this project"
                                                                      for k in paths})
    snap = _snap(store, empty_homes)
    run = snap["runs"][0]
    assert run["activity"]["value"] == "active"
    assert run["lastEvent"]["pending"] and not run["lastEvent"]["err"]
    assert run["pendingTool"]["elapsed"]["value"] >= 0
    assert run["lastError"]["seq"] == 1
    assert [a for a in snap["alerts"] if a["rule"] == "repeated_failure"] == []


def test_unresolved_end_only_when_idle(store, empty_homes):
    _put(store, _doc("local:claude-code:a", [_exec(0, "pytest", True), _exec(1, "tsc", True)],
                     outcome="unresolved_errors"))
    assert any(a["rule"] == "unresolved_end" for a in _snap(store, empty_homes)["alerts"])


def test_runs_ordered_by_latest_activity(store, empty_homes):
    _put(store, _doc("local:claude-code:old-but-long", [_exec(0, "ls", kind="exec"), _exec(5 * 3600_000, "ls", kind="exec")],
                     started=NOW - timedelta(hours=6)))
    _put(store, _doc("local:claude-code:newer-start", [_exec(0, "ls", kind="exec")], started=NOW - timedelta(hours=2)))
    ids = [r["sessionId"] for r in _snap(store, empty_homes)["runs"]]
    assert ids == ["local:claude-code:old-but-long", "local:claude-code:newer-start"]


def test_process_evidence_without_lsof_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(companion.shutil, "which", lambda _: None)
    assert companion.process_evidence({"a": ("claude-code", str(tmp_path))}) == {}


def test_claude_project_dir_encoding():
    assert companion._claude_project_dir("/Users/me/Documents/projects/agent.play") == \
        "-Users-me-Documents-projects-agent-play"


# ---------------------------------------------------------------- context
LS = {"t": 0, "kind": "exec", "cat": "shell", "tool": "Bash", "verb": "ls"}


def test_context_is_tokens_without_a_percentage(store, empty_homes):
    evs = [LS, {"t": 0, "kind": "llm", "model": "m", "in": 10, "cw": 100, "cr": 5000, "out": 5}]
    _put(store, _doc("local:claude-code:a", evs))
    ctx = _snap(store, empty_homes)["runs"][0]["context"]
    assert ctx["kind"] == "recorded" and ctx["value"] == {"tokens": 5110, "limit": None, "percent": None}


def test_context_after_compaction_uses_post_tokens(store, empty_homes):
    evs = [LS, {"t": 0, "kind": "llm", "model": "m", "in": 1, "cw": 0, "cr": 180000, "out": 5},
           {"t": 1, "kind": "compaction", "trigger": "auto", "preTokens": 180000, "postTokens": 12000}]
    _put(store, _doc("local:claude-code:a", evs))
    ctx = _snap(store, empty_homes)["runs"][0]["context"]
    assert ctx["value"]["tokens"] == 12000 and "compaction" in ctx["explanation"]


def test_context_ignores_subagent_lanes(store, empty_homes):
    evs = [LS, {"t": 0, "kind": "llm", "model": "m", "in": 1, "cw": 0, "cr": 100, "out": 5},
           {"t": 1, "kind": "llm", "model": "m", "in": 1, "cw": 0, "cr": 99999, "out": 5, "agent": "sa-1"}]
    _put(store, _doc("local:claude-code:a", evs))
    assert _snap(store, empty_homes)["runs"][0]["context"]["value"]["tokens"] == 101


def test_no_tokens_is_unavailable_not_zero(store, empty_homes):
    evs = [LS, {"t": 0, "kind": "llm", "model": "m", "in": 0, "cw": 0, "cr": 0, "out": 0}]
    _put(store, _doc("local:cursor:a", evs, agent="cursor"))
    ctx = _snap(store, empty_homes)["runs"][0]["context"]
    assert ctx["kind"] == "unavailable" and ctx["value"] is None


# ---------------------------------------------------------------- codex limits
def _codex_file(tmp_path, name, ts, rate_limits, info=None):
    rec = {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": info,
                                                            "rate_limits": rate_limits}}
    p = tmp_path / name
    p.write_text('{"type":"session_meta","payload":{"instructions":"SECRET-INSTR"}}\nnot json\n' + json.dumps(rec) + "\n")
    return p


def _rl(plan="plus", used=32.0, resets=NOW + timedelta(hours=2), weekly=4.0):
    return {"limit_id": "codex", "plan_type": plan,
            "primary": {"used_percent": used, "window_minutes": 300, "resets_at": int(resets.timestamp())},
            "secondary": {"used_percent": weekly, "window_minutes": 10080,
                          "resets_at": int((NOW + timedelta(days=3)).timestamp())}}


def test_codex_limits_fresh(tmp_path):
    p = _codex_file(tmp_path, "a.jsonl", "2026-10-23T11:50:00Z", _rl())
    rows = companion.codex_limits([("s", str(p))], NOW)
    five = next(r for r in rows if r["window"] == "five_hour")
    assert five["used"]["kind"] == "provider_reported" and five["used"]["value"]["leftPercent"] == 68.0
    assert five["used"]["freshness"] == "fresh" and five["resetsAt"]
    assert {r["window"] for r in rows} == {"five_hour", "weekly"}
    assert "SECRET-INSTR" not in json.dumps(rows)


def test_codex_limits_stale_after_reset(tmp_path):
    p = _codex_file(tmp_path, "a.jsonl", "2026-10-23T05:00:00Z", _rl(resets=NOW - timedelta(minutes=5)))
    five = next(r for r in companion.codex_limits([("s", str(p))], NOW) if r["window"] == "five_hour")
    assert five["used"]["freshness"] == "stale" and five["used"]["confidence"] == "low"
    assert "unknown" in five["used"]["explanation"]


def test_codex_limits_conflicting_plans(tmp_path):
    a = _codex_file(tmp_path, "a.jsonl", "2026-10-23T11:50:00Z", _rl(plan="plus"))
    b = _codex_file(tmp_path, "b.jsonl", "2026-10-23T11:40:00Z", _rl(plan="pro", used=90))
    rows = companion.codex_limits([("a", str(a)), ("b", str(b))], NOW)
    five = next(r for r in rows if r["window"] == "five_hour")
    assert five["used"]["value"]["usedPercent"] == 32.0  # newest report wins
    assert five["used"]["confidence"] == "low" and "different accounts" in five["used"]["explanation"]
    assert five["conflictingPlans"] == ["plus", "pro"]


def test_codex_limits_without_windows_are_unavailable(tmp_path):
    p = _codex_file(tmp_path, "a.jsonl", "2026-10-23T11:50:00Z",
                    {"limit_id": "premium", "plan_type": "free", "primary": None, "secondary": None})
    rows = companion.codex_limits([("s", str(p))], NOW)
    assert rows[0]["used"]["kind"] == "unavailable"


def test_codex_limits_missing_file_and_garbage(tmp_path):
    assert companion.codex_limits([("s", str(tmp_path / "gone.jsonl"))], NOW) == []
    g = tmp_path / "g.jsonl"
    g.write_text('{"payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":"x"}}}}\n')
    assert companion.codex_limits([("s", str(g))], NOW)[0]["used"]["kind"] == "unavailable"


def test_codex_context_with_window(tmp_path):
    p = _codex_file(tmp_path, "a.jsonl", "2026-10-23T11:50:00Z", None,
                    info={"last_token_usage": {"input_tokens": 129200}, "model_context_window": 258400})
    ctx = companion.codex_context(str(p))
    assert ctx["kind"] == "provider_reported" and ctx["value"] == {"tokens": 129200, "limit": 258400, "percent": 50.0}


def test_claude_plan_usage_is_always_unavailable(store, empty_homes):
    _put(store, _doc("local:claude-code:a", [_exec(0, "ls", kind="exec")]))
    claude = next(r for r in _snap(store, empty_homes)["limits"] if r["provider"] == "claude-code")
    assert claude["used"]["kind"] == "unavailable" and claude["usageUrl"]


# ---------------------------------------------------------------- learn
def test_learn_card_needs_recurrence_across_runs(store, empty_homes):
    _put(store, _doc("local:claude-code:a", [_exec(i, "pnpm test", True) for i in range(9)]))
    _put(store, _doc("local:claude-code:b", [_exec(0, "ls", kind="exec")]))
    assert _snap(store, empty_homes)["learn"] is None  # nine in one run is one bad afternoon


def test_learn_card_counts_and_denominator(store, empty_homes):
    for i, sid in enumerate("abcd"):
        evs = [_exec(0, "pnpm test", True), _exec(1, "pnpm test", True, "no_result")]
        if i % 2:
            evs.append(_exec(2, "pnpm test"))
        _put(store, _doc(f"local:claude-code:{sid}", evs, started=NOW - timedelta(hours=i + 1)))
    _put(store, _doc("local:claude-code:old", [_exec(0, "pnpm test", True)] * 9, started=NOW - timedelta(days=30)))
    card = _snap(store, empty_homes)["learn"]
    assert card["title"] == "`pnpm test` failed 4 times in 4 runs."  # no_result and >14-day runs are not counted
    assert card["sessions"] == 4 and "Across 4 recent sessions" in card["denominator"]
    assert "2 of those 4" in card["detail"] and card["kind"] == "observed"
    assert len(card["evidence"]) == 4 and all("seq" in e for e in card["evidence"])


# ---------------------------------------------------------------- cli
def test_cli_prints_one_json_document(store, monkeypatch):
    _put(store, _doc("local:claude-code:a", [_exec(0, "pytest", True)] * 3))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["companion", "snapshot", "--json"])
    assert rc == 0
    snap = json.loads(buf.getvalue())
    assert snap["schema"] == companion.SCHEMA and snap["runs"][0]["sessionId"] == "local:claude-code:a"


def test_cli_companion_needs_a_subcommand():
    with pytest.raises(SystemExit):
        cli.main(["companion"])


def test_pending_call_on_a_lagging_map_is_neither_running_nor_failed(store, empty_homes, tmp_path, monkeypatch):
    """The map was built mid-call; the transcript moved on since. The call has most likely finished."""
    src = tmp_path / "s.jsonl"
    src.write_text("x")
    os.utime(src, (NOW.timestamp(), NOW.timestamp()))
    _put(store, _doc("local:claude-code:a", [_exec(0, "ls", kind="exec"), _exec(1, "python", True, "no_result",
                                                                               kind="exec")]),
         source=src, mtime=NOW.timestamp())
    store.index["local:claude-code:a"]["updatedAt"] = companion.iso(NOW - timedelta(minutes=18))
    store.save_index()
    monkeypatch.setattr(companion, "process_evidence", lambda paths: {k: "a claude process is running in this project"
                                                                      for k in paths})
    run = _snap(Store(store.base), empty_homes)["runs"][0]
    assert run["mapBehindSeconds"] == 18 * 60
    assert run["pendingTool"] is None
    assert run["lastEvent"]["pending"] is False and run["lastEvent"]["err"] is False
    assert run["lastError"] is None


# ---------------------------------------------------------------- usage
def _llm(t_s, model="claude-sonnet-5", cost=0.01, **tok):
    e = {"t": int(t_s * 1000), "kind": "llm", "model": model, "in": 10, "cw": 100, "cr": 1000, "out": 50, **tok}
    if cost is not None:
        e["cost"] = cost
    return e


def test_usage_periods_models_and_unpriced(store, empty_homes):
    from agentplay import usage

    _put(store, _doc("local:claude-code:a", [LS, _llm(0), _llm(60, cost=None, model="mystery-model")],
                     started=NOW - timedelta(minutes=30)))
    _put(store, _doc("local:codex:b", [LS, _llm(0, model="gpt-5.5", cost=0.5)], agent="codex",
                     started=NOW - timedelta(days=3)))
    r = usage.report(store, NOW)
    assert r["kind"] == "estimated"
    assert r["last7Days"]["requests"] == 3 and r["last7Days"]["unpricedRequests"] == 1
    assert r["last7Days"]["costUsd"] == 0.51
    assert r["last7Days"]["tokens"]["total"] == 3 * 1160
    mystery = next(m for m in r["last7Days"]["byModel"] if m["key"] == "mystery-model")
    assert mystery["unpricedRequests"] == 1 and mystery["costUsd"] == 0  # unpriced is flagged, the UI never says $0
    assert {a["key"] for a in r["last7Days"]["byAgent"]} == {"claude-code", "codex"}
    assert r["daily"][-1]["requests"] >= 2


def test_usage_cache_is_reused_and_invalidated(store, empty_homes):
    from agentplay import usage

    _put(store, _doc("local:claude-code:a", [LS, _llm(0)], started=NOW - timedelta(hours=1)))
    usage.report(store, NOW)
    cache = json.loads((store.base / "cache" / "companion-usage.json").read_text())
    assert "local:claude-code:a" in cache["sessions"]
    _put(store, _doc("local:claude-code:a", [LS, _llm(0), _llm(30)], started=NOW - timedelta(hours=1)))
    assert usage.report(Store(store.base), NOW)["today"]["requests"] == 2


def test_claude_block_follows_ccusage_rules(store, empty_homes):
    from agentplay import usage

    # Activity at 06:10 opens a window at 06:00 that ends 11:00; activity at 11:50 opens the next at 11:00.
    day = NOW.replace(hour=0, minute=0, second=0)
    _put(store, _doc("local:claude-code:a", [LS, _llm(0)], started=day + timedelta(hours=6, minutes=10)))
    _put(store, _doc("local:claude-code:b", [LS, _llm(0, cost=1.0)], started=day + timedelta(hours=11, minutes=50)))
    b = usage.report(store, day + timedelta(hours=12))["claudeBlock"]
    assert b["start"].endswith("T11:00:00Z") and b["end"].endswith("T16:00:00Z")
    assert b["costUsd"] == 1.0 and b["requests"] == 1
    assert usage.report(store, day + timedelta(hours=17))["claudeBlock"] is None  # window over, nothing since


def test_usage_never_carries_titles_or_paths(store, empty_homes):
    _put(store, _doc("local:claude-code:a", [LS, _llm(0)]), source=Path("/Users/me/SECRET-USER-DIR/x.jsonl"))
    raw = json.dumps(_snap(store, empty_homes)["usage"])
    for secret in ("SECRET-TITLE", "SECRET-TASK", "SECRET-USER-DIR", "SECRET-REMOTE"):
        assert secret not in raw


# ---------------------------------------------------------------- live Claude sessions
@pytest.mark.skipif(sys.platform == "win32", reason="the companion is macOS-only")
def test_live_claude_sessions_alive_only_and_minimal(tmp_path):
    home = tmp_path / ".claude"
    (home / "sessions").mkdir(parents=True)
    me = os.getpid()
    rec = {"pid": me, "sessionId": "abc", "status": "waiting", "cwd": "/Users/me/SECRET-USER-DIR/payments-api",
           "name": "SECRET-SESSION-NAME", "messagingSocketPath": "/tmp/SECRET-SOCK", "startedAt": 1_792_000_000_000,
           "statusUpdatedAt": 1_792_000_100_000}
    (home / "sessions" / f"{me}.json").write_text(json.dumps(rec))
    (home / "sessions" / "999999.json").write_text(json.dumps({**rec, "pid": 999999, "sessionId": "dead"}))
    (home / "sessions" / "bad.json").write_text("{nope")
    (home / "sessions" / "weird.json").write_text(json.dumps({**rec, "status": "dancing", "sessionId": "w"}))
    homes = discover.Homes(*([home] if i == 0 else [tmp_path / "none"] for i in range(len(dataclasses.fields(discover.Homes)))))
    live = companion.live_claude_sessions(homes, NOW)
    assert [r["sessionId"] for r in live] == ["local:claude-code:abc"]
    assert live[0]["status"] == "waiting" and live[0]["project"] == "payments-api"
    assert live[0]["pid"] == me
    assert live[0]["entrypoint"] is None  # absent in this record; "cli" or "sdk-ts" when Claude Code writes it
    raw = json.dumps(live)
    for secret in ("SECRET-USER-DIR", "SECRET-SESSION-NAME", "SECRET-SOCK"):
        assert secret not in raw


@pytest.mark.skipif(sys.platform == "win32", reason="the companion is macOS-only")
def test_live_status_marks_runs_and_sorts_first(store, tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    (home / "sessions").mkdir(parents=True)
    me = os.getpid()
    (home / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": "old", "status": "busy", "cwd": "/x/api"}))
    homes = discover.Homes(*([home] if i == 0 else [tmp_path / "none"] for i in range(len(dataclasses.fields(discover.Homes)))))
    _put(store, _doc("local:claude-code:old", [_exec(0, "ls", kind="exec")], started=NOW - timedelta(days=2)))
    _put(store, _doc("local:claude-code:new", [_exec(0, "ls", kind="exec")], started=NOW - timedelta(hours=1)))
    snap = companion.snapshot(store, now=NOW, homes=homes)
    assert snap["runs"][0]["sessionId"] == "local:claude-code:old"
    assert snap["runs"][0]["liveStatus"] == "busy" and snap["runs"][0]["activity"]["value"] == "active"
    assert snap["live"][0]["inStore"] is True


# ---------------------------------------------------------------- Claude statusline bridge
BRIDGE = Path(__file__).resolve().parents[2] / "apps" / "macos" / "AgentPlayCompanion" / "Resources" / "claude-statusline-bridge.sh"
STATUSLINE = {"session_id": "abc-123", "transcript_path": "/Users/me/SECRET-USER-DIR/t.jsonl",
              "model": {"display_name": "Opus"}, "workspace": {"current_dir": "/Users/me/SECRET-USER-DIR"},
              "context_window": {"used_percentage": 42.5, "context_window_size": 200000},
              "rate_limits": {"five_hour": {"used_percentage": 31, "resets_at": int((NOW + timedelta(hours=2)).timestamp())},
                              "seven_day": {"used_percentage": 64, "resets_at": int((NOW + timedelta(days=3)).timestamp())}}}


@pytest.mark.skipif(sys.platform == "win32", reason="the bridge is a macOS shell script")
def test_bridge_saves_statusline_and_chains_previous(tmp_path):
    import subprocess

    env = {"AGENTPLAY_HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run(["/bin/sh", str(BRIDGE)], input=json.dumps(STATUSLINE), capture_output=True, text=True, env=env)
    assert out.returncode == 0 and out.stdout == ""  # no previous statusline: prints nothing
    saved = json.loads((tmp_path / "live" / "claude" / "abc-123.json").read_text())
    assert saved["rate_limits"]["five_hour"]["used_percentage"] == 31
    (tmp_path / "live" / "claude-statusline.previous").write_text("cat | grep -o Opus")
    out = subprocess.run(["/bin/sh", str(BRIDGE)], input=json.dumps(STATUSLINE), capture_output=True, text=True, env=env)
    assert out.stdout.strip() == "Opus"  # the user's own statusline still runs, on the same input


def test_statusline_limits_and_context_reach_snapshot(store, empty_homes):
    d = store.base / "live" / "claude"
    d.mkdir(parents=True)
    f = d / "abc-123.json"
    f.write_text(json.dumps(STATUSLINE))
    os.utime(f, (NOW.timestamp() - 60, NOW.timestamp() - 60))
    _put(store, _doc("local:claude-code:abc-123", [LS, _llm(0)]))
    snap = _snap(store, empty_homes)
    five = next(r for r in snap["limits"] if r["provider"] == "claude-code" and r["window"] == "five_hour")
    assert five["used"]["value"] == {"usedPercent": 31, "leftPercent": 69} and five["used"]["kind"] == "provider_reported"
    assert any(r["provider"] == "claude-code" and r["window"] == "weekly" for r in snap["limits"])
    ctx = snap["runs"][0]["context"]
    assert ctx["value"] == {"tokens": 85000, "limit": 200000, "percent": 42.5}
    raw = json.dumps(snap)
    assert "SECRET-USER-DIR" not in raw and "Opus" not in raw


def test_statusline_window_past_reset_is_stale(store, empty_homes):
    d = store.base / "live" / "claude"
    d.mkdir(parents=True)
    old = {**STATUSLINE, "rate_limits": {"five_hour": {"used_percentage": 90, "resets_at": int((NOW - timedelta(minutes=5)).timestamp())}}}
    f = d / "abc-123.json"
    f.write_text(json.dumps(old))
    os.utime(f, (NOW.timestamp() - 7200, NOW.timestamp() - 7200))
    five = next(r for r in _snap(store, empty_homes)["limits"] if r["provider"] == "claude-code")
    assert five["used"]["freshness"] == "stale" and five["used"]["confidence"] == "low"


def _claude_line(ts, msg_id, req_id, out, model="claude-opus-5-5", cw=100, cr=1000, cwd="/Users/me/SECRET-DIR/payments-api"):
    return json.dumps({"type": "assistant", "timestamp": ts, "requestId": req_id, "cwd": cwd,
                       "message": {"id": msg_id, "model": model, "role": "assistant",
                                   "content": [{"type": "text", "text": "SECRET-REPLY"}],
                                   "usage": {"input_tokens": 10, "cache_creation_input_tokens": cw,
                                             "cache_read_input_tokens": cr, "output_tokens": out}}}) + "\n"


def test_usage_reads_claude_transcripts_once_per_request(store, tmp_path):
    from agentplay import usage

    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-me-payments-api"
    (proj / "s1" / "subagents").mkdir(parents=True)
    ts = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    later = (NOW - timedelta(hours=1) + timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    main = proj / "s1.jsonl"
    # One response streamed as three lines (output grows), one more request, a placeholder reply.
    main.write_text(_claude_line(ts, "m1", "r1", 5) + _claude_line(later, "m1", "r1", 40) + _claude_line(later, "m1", "r1", 80)
                    + _claude_line(ts, "m2", "r2", 7) + _claude_line(ts, "m3", "r3", 0, model="<synthetic>"))
    # A resumed session copies m1 into its own transcript; a subagent makes its own request.
    (proj / "s2.jsonl").write_text(_claude_line(ts, "m1", "r1", 80))
    (proj / "s1" / "subagents" / "agent-a1.jsonl").write_text(_claude_line(ts, "m4", "r4", 9))
    r = usage.report(store, NOW, claude_homes=[home])
    assert r["today"]["requests"] == 3
    assert r["today"]["tokens"]["output"] == 80 + 7 + 9  # the finished line, not the first
    assert {m["key"] for m in r["today"]["byModel"]} == {"claude-opus-5-5"}
    assert {m["key"] for m in r["today"]["byRepo"]} == {"payments-api"}  # folder name only
    raw = json.dumps(r) + (store.base / "cache" / "companion-claude-usage.json").read_text()
    assert "SECRET-DIR" not in raw and "SECRET-REPLY" not in raw

    # Appended lines are read from where the last look stopped.
    with open(main, "a") as f:
        f.write(_claude_line(ts, "m5", "r5", 11))
    assert usage.report(Store(store.base), NOW, claude_homes=[home])["today"]["requests"] == 4


def test_usage_map_repeats_of_raw_requests_are_dropped(store, tmp_path):
    from agentplay import usage

    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-me-x"
    proj.mkdir(parents=True)
    at = NOW - timedelta(minutes=30)
    (proj / "a.jsonl").write_text(_claude_line(at.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m1", "r1", 50,
                                               model="claude-sonnet-5"))
    # The same request in a map, timed two seconds earlier (maps time a request by when it started).
    _put(store, _doc("local:claude-code:a", [LS, _llm(0, out=50)], started=at - timedelta(seconds=2)))
    # A Codex request only maps know about.
    _put(store, _doc("local:codex:b", [LS, _llm(0, model="gpt-5.5", cost=0.5)], agent="codex", started=at))
    r = usage.report(store, NOW, claude_homes=[home])
    assert r["today"]["requests"] == 2
    assert {a["key"] for a in r["today"]["byAgent"]} == {"claude-code", "codex"}


def test_claude_limit_hits_from_refusals(tmp_path):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-me-x"
    proj.mkdir(parents=True)

    def refusal(at, kind, resets):
        return json.dumps({"type": "assistant", "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                           "message": {"content": [{"type": "text", "text": "SECRET-ERROR-TEXT"}]},
                           "apiErrorParams": {"rate_limit_info": {"status": "rejected", "rateLimitType": kind,
                                                                  "resetsAt": int(resets.timestamp())}}}) + "\n"

    (proj / "a.jsonl").write_text(
        refusal(NOW - timedelta(hours=3), "five_hour", NOW - timedelta(hours=1))  # already reset
        + refusal(NOW - timedelta(minutes=20), "five_hour", NOW + timedelta(hours=2))
        + refusal(NOW - timedelta(minutes=5), "seven_day", NOW + timedelta(days=2)))
    homes = discover.Homes(*([home] if i == 0 else [tmp_path / "none"] for i in range(len(dataclasses.fields(discover.Homes)))))
    rows = companion.claude_limit_hits(homes, NOW)
    assert [r["window"] for r in rows] == ["five_hour", "weekly"]
    assert rows[0]["used"]["value"]["usedPercent"] == 100.0 and rows[0]["used"]["kind"] == "provider_reported"
    assert rows[0]["resetsAt"] == companion.iso(NOW + timedelta(hours=2))
    assert "SECRET-ERROR-TEXT" not in json.dumps(rows)
    assert companion.claude_limit_hits(homes, NOW + timedelta(days=3)) == []  # both reopened


def test_insights_report_is_cached_and_private(store, empty_homes):
    for i in range(12):
        _put(store, _doc(f"local:claude-code:h{i}", [LS, _llm(0)], started=NOW - timedelta(days=1, minutes=i)))
    ins = companion.insights(store, NOW)
    assert ins["days"] == 30 and ins["sessions"] == 12 and len(ins["components"]) == 5
    assert {c["key"] for c in ins["components"]} == {"shipping", "verification", "efficiency", "context", "environment"}
    assert (store.base / "cache" / "companion-insights.json").exists()
    # Within the cache window the same report comes back without recomputing.
    _put(store, _doc("local:claude-code:late", [LS, _llm(0)], started=NOW - timedelta(hours=1)))
    assert companion.insights(store, NOW + timedelta(minutes=5))["sessions"] == 12
    assert companion.insights(store, NOW + timedelta(minutes=20))["sessions"] == 13
    raw = json.dumps(_snap(store, empty_homes)["insights"])
    for secret in ("SECRET-TITLE", "SECRET-TASK", "SECRET-USER-DIR", "SECRET-REMOTE"):
        assert secret not in raw


def _user_line(ts, uid, text, entrypoint="cli", origin=None):
    d = {"type": "user", "timestamp": ts, "uuid": uid, "entrypoint": entrypoint, "message": {"role": "user", "content": text}}
    if origin:
        d["origin"] = {"kind": origin}
    return json.dumps(d) + "\n"


def test_steering_signals_count_people_not_programs(store, tmp_path):
    from agentplay import usage

    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-me-app"
    proj.mkdir(parents=True)
    ts = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    (proj / "a.jsonl").write_text(
        _user_line(ts, "u1", "no, wait: build the SECRET-FEATURE first")  # a first message is never a correction
        + _user_line(ts, "u2", "why is it still not showing???")          # correction and frustration
        + _user_line(ts, "u3", "[Request interrupted by user]")
        + _user_line(ts, "u4", "nice, ship it")
        + json.dumps({"type": "user", "timestamp": ts, "uuid": "u5", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "The user doesn't want to proceed with this tool use."}]}}) + "\n")
    # A resumed copy of the same lines, and a pipeline driving the SDK: neither is the user talking twice.
    (proj / "b.jsonl").write_text(_user_line(ts, "u2", "why is it still not showing???"))
    (proj / "c.jsonl").write_text(_user_line(ts, "p1", "summarize this file", entrypoint="sdk-ts"))
    (proj / "d.jsonl").write_text(_user_line(ts, "z1", "make the button bigger", entrypoint="sdk-ts", origin="human"))
    s = usage.report(store, NOW, claude_homes=[home])["signals"]["current"]
    assert (s["messages"], s["corrections"], s["frustrated"], s["interrupts"], s["refusals"]) == (4, 1, 1, 1, 1)
    assert s["correctionRate"] == 25.0
    assert "SECRET-FEATURE" not in (store.base / "cache" / "companion-claude-usage.json").read_text()


def test_companion_fix_commands_refuse_cleanly(store, empty_homes):
    from agentplay import fixes

    with pytest.raises(fixes.FixError):
        companion.fix_action(store, "preview", "no-such-fix")
    with pytest.raises(fixes.FixError):
        companion.fix_action(store, "undo", "no-such-entry")


# ---------------------------------------------------------------- malformed input and privacy at the edges
def test_codex_context_skips_malformed_fields(tmp_path):
    good = {"timestamp": "2026-10-23T11:40:00Z", "type": "event_msg", "payload": {
        "type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000}, "model_context_window": 4000}}}
    bad_info = {"timestamp": "2026-10-23T11:50:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": "oops"}}
    bad_usage = {"timestamp": "2026-10-23T11:55:00Z", "type": "event_msg", "payload": {
        "type": "token_count", "info": {"last_token_usage": [1, 2], "model_context_window": 4000}}}
    p = tmp_path / "a.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (good, bad_info, bad_usage)) + "\n")
    ctx = companion.codex_context(str(p))
    assert ctx["value"] == {"tokens": 1000, "limit": 4000, "percent": 25.0}  # the newest well-formed count
    p.write_text(json.dumps(bad_info) + "\n")
    assert companion.codex_context(str(p)) is None


def test_hook_cause_names_commands_without_arguments_or_paths(fake_home):
    claude = fake_home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": '"/Users/me/PRIVATE DIR/hooks/notify.sh" --token SECRET-HOOK-ARG'},
        {"type": "command", "command": 'TOKEN=SECRET-ENV pnpm run lint'},
        {"type": "command", "command": '"/Users/me/PRIVATE DIR/hook.sh'},  # an unbalanced quote
    ]}]}}))
    homes = discover.Homes(*([claude] if i == 0 else [fake_home / "none"] for i in range(len(dataclasses.fields(discover.Homes)))))
    cause = companion.hook_cause(homes, "SessionStart:startup")
    # The script a hook runs, never its launcher ("pnpm run"); a line with an unbalanced quote names nothing.
    assert cause["commands"] == ["notify.sh", "lint"]
    assert cause["settingsFiles"] == ["~/.claude/settings.json"]
    raw = json.dumps(cause)
    for secret in ("SECRET-HOOK-ARG", "SECRET-ENV", "PRIVATE", "/Users/me", "--token"):
        assert secret not in raw, secret


def test_malformed_index_shape_is_reported_not_a_crash(store, empty_homes):
    store.index_path.parent.mkdir(parents=True)
    for sessions in ([], {"a": None}):
        store.index_path.write_text(json.dumps({"version": INDEX_VERSION, "sessions": sessions}))
        snap = _snap(Store(store.base), empty_homes)
        assert snap["store"]["indexError"] and snap["runs"] == [] and snap["store"]["available"] is False
        assert all(s["status"] == "error" and s["remedy"] for s in snap["sources"])
    # An index from another version is empty, not broken: the next build rewrites it.
    store.index_path.write_text(json.dumps({"version": INDEX_VERSION + 100, "sessions": []}))
    assert _snap(Store(store.base), empty_homes)["store"]["indexError"] is None


def test_idle_live_session_has_no_pending_tool(store, tmp_path):
    home = tmp_path / ".claude"
    (home / "sessions").mkdir(parents=True)
    me = os.getpid()
    rec = home / "sessions" / f"{me}.json"
    homes = discover.Homes(*([home] if i == 0 else [tmp_path / "none"] for i in range(len(dataclasses.fields(discover.Homes)))))
    src = tmp_path / "s.jsonl"
    src.write_text("x")
    os.utime(src, (NOW.timestamp(), NOW.timestamp()))
    evs = [_exec(0, "ls", kind="exec"), _exec(1, "python", True, "no_result", kind="exec")]
    _put(store, _doc("local:claude-code:a", evs), source=src, mtime=NOW.timestamp())
    # Claude Code says the turn is over: the call with no result is neither running nor failed.
    rec.write_text(json.dumps({"pid": me, "sessionId": "a", "status": "idle", "cwd": "/x/api"}))
    run = companion.snapshot(store, now=NOW, homes=homes)["runs"][0]
    assert run["liveStatus"] == "idle" and run["activity"]["value"] == "active"
    assert run["pendingTool"] is None
    assert run["lastEvent"]["pending"] is False and run["lastEvent"]["err"] is False
    assert run["lastError"] is None
    # Working, the same call is a running tool.
    rec.write_text(json.dumps({"pid": me, "sessionId": "a", "status": "busy", "cwd": "/x/api"}))
    run = companion.snapshot(Store(store.base), now=NOW, homes=homes)["runs"][0]
    assert run["liveStatus"] == "busy" and run["pendingTool"] is not None and run["lastEvent"]["pending"]


def test_usage_cache_holds_no_transcript_paths_and_ignores_the_old_format(store, tmp_path):
    from agentplay import usage

    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-me-SECRET-PROJECT-DIR"
    proj.mkdir(parents=True)
    ts = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    f = proj / "s1.jsonl"
    f.write_text(_claude_line(ts, "m1", "r1", 5))
    cache = store.base / "cache" / "companion-claude-usage.json"
    cache.parent.mkdir(parents=True)
    # A cache from before keys were digests, with a path as its key: ignored and rebuilt, never a crash.
    cache.write_text(json.dumps({"version": 5, "files": {str(f): {"size": 1, "mtime": 1.0, "offset": 0, "reqs": []}}}))
    assert usage.report(store, NOW, claude_homes=[home])["today"]["requests"] == 1
    text = cache.read_text()
    for secret in (str(tmp_path), "SECRET-PROJECT-DIR", "s1.jsonl", "/"):
        assert secret not in text, secret
    assert "local:claude-code:s1" in text  # the session id, which the snapshot shows anyway
    assert usage.report(Store(store.base), NOW, claude_homes=[home])["today"]["requests"] == 1  # the cache hit


def test_usage_reads_a_transcript_line_by_line(store, tmp_path):
    from agentplay import usage

    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-me-app"
    proj.mkdir(parents=True)
    ts = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    f = proj / "a.jsonl"
    whole = _claude_line(ts, "m1", "r1", 5)
    partial = _claude_line(ts, "m2", "r2", 7).rstrip("\n")  # still being written
    f.write_text(whole + partial)
    assert usage.report(store, NOW, claude_homes=[home])["today"]["requests"] == 1
    (hit,) = json.loads((store.base / "cache" / "companion-claude-usage.json").read_text())["files"].values()
    assert hit["offset"] == len(whole.encode())  # the next look starts at the unfinished line
    with open(f, "a") as fh:
        fh.write("\n")
    assert usage.report(Store(store.base), NOW, claude_homes=[home])["today"]["requests"] == 2


def test_merge_keeps_every_raw_request():
    from agentplay import usage

    sec = int(NOW.timestamp())
    a = (sec, "claude-sonnet-5", 10, 100, 1000, 50, 0.01, "claude-code", "api", "local:claude-code:a")
    b = (sec + 2, *a[1:])  # a distinct request that happens to match in model and token counts
    copy = (sec + 1, *a[1:6], 0.01, "claude-code", "api", "local:claude-code:a")  # a map's copy of a
    other = (sec + 60, *a[1:])
    assert usage.merge([a, b], [copy, other]) == [a, b, other]
# ---------------------------------------------------------------- review fixes
def _claude_homes(tmp_path, home):
    return discover.Homes(*([home] if i == 0 else [tmp_path / "none"] for i in range(len(dataclasses.fields(discover.Homes)))))


def test_live_claude_sessions_never_signal_off_posix(tmp_path, monkeypatch):
    # os.kill(pid, 0) on Windows is TerminateProcess: it must never be called there.
    home = tmp_path / ".claude"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "abc", "status": "busy"}))
    called = []
    monkeypatch.setattr(companion.os, "name", "nt")
    monkeypatch.setattr(companion.os, "kill", lambda *a: called.append(a) or (_ for _ in ()).throw(AssertionError))
    assert companion.live_claude_sessions(_claude_homes(tmp_path, home), NOW) == []
    assert called == []


def test_hook_cause_keeps_only_the_program_name(tmp_path, monkeypatch, store):
    from agentplay import health

    home = tmp_path / ".claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "TOKEN=sk-X /bin/hook.sh --a"},
        {"type": "command", "command": "\"$HOME/SECRET-DIR/x y.sh\" 'unbalanced"},
        {"type": "command", "command": "   "}]}]}}))
    homes = _claude_homes(tmp_path, home)
    assert companion.hook_cause(homes, "SessionStart:startup")["commands"] == ["hook.sh"]
    fix = {"id": "f1", "rule": "broken_hook", "subject": "SessionStart:startup", "component": "environment",
           "title": "Fix the hook", "pattern": "p", "effect": "e", "actions": []}
    monkeypatch.setattr(health, "report", lambda *a, **k: {"sessions": 0, "minSessions": 0, "score": None,
                                                           "prevScore": None, "components": [], "fixes": [fix]})
    snap = _snap(store, homes)
    assert snap["insights"]["fixes"][0]["hook"]["commands"] == ["hook.sh"]
    raw = json.dumps(snap) + (store.base / "cache" / "companion-insights.json").read_text()
    for secret in ("sk-X", "--a", "TOKEN", "SECRET-DIR", "/bin/"):
        assert secret not in raw, secret


def test_opus_only_refusal_is_not_the_weekly_window(tmp_path):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-Users-me-x"
    proj.mkdir(parents=True)
    (proj / "a.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "apiErrorParams": {"rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day_opus",
                                               "resetsAt": int((NOW + timedelta(days=2)).timestamp())}}}) + "\n")
    assert companion.claude_limit_hits(_claude_homes(tmp_path, home), NOW) == []


def test_pending_call_neither_resolves_nor_counts():
    doc = _doc("local:claude-code:a", [_exec(i, "pytest", True) for i in range(3)] + [_exec(3, "pytest", True, "no_result")])
    a = companion.repeated_failures(doc, NOW, pending_seq=3)
    assert len(a) == 1 and a[0]["resolvedSeq"] is None and a[0]["count"] == 3
    ok = _doc("local:claude-code:a", [_exec(i, "pytest", True) for i in range(3)] + [_exec(3, "pytest")])
    assert companion.repeated_failures(ok, NOW, pending_seq=3)[0]["resolvedSeq"] is None


def test_each_streak_of_one_failure_is_its_own_alert(store, empty_homes):
    evs = [_exec(i, "pytest", True) for i in range(3)] + [_exec(3, "pytest")] + [_exec(i, "pytest", True) for i in range(4, 7)]
    a = _alerts(store, empty_homes, evs)
    assert [(x["firstSeq"], x["resolvedSeq"], x["count"]) for x in a] == [(0, 3, 3), (4, None, 3)]
    assert len({x["signature"] for x in a}) == 2


def test_process_evidence_skips_the_file_lsof_when_no_transcript_exists(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(companion, "_lsof", lambda args: calls.append(args) or "")
    companion.process_evidence({"a": ("claude-code", str(tmp_path / "gone.jsonl"))})
    assert calls == [["-a", "-d", "cwd", "-c", "claude", "-Fn"]]  # the claude cwd check still runs
    calls.clear()
    (tmp_path / "here.jsonl").write_text("")
    companion.process_evidence({"a": ("codex", str(tmp_path / "here.jsonl"))})
    assert calls == [["-Fn", "--", str(tmp_path / "here.jsonl")]]


def test_fix_and_autotune_actions_drop_the_insights_cache(store, monkeypatch):
    from agentplay import autotune, fixes

    cache = store.base / "cache" / "companion-insights.json"
    monkeypatch.setattr(fixes, "undo", lambda base, ident: {"id": ident})
    monkeypatch.setattr(autotune, "approve", lambda st, fid: {"id": fid})
    monkeypatch.setattr(autotune, "dismiss", lambda st, fid: None)
    for call in (lambda: companion.fix_action(store, "undo", "e1"),
                 lambda: companion.autotune_action(store, "approve", "f1"),
                 lambda: companion.autotune_action(store, "dismiss", "f1")):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{}")
        call()
        assert not cache.exists()
    companion.autotune_action(store, "dismiss", "f1")  # no cache file: still fine


def test_codex_context_skips_malformed_records(tmp_path):
    def rec(info):
        return json.dumps({"timestamp": "2026-10-23T11:50:00Z", "type": "event_msg",
                           "payload": {"type": "token_count", "info": info}}) + "\n"

    p = tmp_path / "a.jsonl"
    p.write_text(rec({"last_token_usage": {"input_tokens": 100}, "model_context_window": 1000})
                 + rec({"last_token_usage": "bad"}) + rec("bad") + rec(["bad"]))
    assert companion.codex_context(str(p))["value"] == {"tokens": 100, "limit": 1000, "percent": 10.0}


@pytest.mark.parametrize("body", ['{"version": 1, "sessions": []}', '{"version": 1, "sessions": 5}'])
def test_index_of_the_wrong_shape_is_reported(store, empty_homes, body):
    store.index_path.parent.mkdir(parents=True)
    store.index_path.write_text(body)
    snap = _snap(store, empty_homes)
    assert snap["store"]["indexError"] and snap["runs"] == [] and snap["store"]["available"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="the companion is macOS-only")
def test_idle_live_session_stays_active_but_runs_no_tool(store, tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    (home / "sessions").mkdir(parents=True)
    me = os.getpid()
    (home / "sessions" / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": "a", "status": "idle"}))
    monkeypatch.setattr(companion, "process_evidence", lambda paths: {"local:claude-code:a": "a claude process is running"})
    _put(store, _doc("local:claude-code:a", [_exec(0, "ls", kind="exec"), _exec(1, "pytest", True, "no_result")]))
    snap = companion.snapshot(store, now=NOW, homes=_claude_homes(tmp_path, home))
    run = snap["runs"][0]
    assert run["liveStatus"] == "idle" and run["activity"]["value"] == "active" and run["pendingTool"] is None
    assert run["recentEvents"][-1]["err"] is False  # the turn ended: the last call is neither running nor failed
    assert [r["sessionId"] for r in snap["live"]] == ["local:claude-code:a"]


def test_hook_executable_with_unbalanced_quotes_shows_nothing():
    # A whitespace split would cut the quoted folder and show "PRIVATE-DIR/x" as the program.
    assert companion._hook_executable('"/Users/me/PRIVATE DIR/hook.sh --a') is None
    assert companion._hook_executable("TOKEN=x /bin/hook.sh --a") == "hook.sh"


def test_hook_executable_names_the_script_not_the_launcher():
    f = companion._hook_executable
    assert f("cd /x && ./h.sh") == "h.sh"
    assert f("env K=v hook") == "hook"
    assert f("bash ~/x.sh") == "x.sh"
    assert f("/Users/me/my proj/h.sh") == "h.sh"
    assert f("uv run python ~/hooks/check.py --fast") == "check.py"
    assert f("python3.12 /opt/a/notify.py > /tmp/LOG.txt") == "notify.py"
    assert f("my-hook 2>&1 | tee /tmp/out") == "my-hook"
    assert f("lint; notify.sh > /tmp/x.log") == "notify.sh"


# ---------------------------------------------------------------- applied changes are measured, not re-offered
def test_hook_fix_carries_its_own_measure():
    from agentplay import autotune, health

    rows = [{"sessionId": f"s{i}", "repos": ["r"], "facts": {"hookFails": {"SessionStart:startup": 1}}} for i in range(6)]
    fx = next(f for f in health.fixes(rows) if f["rule"] == "broken_hook")
    assert fx["metric"] == {"name": "hook_fails", "verb": "SessionStart:startup"}
    assert autotune.metric(rows, fx["metric"]) == 1.0
    assert autotune.metric([{"facts": {}}] * 3, fx["metric"]) == 0.0


def test_user_level_change_touches_every_session():
    from agentplay import autotune

    assert autotune.touches("~/.claude", {"repos": ["anything"]})
    assert autotune.touches(None, {"repos": []})
    assert autotune.touches("r", {"repos": ["r"]}) and not autotune.touches("r", {"repos": ["other"]})


def test_applied_entry_says_measuring_then_worked_or_no_change():
    e = {"id": "x", "metric": {"name": "hook_fails", "verb": "h"}, "baseline": 1.0}
    assert companion._applied_entry({**e, "current": 0.0, "sessionsSince": 1})["verdict"] == "measuring"
    assert companion._applied_entry({**e, "current": 0.0, "sessionsSince": 5})["verdict"] == "worked"
    assert companion._applied_entry({**e, "current": 1.0, "sessionsSince": 5})["verdict"] == "no_change"
    assert companion._applied_entry({"id": "y", "sessionsSince": 5})["verdict"] == "unmeasured"


# ---------------------------------------------------------------- the person's own setup
def test_setup_inventory_counts_uses_by_name_only(tmp_path):
    from datetime import UTC, datetime

    from agentplay import setup_inventory, usage

    home = tmp_path / ".claude"
    (home / "skills" / "launchcut").mkdir(parents=True)
    (home / "skills" / "launchcut" / "SKILL.md").write_text("---\nname: launchcut\ndescription: x\n---\n")
    (home / "skills" / "idle").mkdir()
    (home / "skills" / "idle" / "SKILL.md").write_text("---\nname: idle\n---\n")
    (home / "agents").mkdir()
    (home / "agents" / "judge.md").write_text("---\nname: judge\n---\n")
    (home / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "/Users/me/SECRET/start.sh --token SECRET-ARG"},
        {"type": "command", "command": "other.sh"}]}]}}))
    lines = [
        {"type": "assistant", "timestamp": "2026-10-26T10:00:00Z", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "launchcut"}},
            {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "judge", "prompt": "SECRET PROMPT"}},
            {"type": "tool_use", "name": "mcp__figma__use_figma", "input": {}}]}},
        {"type": "user", "timestamp": "2026-10-26T10:01:00Z",
         "message": {"content": "<command-name>/launchcut</command-name> SECRET TEXT"}},
    ]
    uses = [u for line in lines for u in usage._setup_line(json.dumps(line).encode())]
    assert sorted(u[1:] for u in uses) == [["agent", "judge"], ["command", "launchcut"], ["mcp", "figma"], ["skill", "launchcut"]]
    rep = setup_inventory.report(home, uses, datetime(2026, 10, 27, tzinfo=UTC))
    by = {(i["kind"], i["name"], i.get("detail")): i for i in rep["items"]}
    assert by[("skill", "launchcut", None)]["uses"] == 2 and by[("skill", "idle", None)]["verdict"] == "unused"
    assert by[("agent", "judge", None)]["uses"] == 1
    assert ("hook", "SessionStart", "start.sh") in by and ("hook", "SessionStart", "other.sh") in by
    raw = json.dumps(rep)
    for secret in ("SECRET PROMPT", "SECRET TEXT", "SECRET-ARG", "/Users/me"):
        assert secret not in raw, secret


def test_impact_measures_each_tool_from_maps_and_splits_your_sessions(tmp_path):
    from datetime import UTC, datetime, timedelta

    from agentplay import impact
    from agentplay.store import Store

    st = Store(tmp_path / "ap")
    now = datetime(2026, 10, 27, tzinfo=UTC)

    def put(i: int, turns: int, events: list, errors: int = 0) -> None:
        sid = f"local:claude-code:s{i}"
        st.put({"session": {"id": sid}, "events": events},
               {"sessionId": sid, "startedAt": (now - timedelta(hours=i + 1)).isoformat(), "role": "root",
                "userTurns": turns, "calls": 100, "errors": errors, "retries": 0, "compactions": 0,
                "costUsd": 1.0, "repos": ["payments-api"], "sessionTitle": "SECRET PROMPT"})

    fig = lambda seq, err=False: {"kind": "mcp", "server": "figma", "tool": "get", "seq": seq, "err": err,  # noqa: E731
                                  "errClass": "auth" if err else None, "outBytes": 400_000, "dur": 1000}
    for i in range(5):  # yours, with figma: big results, few errors
        put(i, 4, [fig(3), fig(9)], errors=1)
    for i in range(5, 11):  # yours, without
        put(i, 4, [{"kind": "exec", "tool": "Bash", "seq": 1}], errors=8)
    put(11, 1, [fig(1, err=True)] * 4)  # a one-prompt run: counted as automated
    put(40 * 24, 4, [fig(1)])  # older than the window
    r = impact.measure(st, now)
    assert r["sessions"] == {"yours": 11, "automated": 1, "yoursCostUsd": 11.0, "automatedCostUsd": 1.0, "yoursCompacted": 0}
    f = next(i for i in r["items"] if i["name"] == "figma")
    assert (f["calls"], f["failed"], f["sessions"], f["yours"]) == (14, 4, 6, 5)
    assert f["tokens"] == 14 * 100_000
    assert f["compare"]["with"] == 5 and f["compare"]["without"] == 6
    assert f["compare"]["verdict"] == "fewer failed calls"
    assert f["advice"]["kind"] == "failing" and "its login" in f["advice"]["text"]
    assert len(f["recent"]) == 3 and f["recent"][0]["sessionId"] == "local:claude-code:s0" and f["recent"][0]["seq"] == 3
    assert "SECRET" not in json.dumps(r)


def test_impact_advice_rules():
    from agentplay import impact

    base = {"calls": 100, "failed": 0, "tokens": 0, "sessions": 2, "errClasses": {}, "compare": {"verdict": "too few"}}
    assert impact.advice({**base, "failed": 30, "errClasses": {"timeout": 30}})["kind"] == "failing"
    assert impact.advice({**base, "tokens": 200_000})["kind"] == "heavy"
    assert impact.advice({**base, "compare": {"verdict": "fewer compactions"}})["kind"] == "helps"
    assert impact.advice(base) == {"kind": "fine", "text": "All 100 calls worked."}


def test_setup_lists_connectors_and_builtins_seen_in_sessions(tmp_path):
    from datetime import UTC, datetime

    from agentplay import setup_inventory

    home = tmp_path / ".claude"
    home.mkdir()
    m = lambda kind, name, calls: {"kind": kind, "name": name, "calls": calls, "failed": 0, "tokens": 0,  # noqa: E731
                                   "sessions": 1, "yours": 1, "recent": [], "advice": {"kind": "fine", "text": "x"}}
    rep = setup_inventory.report(home, [], datetime(2026, 10, 27, tzinfo=UTC), measured={
        "items": [m("mcp", "claude_ai_Slack", 9), m("agent", "Explore", 4)], "sessions": {"yours": 1}})
    by = {i["name"]: i for i in rep["items"]}
    assert by["Slack"]["source"] == "claude.ai connector" and by["Slack"]["uses"] == 9
    assert by["Explore"]["source"] == "built in" and by["Explore"]["verdict"] == "used"
    assert rep["sessions"] == {"yours": 1}


def test_tips_say_what_happened_and_what_to_do():
    from agentplay import impact

    def m(kind: str, name: str, advice: str, **kw: Any) -> dict:
        base = {"calls": 100, "failed": 0, "tokens": 0, "sessions": 4, "compare": {}, "advice": {"kind": advice, "text": "x. Check its login."}}
        return {"kind": kind, "name": name, "source": "you", "verdict": "used", "measured": {**base, **kw}}

    items = [m("mcp", "figma", "heavy", tokens=800_000), m("mcp", "linear", "failing", failed=40),
             m("agent", "Explore", "helps", compare={"with": 6, "without": 9, "verdict": "fewer failed calls",
                                                       "errorRateWith": 4.15, "errorRateWithout": 12.2})]
    items += [{"kind": "skill", "name": f"s{i}", "source": "you", "verdict": "unused"} for i in range(5)]
    setup = {"days": 30, "items": items, "sessions": {"yours": 20, "yoursCompacted": 4}}
    few = {t["kind"] for t in impact.tips(setup, {("skill", f"s{i}"): "short" for i in range(5)})}
    assert few == {"failing", "context", "room", "use"}  # unused setup that costs little isn't a tip
    big = impact.tips(setup, {("skill", f"s{i}"): "x" * 2000 for i in range(5)})
    by = {t["kind"]: t for t in big}
    assert by["unused"]["action"] == "skills_folder" and "tokens" in by["unused"]["what"]
    assert "200k tokens" in by["context"]["what"] and by["failing"]["todo"] == "Check its login."
    assert "4% of calls failed, against 12%" in by["use"]["what"]
    for t in big:
        assert t["title"] and t["what"] and t["todo"] and "\u2014" not in json.dumps(t)


def test_coach_keeps_only_checked_suggestions():
    from agentplay import coach

    reply = 'Sure:\n{"suggestions": [' + ",".join([
        '{"kind": "use-skill", "title": "Use tdd — always", "why": "Fewer corrections.", "name": "tdd"}',
        '{"kind": "use-skill", "title": "Use magic", "why": "It is great.", "name": "not-installed"}',
        '{"kind": "weird", "title": "Rest", "why": "Breaks help.", "name": null}',
        '{"kind": "habit", "title": "", "why": "no title"}',
        '{"kind": "habit", "title": "Four", "why": "one too many"}',
        '{"kind": "habit", "title": "Five", "why": "two too many"}']) + "]}"
    got = coach.parse(reply, {"tdd"})
    assert len(got) == 3
    assert got[0] == {"kind": "use-skill", "title": "Use tdd , always", "why": "Fewer corrections.", "name": "tdd"}
    assert got[1]["name"] is None and got[2]["kind"] == "habit"
    assert coach.parse("no json here", {"tdd"}) == []


def test_coach_runs_isolated_once_a_day_and_sends_no_text(tmp_path, monkeypatch):
    monkeypatch.delenv("USER", raising=False)  # as the app starts the core
    monkeypatch.delenv("LOGNAME", raising=False)
    import os
    import stat
    from datetime import UTC, datetime, timedelta

    from agentplay import coach
    from agentplay.store import Store

    home = tmp_path / ".claude"
    (home / "skills" / "tdd").mkdir(parents=True)
    (home / "skills" / "tdd" / "SKILL.md").write_text("---\nname: tdd\ndescription: Write the test first.\n---\nSECRET BODY\n")
    seen = tmp_path / "seen"
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n"
                    f'printf "%s\\n" "$@" > "{seen}.args"\n'
                    f'echo "$USER" > "{seen}.user"\n'
                    f'cat > "{seen}.stdin"\n'
                    'echo \'{"result": "{\\"suggestions\\": [{\\"kind\\": \\"use-skill\\", \\"title\\": \\"Use tdd\\", '
                    '\\"why\\": \\"It fits.\\", \\"name\\": \\"tdd\\"}]}", "usage": {"input_tokens": 10, "output_tokens": 5}}\'\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    st = Store(tmp_path / "ap")
    snap = {"usage": {"setup": {"items": [{"kind": "skill", "name": "tdd", "uses": 2}]},
                      "signals": {"current": {"messages": 40, "correctionRate": 7.5}}},
            "insights": {"score": 61, "components": [{"name": "Verification", "points": 8.5}], "fixes": [{"title": "Run checks"}]},
            "runs": [{"title": "SECRET PROMPT"}]}
    now = datetime(2026, 10, 27, tzinfo=UTC)
    out = coach.run(st, str(fake), snap, home, now=now)
    assert out["ran"] and out["suggestions"][0]["name"] == "tdd" and out["tokens"] == 15
    args = (tmp_path / "seen.args").read_text().split("\n")
    for flag in ("--tools", "--setting-sources", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in args, flag
    assert args[args.index("--model") + 1] == "haiku"
    assert (tmp_path / "seen.user").read_text().strip()  # Claude Code needs it to find its login
    sent = (tmp_path / "seen.stdin").read_text()
    assert "Write the test first." in sent and "Verification" in sent
    assert "SECRET" not in sent
    os.remove(tmp_path / "seen.stdin")
    again = coach.run(st, str(fake), snap, home, now=now + timedelta(hours=5))
    assert not again["ran"] and not (tmp_path / "seen.stdin").exists()
    assert coach.cached(st)["suggestions"] == out["suggestions"]
    assert coach.run(st, str(fake), snap, home, now=now + timedelta(hours=21))["ran"]
