"""The companion snapshot: what it derives, and what it must never carry."""

from __future__ import annotations

import io
import json
import os
import socket
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentplay import builder, cli, companion, discover, pipeline
from agentplay.store import Store

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
                   "requests": 0, "subagents": 0, "errors": sum(1 for e in evs if e.get("err")), "filesTouched": 0,
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
    return discover.Homes(*([tmp_path / "none"] for _ in range(7)))


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
    _put(store, _doc("local:claude-code:a", [_exec(0, "pytest", err=True)] * 3), source=Path("/Users/SECRET-USER/x.jsonl"))
    raw = json.dumps(_snap(store, empty_homes))
    for secret in ("SECRET-TITLE", "SECRET-TASK", "SECRET-REMOTE", "SECRET-PR", "SECRET-USER"):
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
