"""Write the companion's synthetic fixtures through the real snapshot code.

    python scripts/companion_fixtures.py [out_dir]

Every fixture is built by `agentplay.companion.snapshot` over synthetic maps in a
temporary store, then marked ``"sample": true``. Nothing here reads your sessions.
The Swift app only loads these in a dev build (`--fixture <name>`), and labels
them "Sample data".
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))

from agentplay import builder, companion, discover  # noqa: E402
from agentplay.store import Store  # noqa: E402

NOW = datetime(2026, 10, 23, 10, 46, 0, tzinfo=UTC)
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parents[1] / "apps" / "macos" / "AgentPlayCompanion" / "Fixtures")


def ev(t_s: float, kind: str, **kw):
    return {"t": int(t_s * 1000), "kind": kind, "agent": "main", "err": False, **kw}


def sh(t_s, verb, err=False, cls="exit_code", kind=None):
    e = ev(t_s, kind or ("verify" if verb in ("pnpm test", "pytest", "tsc", "cargo test") else "exec"),
           tool="Bash", verb=verb, cat="test", err=err)
    if err:
        e["errClass"] = cls
    return e


def llm(t_s, tokens, model="claude-sonnet-5"):
    # Costs as the builder would estimate them at list prices (input + cache + output, roughly).
    cost = round(tokens * 0.3 / 1e6 + 300 * 15 / 1e6, 4)
    return ev(t_s, "llm", model=model, cost=cost, **{"in": 12, "cw": 400, "cr": tokens - 412, "out": 300})


def live_record(base, pid, sid, status, cwd, started):
    """A Claude Code live-process record, as ~/.claude/sessions/<pid>.json holds it."""
    d = base / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}-{sid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": sid, "status": status, "cwd": cwd, "kind": "interactive",
        "startedAt": int(started.timestamp() * 1000), "statusUpdatedAt": int((NOW - timedelta(seconds=40)).timestamp() * 1000)}))


def statusline(base, sid, five, week, ctx_pct, age_s=30):
    """What the optional statusline bridge saves (Claude Code's documented statusline JSON)."""
    d = base / "ap" / "live" / "claude"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.json"
    f.write_text(json.dumps({"session_id": sid,
        "context_window": {"used_percentage": ctx_pct, "context_window_size": 200000},
        "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": int((NOW + timedelta(hours=2, minutes=18)).timestamp())},
                        "seven_day": {"used_percentage": week, "resets_at": int((NOW + timedelta(days=3, hours=4)).timestamp())}}}))
    os.utime(f, (NOW.timestamp() - age_s, NOW.timestamp() - age_s))


def edit(t_s, f=0):
    return ev(t_s, "edit", tool="Edit", cat="file", targets=[{"file": f, "touch": "edit", "op": "modify"}])


def read(t_s, f=0):
    return ev(t_s, "read", tool="Read", cat="file", targets=[{"file": f, "touch": "read"}])


def doc(sid, agent, started, events, repo, files, outcome="completed", models=("claude-sonnet-5",)):
    evs = [{**e, "seq": i} for i, e in enumerate(events)]
    return {
        "schema": "agentplay/v1",
        "session": {"id": sid, "agentTool": agent, "startedAt": companion.iso(started), "models": list(models),
                    "durationMs": evs[-1]["t"], "pathMode": "relative", "outcome": outcome, "role": "root",
                    "parentSessionId": None, "truncated": False},
        "totals": {"calls": sum(1 for e in evs if e["kind"] not in ("llm", "user", "error", "delay", "compaction")),
                   "requests": sum(1 for e in evs if e["kind"] == "llm"), "subagents": 0,
                   "errors": sum(1 for e in evs if e["err"]), "filesTouched": len(files), "costUsd": 0,
                   "tokens": 0, "userTurns": 1, "retries": sum(1 for e in evs if e.get("retryOf") is not None),
                   "compactions": sum(1 for e in evs if e["kind"] == "compaction")},
        "repos": [{"id": "r0", "name": repo}], "files": [{"id": i, "repo": "r0", "path": p} for i, p in enumerate(files)],
        "agents": [{"id": "main", "kind": "main"}], "prs": [], "events": evs,
    }


