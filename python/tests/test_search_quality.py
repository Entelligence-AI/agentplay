"""Search fallback, per-kind posting caps, unresolved-repo memory and capped facts.files."""

from __future__ import annotations

import http.client
import json
import shutil
import threading
from typing import Any

from test_local_tool import _write_claude_session, env  # noqa: F401 - env is a fixture

from agentplay import discover, facts, pipeline, search
from agentplay.server import serve
from agentplay.store import Store


def _ev(seq: int, kind: str = "exec", **kw: Any) -> dict[str, Any]:
    return {"seq": seq, "t": seq * 1000, "agent": "main", "err": False, "kind": kind, **kw}


def _doc(events: list[dict[str, Any]], files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema": "agentplay/v1",
        "session": {"id": "s1", "agentTool": "claude-code", "startedAt": "2026-10-01T10:00:00Z", "durationMs": 1,
                    "models": [], "outcome": "completed", "role": "root"},
        "totals": {}, "repos": [{"id": "r0", "name": "acme/api"}], "files": files or [], "events": events,
    }


# ------------------------------------------------------------------ argument fallback

def _terms() -> dict[str, dict[str, list[list[Any]]]]:
    return {"a": {"cmd:pnpm test": [[3, "read"]], "cmd:eslint-fix": [[4, "read"]], "cmd:prefix": [[5, "read"]]}}


def test_fallback_needs_a_recorded_command_not_a_substring():
    terms = _terms()
    assert search.argument_fallback(terms, "pnpm test --watch")[0] == "pnpm test"
    assert search.argument_fallback(terms, "pnpm install foo")[0] == "pnpm"  # family of `pnpm test`
    assert search.argument_fallback(terms, "fix login bug") is None  # `fix` is only inside eslint-fix
    assert search.argument_fallback(terms, "pre something") is None


