"""Session facts (`facts.py`), their backfill, and `GET /api/stats`."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agentplay import facts, pipeline
from agentplay.server import serve
from agentplay.store import Store

DEMO = Path(__file__).resolve().parents[2] / "viewer" / "public" / "demo"


def _ev(seq: int, kind: str, agent: str = "main", **kw: Any) -> dict[str, Any]:
    return {"seq": seq, "t": seq * 1000, "agent": agent, "err": False, "kind": kind, **kw}


def _llm(seq: int, cost: float, agent: str = "main", **kw: Any) -> dict[str, Any]:
    return _ev(seq, "llm", agent, model="claude-sonnet-4-5", cost=cost, **{"in": 100, "cw": 0, "cr": 900, "out": 50, **kw})


def _doc(events: list[dict[str, Any]], *, outcome: str = "completed", sid: str = "s1",
         started: str = "2026-10-01T10:00:00Z", agents: list[dict[str, Any]] | None = None,
         totals: dict[str, Any] | None = None, role: str = "root") -> dict[str, Any]:
    return {
        "schema": "agentplay/v1",
        "session": {"id": sid, "agentTool": "claude-code", "startedAt": started, "durationMs": 60000,
                    "models": ["claude-sonnet-4-5"], "outcome": outcome, "role": role},
        "totals": {"calls": 5, "requests": 3, "userTurns": 2, "subagents": 0, "errors": 1, "tokens": 0,
                   "costUsd": sum(e.get("cost", 0) for e in events), "filesTouched": 2, "prsCreated": 0,
                   **(totals or {})},
        "repos": [{"id": "r0", "name": "acme/api"}],
        "files": [{"id": 0, "repo": "r0", "path": "src/a.ts", "reads": 2, "edits": 1, "hits": 3},
                  {"id": 1, "repo": "r0", "path": "src/unused.ts", "reads": 0, "edits": 0, "hits": 0}],
        "agents": agents or [{"id": "main", "kind": "main"}],
        "events": events,
    }


def test_commands_files_prompt_and_verify():
    doc = _doc([
        _ev(0, "user", chars=420),
        _llm(1, 0.10),
        _ev(2, "verify", verb="pytest", err=True, errClass="exit_code"),
        _llm(3, 0.20),
        _ev(4, "verify", verb="pytest", retryOf=2),
        _ev(5, "edit", targets=[{"file": 0, "touch": "edit"}]),
        _ev(6, "verify", verb="pytest"),
        _ev(7, "user", chars=30),
    ])
    f = facts.compute(doc)
    assert f["cmd"]["pytest"] == {"calls": 3, "fails": 1, "retries": 1, "byClass": {"exit_code": 1},
                                  "verify": True, "outBytes": 0}
    assert f["files"] == {"acme/api:src/a.ts": {"hits": 3, "reads": 2, "edits": 1}}  # untouched files dropped
    assert f["firstPromptChars"] == 420 and f["userTurns"] == 2
    assert f["edited"] is True and f["verifyAfterLastEdit"] is True
    assert f["errorsBySource"] == {"tool": {"exit_code": 1}}


def test_waste_charges_each_request_once_in_bucket_order():
    # The failed call charges the next request as a retry; the rest of an
    # abandoned session is abandoned spend, never both.
    doc = _doc([
        _llm(0, 0.5),
        _ev(1, "exec", verb="make", err=True, errClass="exit_code"),
        _llm(2, 1.0),
        _llm(3, 2.0),
    ], outcome="unresolved_errors")
    w = facts.compute(doc)["waste"]
    assert w["retry"] == pytest.approx(1.0)
    assert w["abandoned"] == pytest.approx(2.5)
    assert w["cacheMiss"] == 0.0  # every request already charged


def test_rereads_and_dead_subagents():
    doc = _doc([
        _ev(0, "read", targets=[{"file": 0, "touch": "read"}]),
        _llm(1, 0.1),
        _ev(2, "compaction", preTokens=150000),
        _ev(3, "read", targets=[{"file": 0, "touch": "read"}]),
        _llm(4, 0.3),
        _ev(5, "subagent", err=True, errClass="no_result"),
        _llm(6, 0.7, agent="sub-1"),
    ], agents=[{"id": "main", "kind": "main"}, {"id": "sub-1", "kind": "subagent", "launchSeq": 5}])
    f = facts.compute(doc)
    assert f["rereadsAfterCompaction"] == 1
    assert f["preTokens"] == [150000]
    assert f["waste"]["reread"] == pytest.approx(0.3)
    assert f["waste"]["deadSubagent"] == pytest.approx(0.7)


def test_stamp_survives_a_map_missing_fields():
    assert facts.stamp({"events": [{"kind": "llm"}]}) == {"facts": None, "factsVersion": facts.FACTS_VERSION}


def test_demo_index_facts_are_current():
    """Regenerate with `scripts/demo_facts.py` when this fails."""
    index = json.loads((DEMO / "index.json").read_text(encoding="utf-8"))
    for row in index["sessions"]:
        doc = json.loads((DEMO / row["file"]).read_text(encoding="utf-8"))
        assert row.get("factsVersion") == facts.FACTS_VERSION, row["file"]
        assert row.get("facts") == facts.compute(doc), row["file"]


def _seed(tmp_path: Path, docs: list[dict[str, Any]], *, with_facts: bool) -> Store:
    st = Store(tmp_path)
    for d in docs:
        row = {"sessionId": d["session"]["id"], "sessionDate": d["session"]["startedAt"][:10],
               "startedAt": d["session"]["startedAt"], "agentTool": "claude-code", "models": [],
               "repos": ["acme/api"], "calls": 5, "role": d["session"].get("role"), "contentHash": d["session"]["id"]}
        if with_facts:
            row.update(facts.stamp(d))
        st.put(d, row)
    st.save_index()
    return st


def test_unchanged_sessions_get_facts_backfilled(tmp_path):
    st = _seed(tmp_path, [_doc([_llm(0, 0.1)])], with_facts=False)
    assert pipeline._sync_facts(st, "s1") is True
    assert st.index["s1"]["factsVersion"] == facts.FACTS_VERSION
    assert pipeline._sync_facts(st, "s1") is False  # already current


@pytest.fixture()
def stats_server(tmp_path):
    docs = [
        _doc([_llm(0, 0.1)], sid="old", started="2026-09-01T10:00:00Z"),
        _doc([_llm(0, 0.2)], sid="new", started="2026-10-02T10:00:00Z", totals={"prsCreated": 1}),
        _doc([_llm(0, 0.3)], sid="child", started="2026-10-02T11:00:00Z", role="subagent"),
    ]
    st = _seed(tmp_path, docs, with_facts=False)  # rows written before facts existed
    httpd = serve(0, store=st)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield httpd, st
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(httpd, path: str, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, json.loads(body)


def test_stats_backfills_in_memory_and_filters(stats_server):
    httpd, st = stats_server
    status, body = _get(httpd, "/api/stats")
    assert status == 200
    ids = {s["sessionId"] for s in body["sessions"]}
    assert ids == {"old", "new"}  # subagent sessions are folded into their parent
    new = next(s for s in body["sessions"] if s["sessionId"] == "new")
    assert new["facts"]["prsCreated"] == 1
    assert "facts" not in json.dumps(st.index["new"])  # the server never writes the index
    _, body = _get(httpd, "/api/stats?from=2026-10-01")
    assert [s["sessionId"] for s in body["sessions"]] == ["new"]


def test_stats_and_sessions_keep_facts_apart(stats_server):
    httpd, _ = stats_server
    _, page = _get(httpd, "/api/sessions")
    assert all("facts" not in s for s in page["sessions"])


def test_stats_refuses_a_rebinding_host(stats_server):
    httpd, _ = stats_server
    status, _ = _get(httpd, "/api/stats", {"Host": "evil.example:80"})
    assert status == 403