def put(store, d, src: Path | None, age_s: float | None):
    s = builder.summary(d)
    started = datetime.fromisoformat(d["session"]["startedAt"].replace("Z", "+00:00"))
    row = {**s, "sessionId": d["session"]["id"], "sessionDate": d["session"]["startedAt"][:10],
           "endedAt": companion.iso(started + timedelta(milliseconds=d["session"]["durationMs"])),
           "contentHash": builder.content_hash(d), "sourcePath": None, "sourceMtime": None}
    if src is not None and age_s is not None:
        src.write_text("{}\n")
        os.utime(src, (NOW.timestamp() - age_s, NOW.timestamp() - age_s))
        row["sourcePath"], row["sourceMtime"] = str(src), NOW.timestamp() - age_s
    store.put(d, row)


def codex_file(path: Path, ts: datetime, plan="plus", used=32.0, weekly=41.0, resets_in_h=2.2, window=True,
               tokens=184000, limit=258400):
    rl = {"limit_id": "codex", "plan_type": plan, "primary": None, "secondary": None}
    if window:
        rl["primary"] = {"used_percent": used, "window_minutes": 300,
                         "resets_at": int((ts + timedelta(hours=resets_in_h)).timestamp())}
        rl["secondary"] = {"used_percent": weekly, "window_minutes": 10080,
                           "resets_at": int((ts + timedelta(days=4)).timestamp())}
    rec = {"timestamp": companion.iso(ts), "type": "event_msg", "payload": {"type": "token_count", "rate_limits": rl,
           "info": {"last_token_usage": {"input_tokens": tokens}, "model_context_window": limit}}}
    path.write_text(json.dumps(rec) + "\n")


def payments_run(started):
    return doc("local:claude-code:sample-7f3a", "claude-code", started, [
        ev(0, "user", chars=120), llm(2, 61000), read(5, 0), read(9, 1), edit(30, 0), sh(48, "pnpm test", True),
        llm(60, 88000), edit(75, 0), sh(96, "pnpm test", True), edit(130, 1), sh(170, "pnpm test", True),
        llm(200, 142000), read(215, 1)], "payments-api", ["src/charge.ts", "src/charge.test.ts"])


def codex_run(started):
    return doc("local:codex:sample-0c1d", "codex", started, [
        ev(0, "user", chars=80), read(20, 0), read(60, 1), edit(240, 0), sh(400, "pnpm lint"), edit(600, 0),
        read(640, 2)], "frontend", ["src/cart.ts", "src/cart.css", "src/api.ts"], models=("gpt-5.5-codex",))