def test_natural_language_query_returns_no_sessions_and_no_note(env):  # noqa: F811
    _write_claude_session(env["home"], "11111111-0000-0000-0000-000000000001", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    httpd = serve(0, store=Store(env["sm"]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        conn.request("GET", "/api/sessions?search=fix%20login%20bug")
        body = json.loads(conn.getresponse().read())
        assert body["sessions"] == [] and body["searchNote"] is None
    finally:
        httpd.shutdown()


# ------------------------------------------------------------------ true counts per touch kind

def _busy_doc() -> dict[str, Any]:
    n = search.MAX_SEQS + 10
    evs = [_ev(i, "read", targets=[{"file": 0, "touch": "read"}]) for i in range(n)]
    evs += [_ev(n, "edit", targets=[{"file": 0, "touch": "edit"}]), _ev(n + 1, "edit", targets=[{"file": 0, "touch": "edit"}])]
    return _doc(evs, [{"id": 0, "repo": "r0", "path": "src/a.ts"}])


def test_busy_file_keeps_its_edits_and_true_counts():
    n = search.MAX_SEQS + 10
    hit = search.query({"s": search.postings(_busy_doc())}, "file:a.ts")["s"]
    by_touch = {c["touch"]: c for c in hit["chips"]}
    assert by_touch["edit"]["n"] == 2 and by_touch["read"]["n"] == n
    assert by_touch["read"]["label"] == f"read a.ts {n}×"
    assert hit["count"] == n + 2
    assert {n, n + 1} <= set(hit["seqs"])  # the stepper can reach the edits
    stored = search.postings(_busy_doc())["file:a.ts"]
    assert len(stored) == search.MAX_SEQS + 2  # stored size stays bounded


def test_old_search_index_is_rebuilt(tmp_path):
    st = Store(tmp_path)
    doc = _busy_doc()
    st.put(doc, {"sessionId": "s1", "contentHash": "h1", "calls": 1})
    st.save_index()
    (tmp_path / "search.json").write_text(json.dumps({"version": search.SEARCH_VERSION - 1, "sessions": {
        "s1": {"hash": "h1", "terms": {"file:a.ts": [[1, "read"]]}}}}))
    assert search.refresh(st) == 1


def test_empty_content_hash_is_always_stale(tmp_path):
    st = Store(tmp_path)
    st.put(_busy_doc(), {"sessionId": "s1", "contentHash": "", "calls": 1})
    st.save_index()
    assert search.refresh(st) == 1
    assert search.refresh(st) == 1


def test_search_file_with_non_dict_terms_is_rebuilt_not_a_500(tmp_path):
    st = Store(tmp_path)
    st.put(_busy_doc(), {"sessionId": "s1", "contentHash": "h1", "calls": 1, "sessionDate": "2026-10-01"})
    st.save_index()
    (tmp_path / "search.json").write_text(json.dumps({"version": search.SEARCH_VERSION, "sessions": {
        "s1": {"hash": "h1", "terms": ["oops"]}}}))
    httpd = serve(0, store=Store(tmp_path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        conn.request("GET", "/api/sessions?search=a.ts")
        resp = conn.getresponse()
        assert resp.status == 200
        assert [s["sessionId"] for s in json.loads(resp.read())["sessions"]] == ["s1"]
    finally:
        httpd.shutdown()


# ------------------------------------------------------------------ unresolved repos

def test_unresolved_repo_is_not_re_parsed_every_run(env, monkeypatch):  # noqa: F811
    _write_claude_session(env["home"], "11111111-0000-0000-0000-000000000001", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    (env["sm"] / "repos.json").unlink()
    shutil.rmtree(env["repo"])  # the checkout is gone: the name can never resolve again
    calls: list[str] = []
    real = discover.build_map
    monkeypatch.setattr(discover, "build_map", lambda *a, **k: (calls.append("x"), real(*a, **k))[1])
    for _ in range(3):
        pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert len(calls) == 1


def test_unresolved_repo_is_retried_after_the_wait(env, monkeypatch):  # noqa: F811
    _write_claude_session(env["home"], "11111111-0000-0000-0000-000000000001", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    (env["sm"] / "repos.json").unlink()
    shutil.rmtree(env["repo"])
    calls: list[str] = []
    real = discover.build_map
    monkeypatch.setattr(discover, "build_map", lambda *a, **k: (calls.append("x"), real(*a, **k))[1])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    monkeypatch.setattr(pipeline, "UNRESOLVED_RETRY_S", 0)
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert len(calls) == 2


def test_a_lookup_that_crashed_is_not_cached_as_unresolved(env, monkeypatch):  # noqa: F811
    _write_claude_session(env["home"], "11111111-0000-0000-0000-000000000001", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    (env["sm"] / "repos.json").unlink()

    def broken(*a, **k):
        raise RuntimeError("transcript unreadable")

    monkeypatch.setattr(discover, "build_map", broken)
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    # The parse failed, not the lookup: the checkout still exists and is found on the next run.
    assert not (env["sm"] / "repos-unresolved.json").exists()


# ------------------------------------------------------------------ capped facts.files

def test_facts_files_are_capped_to_the_busiest_with_a_remainder():
    n = facts.MAX_FACT_FILES + 25
    files = [{"id": i, "repo": "r0", "path": f"f{i}.ts", "hits": i + 1, "reads": 1, "edits": 0} for i in range(n)]
    f = facts.compute(_doc([], files))
    assert len(f["files"]) == facts.MAX_FACT_FILES
    assert f"acme/api:f{n - 1}.ts" in f["files"] and "acme/api:f0.ts" not in f["files"]
    rest = f["filesRest"]
    assert rest["count"] == 25 and rest["reads"] == 25 and rest["hits"] == sum(range(1, 26))
    assert sum(x["reads"] for x in f["files"].values()) + rest["reads"] == n


def test_small_sessions_have_no_remainder():
    f = facts.compute(_doc([], [{"id": 0, "repo": "r0", "path": "a.ts", "hits": 1, "reads": 1, "edits": 0}]))
    assert "filesRest" not in f


def test_stats_serves_every_file_of_a_capped_session(tmp_path):
    """A path below each session's cap still reaches the cross-session heatmap."""
    st = Store(tmp_path)
    for sid in ("s1", "s2"):
        busy = [{"id": i, "repo": "r0", "path": f"{sid}/f{i}.ts", "hits": 5, "reads": 1, "edits": 0}
                for i in range(facts.MAX_FACT_FILES + 10)]
        shared = {"id": 9999, "repo": "r0", "path": "shared.ts", "hits": 1, "reads": 1, "edits": 0}
        doc = _doc([], busy + [shared])
        doc["session"]["id"] = sid
        stamped = facts.stamp(doc)
        assert "acme/api:shared.ts" not in stamped["facts"]["files"]  # the stored facts stay capped
        st.put(doc, {"sessionId": sid, "contentHash": f"h{sid}", "calls": 1, "sessionDate": "2026-10-01", **stamped})
    st.save_index()
    httpd = serve(0, store=Store(tmp_path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        conn.request("GET", "/api/stats")
        sessions = json.loads(conn.getresponse().read())["sessions"]
        assert len(sessions) == 2
        assert all(s["facts"]["files"]["acme/api:shared.ts"]["hits"] == 1 for s in sessions)
        assert all(len(s["facts"]["files"]) == facts.MAX_FACT_FILES + 11 for s in sessions)
        assert not any("filesRest" in s["facts"] for s in sessions)
    finally:
        httpd.shutdown()