def build(name: str, fn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = Store(base / "ap")
        live: dict[str, str] = {}
        homes_present = fn(store, base, live) or {}
        store.save_index()
        companion.process_evidence = lambda paths: {k: v for k, v in live.items() if k in paths}
        homes = discover.Homes(*([base / ".claude"] if i == 0 else [base / "nowhere"] for i in range(7)))
        snap = companion.snapshot(Store(base / "ap"), now=NOW, homes=homes)
        for s in snap["sources"]:
            info = homes_present.get(s["agent"])
            if info:
                s.update(info)
                if s["status"] == "found" and not s["sessionCount"]:
                    s["sessionCount"] = 0
        snap["sample"] = True
        snap["timing"] = {"snapshotMs": 0}
        raw = json.dumps(snap, indent=2).replace(tmp, "/tmp/sample")
        (OUT / f"{name}.json").write_text(raw + "\n")
        keep = os.environ.get("COMPANION_FIXTURE_STORE")
        if keep and name == "two-active":
            # The synthetic maps behind the main sample, for demo captures of the real viewer.
            import shutil
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(base / "ap", keep)
        print(f"wrote {name}.json: {len(snap['runs'])} runs, {len(snap['alerts'])} alerts, "
              f"learn={'yes' if snap['learn'] else 'no'}")


FOUND = {"status": "found", "remedy": None}


def two_active(store, base, live):
    put(store, payments_run(NOW - timedelta(minutes=4)), base / "a.jsonl", 6)
    live["local:claude-code:sample-7f3a"] = "a claude process is running in this project"
    live_record(base, os.getpid(), "sample-7f3a", "busy", "/Users/you/work/payments-api", NOW - timedelta(minutes=4))
    live_record(base, os.getpid(), "sample-9e2b", "waiting", "/Users/you/work/docs-site", NOW - timedelta(minutes=26))
    statusline(base, "sample-7f3a", 34, 58, 71)
    put(store, codex_run(NOW - timedelta(minutes=11)), base / "b.jsonl", 20)
    codex_file(base / "b.jsonl", NOW - timedelta(seconds=20))
    os.utime(base / "b.jsonl", (NOW.timestamp() - 20, NOW.timestamp() - 20))
    live["local:codex:sample-0c1d"] = "transcript held open by a running process"
    for i in range(3):
        put(store, doc(f"local:claude-code:sample-old{i}", "claude-code", NOW - timedelta(days=i + 1), [
            sh(0, "pnpm test", True), edit(20), sh(50, "pnpm test", True), sh(90, "pnpm test")],
            "payments-api", ["src/charge.ts"]), None, None)
    return {"claude-code": {**FOUND, "paths": ["~/.claude"]}, "codex": {**FOUND, "paths": ["~/.codex"]}}


def no_agents(store, base, live):
    return {}


def detected_idle(store, base, live):
    put(store, doc("local:claude-code:sample-idle", "claude-code", NOW - timedelta(hours=26), [
        ev(0, "user", chars=60), llm(3, 40000), read(10), edit(40), sh(70, "pytest"), llm(80, 52000)],
        "billing", ["app/models.py"], outcome="verified"), base / "c.jsonl", 26 * 3600)
    return {"claude-code": {**FOUND, "paths": ["~/.claude"]}}


def stale_quota(store, base, live):
    put(store, codex_run(NOW - timedelta(hours=7)), base / "b.jsonl", 6 * 3600)
    codex_file(base / "b.jsonl", NOW - timedelta(hours=6), used=88.0, resets_in_h=3)
    os.utime(base / "b.jsonl", (NOW.timestamp() - 6 * 3600, NOW.timestamp() - 6 * 3600))
    return {"codex": {**FOUND, "paths": ["~/.codex"]}}


def conflicting(store, base, live):
    put(store, codex_run(NOW - timedelta(minutes=30)), base / "b.jsonl", 600)
    codex_file(base / "b.jsonl", NOW - timedelta(minutes=10), plan="plus", used=32.0)
    other = doc("local:codex:sample-other", "codex", NOW - timedelta(minutes=50), [ev(0, "user", chars=10),
                read(5)], "infra", ["main.tf"], models=("gpt-5.5-codex",))
    put(store, other, base / "d.jsonl", 900)
    codex_file(base / "d.jsonl", NOW - timedelta(minutes=15), plan="pro", used=71.0)
    for p, age in (("b.jsonl", 600), ("d.jsonl", 900)):
        os.utime(base / p, (NOW.timestamp() - age, NOW.timestamp() - age))
    return {"codex": {**FOUND, "paths": ["~/.codex"]}}


def partial(store, base, live):
    put(store, doc("local:cursor:sample-cur", "cursor", NOW - timedelta(minutes=40), [
        ev(0, "user", chars=10), read(4), edit(30), ev(31, "llm", model="cursor-auto", **{"in": 0, "cw": 0, "cr": 0,
                                                                                         "out": 0})],
        "mobile", ["App.tsx"], models=("cursor-auto",)), base / "cursor.db", 120)
    put(store, payments_run(NOW - timedelta(hours=3)), None, None)
    store.index["local:claude-code:sample-7f3a"]["imported"] = "payments.json.gz"
    store.index["local:claude-code:sample-broken"] = {**store.index["local:claude-code:sample-7f3a"],
                                                       "sessionId": "local:claude-code:sample-broken",
                                                       "key": "missing-0000000000"}
    return {"cursor": {**FOUND, "paths": ["~/.cursor"]},
            "claude-code": {"status": "error", "paths": ["~/.claude"],
                            "remedy": "Can't read ~/.claude/projects. Check its permissions, or grant AgentPlay "
                                      "Companion access to that folder."}}


def many(store, base, live):
    for i in range(14):
        agent = ("claude-code", "codex", "claude-code", "opencode")[i % 4]
        d = doc(f"local:{agent}:sample-m{i:02d}", agent, NOW - timedelta(hours=2 * i + 1), [
            ev(0, "user", chars=10), read(5), edit(20), sh(40, "pytest", i % 3 == 0), sh(60, "pytest")],
            "payments-api" if i % 2 else "frontend", ["src/a.py"])
        put(store, d, None, None)
    return {"claude-code": {**FOUND, "paths": ["~/.claude"]}, "codex": {**FOUND, "paths": ["~/.codex"]},
            "opencode": {**FOUND, "paths": ["~/.local/share/opencode"]}}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in (("two-active", two_active), ("no-agents", no_agents), ("detected-idle", detected_idle),
                     ("stale-quota", stale_quota), ("conflicting-plans", conflicting),
                     ("partial-unavailable", partial), ("many-sessions", many)):
        build(name, fn)
