"""End-to-end tests for the local tool: build, store, server, export, import."""

from __future__ import annotations

import base64
import contextlib
import gzip
import io
import json
import os
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

import pytest

from agentplay import cli, discover, materialize, pipeline
from agentplay import server as server_mod
from agentplay import store as store_mod
from agentplay.server import serve
from agentplay.store import Store


def _line(**d: Any) -> str:
    return json.dumps(d) + "\n"


def _write_claude_session(home: Path, stem: str, cwd: Path, *, ts_day: str = "2026-09-10", edits_without_read: bool = False) -> Path:
    proj = home / ".claude" / "projects" / "-work-app"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{stem}.jsonl"
    target = f"{cwd}/src/app.py"
    lines = [
        _line(type="user", timestamp=f"{ts_day}T01:00:00Z", cwd=str(cwd), sessionId=stem,
              message={"role": "user", "content": "please fix the SECRET-PROMPT bug"}),
    ]
    seq = 1
    if not edits_without_read:
        lines.append(_line(type="assistant", timestamp=f"{ts_day}T01:00:01Z", cwd=str(cwd), sessionId=stem,
                           message={"id": f"m{seq}", "model": "claude-sonnet-5", "role": "assistant",
                                    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
                                    "content": [{"type": "tool_use", "id": "r1", "name": "Read",
                                                 "input": {"file_path": target}}]}))
        lines.append(_line(type="user", timestamp=f"{ts_day}T01:00:02Z", cwd=str(cwd), sessionId=stem,
                           message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1",
                                                                 "content": "x = 1"}]}))
    for i in range(4):
        tid = f"e{i}"
        lines.append(_line(type="assistant", timestamp=f"{ts_day}T01:00:{10 + 2 * i:02d}Z", cwd=str(cwd), sessionId=stem,
                           message={"id": f"m{10 + i}", "model": "claude-sonnet-5", "role": "assistant",
                                    "usage": {"input_tokens": 10, "output_tokens": 5},
                                    "content": [{"type": "tool_use", "id": tid, "name": "Edit",
                                                 "input": {"file_path": target, "old_string": "a", "new_string": "b"}}]}))
        lines.append(_line(type="user", timestamp=f"{ts_day}T01:00:{11 + 2 * i:02d}Z", cwd=str(cwd), sessionId=stem,
                           message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                                 "content": "ok"}]}))
    f.write_text("".join(lines))
    return f


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Windows expands ~ from USERPROFILE, and VS Code paths from APPDATA.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AGENTPLAY_HOME", str(tmp_path / "sm"))
    for var in ("AGENTPLAY_CLAUDE_HOMES", "AGENTPLAY_CODEX_HOMES", "AGENTPLAY_CURSOR_HOMES"):
        monkeypatch.delenv(var, raising=False)
    repo = tmp_path / "work" / "app"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:acme/app.git\n')
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    return {"home": home, "repo": repo, "sm": tmp_path / "sm"}


def test_cache_root_follows_agentplay_home_set_after_import(tmp_path, monkeypatch):
    """Cache roots resolve per call, not at import.

    Binding these at import time made every cache read fall back to the developer's
    real ``~/.agentplay/cache``, so a test that set ``AGENTPLAY_HOME`` in a fixture
    still ingested their whole session history.
    """
    monkeypatch.setenv("AGENTPLAY_HOME", str(tmp_path / "elsewhere"))
    assert materialize.cache_root() == tmp_path / "elsewhere" / "cache"
    assert store_mod.root() == tmp_path / "elsewhere"
    for derived in (
        materialize.opencode_cache_root(),
        materialize.cursor_store_cache_root(),
        materialize.cursor_composer_cache_root(),
        materialize.kiro_cache_root(),
    ):
        assert derived.parent == tmp_path / "elsewhere" / "cache"


def test_build_is_incremental_and_redacted(env):
    _write_claude_session(env["home"], "11111111-0000-0000-0000-000000000001", env["repo"])
    rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert (rep.found, rep.built, rep.unchanged) == (1, 1, 0)
    again = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert (again.built, again.unchanged) == (0, 1)
    st = Store(env["sm"])
    (row,) = st.rows()
    assert row["sessionId"] == "local:claude-code:11111111-0000-0000-0000-000000000001"
    assert row["sessionTitle"] == "please fix the SECRET-PROMPT bug"  # local index only
    raw = (env["sm"] / "maps" / f"{row['key']}.json").read_text()
    assert "SECRET-PROMPT" not in raw  # the map itself never carries prompt text
    doc = st.get(row["sessionId"])
    assert doc["schema"] == "agentplay/v1"
    assert [f["path"] for f in doc["files"]] == ["src/app.py"]
    assert doc["repos"][0]["remote"] == "acme/app"


def test_incremental_build_picks_up_a_new_workflow_subagent(env):
    stem = "11111111-0000-0000-0000-000000000002"
    f = _write_claude_session(env["home"], stem, env["repo"])
    assert pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"])).built == 1
    run = f.with_suffix("") / "subagents" / "workflows" / "wf_0a1b2c3d-4e5"
    run.mkdir(parents=True)
    agent = run / "agent-a0f1e2d3c4b5a6978.jsonl"
    agent.write_text(
        _line(type="assistant", timestamp="2026-09-10T02:00:00Z", cwd=str(env["repo"]), sessionId=stem,
              isSidechain=True,
              message={"id": "w1", "model": "claude-sonnet-5", "role": "assistant",
                       "usage": {"input_tokens": 10, "output_tokens": 5},
                       "content": [{"type": "tool_use", "id": "w1t", "name": "Read",
                                    "input": {"file_path": f"{env['repo']}/src/app.py"}}]})
        + _line(type="user", timestamp="2026-09-10T02:00:01Z", cwd=str(env["repo"]), sessionId=stem,
                isSidechain=True,
                message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "w1t",
                                                      "content": "x = 1"}]})
    )
    later = f.stat().st_mtime + 60
    os.utime(agent, (later, later))

    again = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert (again.built, again.unchanged) == (1, 0)
    sid = f"local:claude-code:{stem}"
    doc = Store(env["sm"]).get(sid)
    lanes = {a["id"]: a["kind"] for a in doc["agents"]}
    assert [lanes[e["agent"]] for e in doc["events"] if e.get("tool") == "Read"] == ["main", "subagent"]
    cand = discover.Candidate(sid, "claude-code", str(f), later)
    assert discover.source_bytes(cand) == f.stat().st_size + agent.stat().st_size


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_unreadable_workflow_folder_fails_only_its_own_session(env):
    _write_claude_session(env["home"], "11111111-0000-0000-0000-000000000003", env["repo"])
    f = _write_claude_session(env["home"], "11111111-0000-0000-0000-000000000004", env["repo"])
    run = f.with_suffix("") / "subagents" / "workflows" / "wf_0a1b2c3d-4e5"
    run.mkdir(parents=True)
    run.chmod(0)
    try:
        rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    finally:
        run.chmod(0o700)
    assert (rep.found, rep.built, rep.failed) == (2, 1, 1)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_hook_detail_keeps_readable_subagents_beside_an_unreadable_folder(tmp_path):
    from agentplay.hook_detail import _transcripts

    f = tmp_path / "s.jsonl"
    f.write_text("")
    subs = tmp_path / "s" / "subagents"
    (subs / "workflows" / "ok").mkdir(parents=True)
    direct = subs / "agent-a.jsonl"
    nested = subs / "workflows" / "ok" / "agent-b.jsonl"
    direct.write_text("")
    nested.write_text("")
    bad = subs / "workflows" / "locked"
    bad.mkdir()
    bad.chmod(0)
    try:
        assert _transcripts(str(f)) == [str(f), str(direct), str(nested)]
    finally:
        bad.chmod(0o700)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        # A harness block usually *prefixes* a real prompt. Dropping the turn on
        # sight of a leading "<" threw the prompt away with it.
        ("<system-reminder>\nWorktree: /x\n</system-reminder>\n\nset up the staging deploy script",
         "set up the staging deploy script"),
        ("[Image #1] [Image #2] yesterday is image 1", "yesterday is image 1"),
        # Whole turns the harness wrote: no prompt in them at all.
        ("[Request interrupted by user]", None),
        ("[Request interrupted by user for tool use]", None),
        ("<task-notification>\n<task-id>abc</task-id>\n</task-notification>", None),
        ("<command-name>/setup</command-name>\n<command-args>--x</command-args>", None),
        ("Caveat: The messages below were generated", None),
        ("please fix the SECRET-PROMPT bug", "please fix the SECRET-PROMPT bug"),
    ],
)
def test_clean_title_keeps_the_prompt_and_drops_the_harness(raw, want):
    assert discover._clean_title(raw, 60) == want


def test_task_titles_are_served_but_never_written_to_the_map(env):
    """Task labels are prompt text: index and served map yes, map on disk no."""
    _write_claude_session(env["home"], "44444444-0000-0000-0000-000000000004", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    st = Store(env["sm"])
    (row,) = st.rows()

    assert row["taskTitles"] == ["please fix the SECRET-PROMPT bug"]
    raw = (env["sm"] / "maps" / f"{row['key']}.json").read_text()
    assert "SECRET-PROMPT" not in raw          # what `export` writes
    assert st.get(row["sessionId"])["tasks"][0]["name"] is None

    httpd = serve(0, store=st)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(
            f"{base}/api/map-doc?id={urllib.request.quote(row['sessionId'], safe='')}"
        ) as r:
            served = json.loads(r.read())
        assert served["tasks"][0]["name"] == "please fix the SECRET-PROMPT bug"
        with urllib.request.urlopen(f"{base}/api/sessions?pageSize=5") as r:
            page = json.loads(r.read())
        assert "taskTitles" not in page["sessions"][0]  # the list has no use for them
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_no_titles_also_drops_task_titles(env):
    _write_claude_session(env["home"], "55555555-0000-0000-0000-000000000005", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]), titles=False)
    (row,) = Store(env["sm"]).rows()
    assert row["sessionTitle"] is None and row["taskTitles"] is None


def _write_multi_turn_session(home: Path, stem: str, cwd: Path) -> Path:
    """Three real prompts, separated by records that emit no user turn.

    The interruption, the harness-only turn and the system notification each
    look like a "user" record and each produce no ``user`` event, so none of
    them may take a slot in the task-title list.
    """
    proj = home / ".claude" / "projects" / "-work-app"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{stem}.jsonl"
    target = f"{cwd}/src/app.py"
    lines: list[str] = []

    def user(ts: str, content: Any, **extra: Any) -> None:
        lines.append(_line(type="user", timestamp=ts, cwd=str(cwd), sessionId=stem,
                           message={"role": "user", "content": content}, **extra))

    def tool_call(ts: str, mid: str, tid: str) -> None:
        lines.append(_line(type="assistant", timestamp=ts, cwd=str(cwd), sessionId=stem,
                           message={"id": mid, "model": "claude-sonnet-5", "role": "assistant",
                                    "usage": {"input_tokens": 10, "output_tokens": 5},
                                    "content": [{"type": "tool_use", "id": tid, "name": "Read",
                                                 "input": {"file_path": target}}]}))
        lines.append(_line(type="user", timestamp=ts, cwd=str(cwd), sessionId=stem,
                           message={"role": "user", "content": [{"type": "tool_result",
                                                                 "tool_use_id": tid, "content": "x = 1"}]}))

    user("2026-09-10T01:00:00Z", "first prompt about parsing")
    tool_call("2026-09-10T01:00:01Z", "m1", "t1")
    user("2026-09-10T01:00:03Z", "[Request interrupted by user]")
    user("2026-09-10T01:00:04Z", "<task-notification>\n<task-id>abc</task-id>\n</task-notification>")
    user("2026-09-10T01:00:05Z", "meta chatter", isMeta=True)
    user("2026-09-10T01:00:06Z", "second prompt about caching")
    tool_call("2026-09-10T01:00:07Z", "m2", "t2")
    user("2026-09-10T01:00:09Z", "[SYSTEM NOTIFICATION - NOT USER INPUT] a task finished")
    user("2026-09-10T01:00:10Z", "third prompt about the viewer")
    tool_call("2026-09-10T01:00:11Z", "m3", "t3")
    f.write_text("".join(lines))
    return f


def test_task_titles_line_up_with_the_user_turns_the_builder_emits(env):
    """One title per emitted user turn, so `firstTurn` indexes the right label.

    Interruptions, harness-only turns and meta records are "user" records that
    emit no `user` event. Counting them here put six entries against three
    turns, which blanked task 2 and gave task 3 task 2's prompt.
    """
    _write_multi_turn_session(env["home"], "77777777-0000-0000-0000-000000000007", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    st = Store(env["sm"])
    (row,) = st.rows()
    doc = st.get(row["sessionId"])

    assert sum(1 for e in doc["events"] if e["kind"] == "user") == 3
    assert row["taskTitles"] == [
        "first prompt about parsing",
        "second prompt about caching",
        "third prompt about the viewer",
    ]
    assert [t["firstTurn"] for t in doc["tasks"]] == [1, 2, 3]
    named = server_mod._with_task_titles(doc, row)
    assert [t["name"] for t in named["tasks"]] == [
        "first prompt about parsing",
        "second prompt about caching",
        "third prompt about the viewer",
    ]


_MULTI_TURN_TITLES = [
    "first prompt about parsing",
    "second prompt about caching",
    "third prompt about the viewer",
]


@pytest.mark.parametrize("unfilled", ["absent", "null"])
def test_unchanged_session_gains_task_titles_without_a_rebuild(env, unfilled):
    """A row with no task titles gets them on the next incremental run.

    Two shapes mean "not filled in" and both have to be refilled: a row written
    before task titles existed carries no key at all, and a `--no-titles` run
    leaves the key set to None. Reading `None` as "already done" left every
    session a `--no-titles` run had touched generically labelled until its
    transcript changed or the user forced a `--rebuild`.
    """
    _write_multi_turn_session(env["home"], "88888888-0000-0000-0000-000000000008", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))

    old = Store(env["sm"])
    (row,) = old.rows()
    sid, content_hash = row["sessionId"], row["contentHash"]
    if unfilled == "absent":
        del old.index[sid]["taskTitles"]      # the shape an older version wrote
    else:
        old.index[sid]["taskTitles"] = None   # the shape a `--no-titles` run writes
    old.save_index()

    rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert (rep.built, rep.unchanged) == (0, 1)  # the map itself was not rebuilt

    (row,) = Store(env["sm"]).rows()
    assert row["contentHash"] == content_hash
    assert row["taskTitles"] == _MULTI_TURN_TITLES


def test_a_row_that_already_has_titles_is_not_re_read(env, monkeypatch):
    """Filled is filled: a default run neither re-extracts nor overwrites.

    Only `None`-or-absent counts as unfilled, so a row carrying real labels is
    left exactly as it is rather than being rebuilt from the transcript on
    every incremental run.
    """
    _write_multi_turn_session(env["home"], "99999999-0000-0000-0000-000000000009", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    st = Store(env["sm"])
    (row,) = st.rows()
    st.index[row["sessionId"]]["taskTitles"] = ["kept"]
    st.index[row["sessionId"]]["sessionTitle"] = "kept too"
    st.save_index()

    def _boom(*_a, **_k):
        raise AssertionError("a filled row must not be read from the transcript again")

    monkeypatch.setattr(discover, "local_title", _boom)
    monkeypatch.setattr(discover, "local_task_titles", _boom)

    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    (row,) = Store(env["sm"]).rows()
    assert row["taskTitles"] == ["kept"]
    assert row["sessionTitle"] == "kept too"


def test_no_titles_clears_the_prompt_text_an_earlier_run_stored(env):
    """The opt-out reaches data already on disk, not only future sessions.

    `--no-titles` is a per-run switch. A default build writes the first prompt
    and every task label into `index.json`; a later `--no-titles` run has to
    take them back out again, or the switch never touches the prompt text it
    exists for and local map responses keep serving the labels.
    """
    _write_multi_turn_session(env["home"], "bbbbbbbb-0000-0000-0000-00000000000b", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    index_path = env["sm"] / "index.json"
    stored = index_path.read_text(encoding="utf-8")
    assert all(t in stored for t in _MULTI_TURN_TITLES)   # the state being opted out of

    rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]), titles=False)
    assert (rep.built, rep.unchanged) == (0, 1)           # cleared in place, nothing rebuilt

    # On disk, not just in what the API chooses to serve: the finding is that
    # the prompt text is still in the index.
    stored = index_path.read_text(encoding="utf-8")
    for fragment in (*_MULTI_TURN_TITLES, "prompt about", "parsing", "caching", "viewer"):
        assert fragment not in stored

    st = Store(env["sm"])
    (row,) = st.rows()
    assert row["sessionTitle"] is None and row["taskTitles"] is None
    doc = st.get(row["sessionId"])
    assert [t["name"] for t in server_mod._with_task_titles(doc, row)["tasks"]] == [None, None, None]


def test_titles_come_back_on_the_next_default_run(env):
    """Clearing is this run's doing, not a preference the next run inherits."""
    _write_multi_turn_session(env["home"], "cccccccc-0000-0000-0000-00000000000c", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]), titles=False)
    (row,) = Store(env["sm"]).rows()
    assert row["sessionTitle"] is None and row["taskTitles"] is None

    rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert (rep.built, rep.unchanged) == (0, 1)
    (row,) = Store(env["sm"]).rows()
    assert row["sessionTitle"] == "first prompt about parsing"
    assert row["taskTitles"] == _MULTI_TURN_TITLES


@pytest.mark.parametrize(
    ("peer", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.0.1.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),   # an IPv6 socket serving an IPv4 local client
        ("fe80::1%eth0", False),
        ("192.168.1.20", False),
        ("10.0.0.7", False),
        ("", False),                  # unparseable fails closed
    ],
)
def test_is_loopback_peer(peer, loopback):
    assert server_mod._is_loopback((peer, 51000)) is loopback


def test_task_titles_are_withheld_from_a_non_loopback_client(env):
    """Titles are prompt text and the API is unauthenticated: local peers only.

    `serve(host=...)` accepts a non-loopback bind, and the stored map carries no
    prompt text, so the merge is what a network client must not get.
    """
    _write_claude_session(env["home"], "aaaaaaaa-0000-0000-0000-00000000000a", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    st = Store(env["sm"])
    (row,) = st.rows()
    assert row["taskTitles"] == ["please fix the SECRET-PROMPT bug"]

    httpd = serve(0, store=st)
    base_handler = httpd.RequestHandlerClass

    class RemoteHandler(base_handler):  # type: ignore[valid-type, misc]
        def setup(self) -> None:
            super().setup()
            self.client_address = ("192.168.1.20", 51000)

    httpd.RequestHandlerClass = RemoteHandler
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/api/map-doc?id={urllib.request.quote(row['sessionId'], safe='')}")
        assert e.value.code == 403
        assert "SECRET-PROMPT" not in e.value.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("headers", [{"Host": "evil.example:7317"}, {"Origin": "https://evil.example"}])
def test_task_titles_are_withheld_from_a_rebinding_page(env, headers):
    """A DNS-rebinding page reaches 127.0.0.1 from a loopback peer under its own
    Host. Prompt text must still only go to this machine's viewer."""
    _write_claude_session(env["home"], "bbbbbbbb-0000-0000-0000-00000000000b", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    st = Store(env["sm"])
    (row,) = st.rows()
    httpd = serve(0, store=st)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/map-doc?id={urllib.request.quote(row['sessionId'], safe='')}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers=headers))
        assert e.value.code == 403
        assert "SECRET-PROMPT" not in e.value.read().decode()
        with urllib.request.urlopen(url) as r:
            assert "SECRET-PROMPT" in r.read().decode()  # the real viewer still gets them
    finally:
        httpd.shutdown()
        httpd.server_close()

@pytest.mark.parametrize("path", ["/api/status", "/api/sessions", "/"])
def test_a_rebinding_page_reads_nothing(env, path):
    """Session titles are prompt text and maps name private repos, so a rebinding
    Host gets 403 on every read, not just the routes that merge prompt labels."""
    _write_claude_session(env["home"], "cccccccc-0000-0000-0000-00000000000c", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    httpd = serve(0, store=Store(env["sm"]))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}{path}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers={"Host": "evil.example:7317"}))
        assert e.value.code == 403
        assert "SECRET-PROMPT" not in e.value.read().decode()
        if path != "/":  # no built viewer in the test tree, so only the API is checked locally
            with urllib.request.urlopen(url) as r:
                assert r.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_server_picks_up_sessions_built_while_it_runs(env):
    """`view` serves first and scans in a background thread, so a Store held for
    the server's lifetime has to notice an index written underneath it."""
    _write_claude_session(env["home"], "66666666-0000-0000-0000-000000000006", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))

    served = Store(env["sm"])
    httpd = serve(0, store=served)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def count() -> int:
        with urllib.request.urlopen(f"{base}/api/status") as r:
            return json.loads(r.read())["mapCount"]

    try:
        assert count() == 1
        # A second session appears while the server is up, via its own Store —
        # exactly what the background scan does.
        _write_claude_session(env["home"], "77777777-0000-0000-0000-000000000007", env["repo"])
        pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
        assert count() == 2
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_status_reports_a_background_scan(env):
    """The viewer tells "no sessions yet" from "no sessions" by this flag."""
    scanning = threading.Event()
    scanning.set()
    httpd = serve(0, store=Store(env["sm"]), scanning=scanning)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def status() -> dict[str, Any]:
        with urllib.request.urlopen(f"{base}/api/status") as r:
            return json.loads(r.read())

    try:
        assert status()["scanning"] is True and status()["mapCount"] == 0
        scanning.clear()
        assert status()["scanning"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_long_build_saves_the_index_as_it_goes(env, monkeypatch):
    """A first scan can take minutes; the viewer should not sit on an empty list
    until it ends, so the index is written mid-run too."""
    for n in range(3):
        _write_claude_session(env["home"], f"9999999{n}-0000-0000-0000-00000000000{n}", env["repo"])
    monkeypatch.setattr(pipeline, "SAVE_EVERY_S", 0.0)
    saves = 0
    real = Store.save_index

    def counting(self: Store) -> None:
        nonlocal saves
        saves += 1
        real(self)

    monkeypatch.setattr(Store, "save_index", counting)
    rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert rep.built == 3
    # One after each of the first two maps, plus the final save.
    assert saves == 3
    assert len(Store(env["sm"]).index) == 3


def test_reload_if_changed_is_opt_in_because_it_discards_pending_writes(env):
    """Why the reload is an explicit call and not a check inside `index`.

    `build_all` does many `put`s before a single `save_index`. A reload between
    them drops every pending row, so only the server — which never writes — may
    ask for one.
    """
    _write_claude_session(env["home"], "88888888-0000-0000-0000-000000000008", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))

    st = Store(env["sm"])
    st.index["pending"] = {"key": "k", "sessionId": "pending"}
    # Nothing moved on disk: the pending row survives.
    assert st.reload_if_changed() is False
    assert "pending" in st.index

    # Someone else writes the index. Asking to reload now is what would cost us
    # the pending row -- correct for the server, which is why a writer must not.
    other = Store(env["sm"])
    other.index["elsewhere"] = {"key": "k2", "sessionId": "elsewhere"}
    other.save_index()
    assert st.reload_if_changed() is True
    assert "pending" not in st.index
    assert "elsewhere" in st.index


def test_server_endpoints(env):
    _write_claude_session(env["home"], "22222222-0000-0000-0000-000000000002", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    httpd = serve(0, store=Store(env["sm"]))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path: str) -> Any:
        with urllib.request.urlopen(base + path) as r:
            return json.loads(r.read())

    try:
        status = get("/api/status")
        assert status == {"success": True, "available": True, "mapCount": 1, "latestSessionDate": "2026-09-10",
                          "scanning": False}
        page = get("/api/sessions?pageSize=10")
        assert page["hasMore"] is False and len(page["sessions"]) == 1
        row = page["sessions"][0]
        assert "sourcePath" not in row and row["agentTool"] == "claude-code"
        assert get("/api/sessions?search=nomatch")["sessions"] == []
        sid = row["sessionId"]
        batch = get("/api/map?session_id=" + urllib.request.quote(sid, safe="") + "&session_id=missing")
        assert batch["maps"][1] == {"sessionId": "missing", "status": "not_found"}
        doc = get(batch["maps"][0]["url"])
        assert doc["session"]["id"] == sid
        for gone in ("/api/nope",):
            with pytest.raises(urllib.error.HTTPError) as err:
                get(gone)
            assert err.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_server_hides_zero_call_sessions(env):
    st = Store(env["sm"])
    st.index["oneshot"] = {"key": "k", "sessionId": "oneshot", "startedAt": "2026-09-11T00:00:00Z", "calls": 0}
    st.index["real"] = {"key": "k2", "sessionId": "real", "startedAt": "2026-09-10T00:00:00Z", "calls": 3}
    httpd = serve(0, store=st)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/sessions") as r:
            assert [s["sessionId"] for s in json.loads(r.read())["sessions"]] == ["real"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_build_json_and_export(env, capsys, monkeypatch):
    _write_claude_session(env["home"], "33333333-0000-0000-0000-000000000003", env["repo"])
    assert cli.main(["build", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert (rep["found"], rep["built"]) == (1, 1)
    monkeypatch.chdir(env["repo"])
    assert cli.main(["export", "000000000003", "-o", "s.json.gz"]) == 0
    assert (env["repo"] / "s.json.gz").stat().st_size > 100


def test_hashed_without_key_warns_and_export_drops_the_dirs_only_hint(env, capsys, monkeypatch):
    _write_claude_session(env["home"], "33333333-0000-0000-0000-000000000003", env["repo"])
    monkeypatch.delenv("AGENTPLAY_HMAC_KEY", raising=False)
    monkeypatch.chdir(env["repo"])
    assert cli.main(["build", "--path-mode", "hashed"]) == 0
    err = capsys.readouterr().err
    assert "needs AGENTPLAY_HMAC_KEY" in err and "dirs_only" in err
    assert cli.main(["export", "000000000003", "-o", "s.json"]) == 0
    out = capsys.readouterr().out
    assert "path mode: dirs_only" in out and "Rebuild with" not in out
    assert "contains no prompt or output text" in out
    # A relative map still gets the hint, and a keyed hashed build no warning.
    assert cli.main(["build", "--rebuild"]) == 0
    capsys.readouterr()
    assert cli.main(["export", "000000000003", "-o", "s.json"]) == 0
    assert "Rebuild with --path-mode dirs_only" in capsys.readouterr().out
    monkeypatch.setenv("AGENTPLAY_HMAC_KEY", "test-key")
    assert cli.main(["build", "--rebuild", "--path-mode", "hashed"]) == 0
    assert "AGENTPLAY_HMAC_KEY" not in capsys.readouterr().err


def test_local_path_remote_is_recorded_without_a_remote(env, capsys, monkeypatch):
    for url in (
        "/Users/ada/someone/app.git",
        "~/someone/app",
        "file:///Users/ada/someone/app",
        "C:\\Users\\someone\\app",
        "../someone/app",
    ):
        (env["repo"] / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
        _write_claude_session(env["home"], "33333333-0000-0000-0000-000000000003", env["repo"])
        for mode in ("relative", "dirs_only"):
            pipeline.build_all(agents={"claude-code"}, rebuild=True, path_mode=mode, store=Store(env["sm"]))
            (doc,) = list(Store(env["sm"]).docs())
            assert [(r["name"], r["remote"]) for r in doc["repos"]] == [("app", None)], (url, mode)
            assert "someone" not in json.dumps(doc)
    monkeypatch.chdir(env["repo"])
    assert cli.main(["export", "000000000003", "-o", "s.json"]) == 0
    out = capsys.readouterr().out
    assert "repo names (app)" in out and "someone" not in out


def test_built_maps_validate_against_published_schema(env):
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parents[2] / "schema" / "agentplay-v1.schema.json"
    if not schema_path.is_file():
        pytest.skip("schema/ is only in a repo checkout, not the sdist")
    schema = json.loads(schema_path.read_text())
    _write_claude_session(env["home"], "44444444-0000-0000-0000-000000000004", env["repo"])
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    (doc,) = list(Store(env["sm"]).docs())
    jsonschema.validate(doc, schema)


def test_cli_import_roundtrip(env, capsys, monkeypatch, tmp_path):
    _write_claude_session(env["home"], "55555555-0000-0000-0000-000000000005", env["repo"])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["build"]) == 0
    assert cli.main(["export", "000000000005", "-o", "m.json.gz"]) == 0
    other = tmp_path / "other"
    monkeypatch.setenv("AGENTPLAY_HOME", str(other))
    assert cli.main(["import", "m.json.gz", "--title", "from a teammate"]) == 0
    (row,) = Store(other).rows()
    assert row["sessionTitle"] == "from a teammate" and row["imported"] == "m.json.gz"
    (tmp_path / "bad.json").write_text('{"schema": "other/v9"}')
    assert cli.main(["import", "bad.json"]) == 1


# ---------------------------------------------------------------- pasted images
def _png(rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb))) + chunk(b"IEND", b""))


_PNG_A, _PNG_B, _PNG_C, _PNG_D = (_png(c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (9, 9, 9)))
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _img(data: bytes, media_type: str = "image/png", *, extra: str = "") -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(data).decode() + extra}}


def _write_image_session(home: Path, stem: str, cwd: Path, *, sentinel: str = "") -> Path:
    """A transcript whose prompts carry pasted images, plus every record that must not take a turn.

    The builder numbers a user turn from `user_turn_text` alone, so the
    `isSidechain` record and the resumed-session duplicate each take a slot
    (the builder neither dedupes nor looks at `isSidechain`); the meta,
    interruption and harness records do not.
    """
    proj = home / ".claude" / "projects" / "-work-app"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{stem}.jsonl"

    def user(sec: int, content: Any, **extra: Any) -> str:
        return _line(type="user", timestamp=f"2026-09-10T01:00:{sec:02d}Z", cwd=str(cwd), sessionId=stem,
                     message={"role": "user", "content": content}, **extra)

    prompt = [{"type": "text", "text": "[Image #2] [Image #3] look at the red and green"},
              _img(_PNG_A, extra=sentinel), _img(_PNG_B)]
    f.write_text("".join([
        user(0, prompt, imagePasteIds=[2, 3]),
        user(1, [{"type": "text", "text": "[Image: source: /private/tmp/x/2.png]"}], isMeta=True),
        _line(type="assistant", timestamp="2026-09-10T01:00:02Z", cwd=str(cwd), sessionId=stem,
              message={"id": "m1", "model": "claude-sonnet-5", "role": "assistant",
                       "usage": {"input_tokens": 10, "output_tokens": 5}, "content": [{"type": "text", "text": "ok"}]}),
        user(3, [{"type": "text", "text": "sidechain note"}, _img(_PNG_C)], isSidechain=True),
        user(4, [{"type": "text", "text": "[Request interrupted by user]"}]),
        user(5, [{"type": "text", "text": "compare the jpeg screenshot"}, _img(_JPEG, "image/jpeg")], imagePasteIds=[4]),
        # A resumed session replays the earlier prompt with its original timestamp. The builder
        # numbers turns in timestamp order, so the replay is turn 2 even though it sits later in
        # the file; numbering in file order would point every turn after it at the wrong prompt.
        user(0, prompt, imagePasteIds=[2, 3]),
        user(7, "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"),
        user(8, "a closing prompt with no image"),
    ]))
    return f


_IMAGE_TURNS = {1: [_PNG_A, _PNG_B], 2: [_PNG_A, _PNG_B], 3: [_PNG_C], 4: [_JPEG]}


class _Served:
    def __init__(self, httpd: Any, store: Store, row: dict[str, Any], transcript: Path) -> None:
        self.httpd, self.store, self.row, self.transcript = httpd, store, row, transcript
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.sid = row["sessionId"]

    def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any, bytes]:
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def images(self, headers: dict[str, str] | None = None) -> tuple[int, Any, bytes]:
        return self.get(f"/api/prompt-images?id={urllib.request.quote(self.sid, safe='')}", headers)

    def hook(self, uuid: Any, headers: dict[str, str] | None = None) -> tuple[int, Any, bytes]:
        return self.get(f"/api/hook-detail?id={urllib.request.quote(self.sid, safe='')}&uuid={uuid}", headers)

    def image(self, turn: Any, i: Any, headers: dict[str, str] | None = None) -> tuple[int, Any, bytes]:
        return self.get(f"/api/prompt-image?id={urllib.request.quote(self.sid, safe='')}&turn={turn}&i={i}", headers)


@contextlib.contextmanager
def _serve_image_session(env, *, remote: bool = False, sentinel: str = ""):
    stem = "cccccccc-0000-0000-0000-00000000000c"
    transcript = _write_image_session(env["home"], stem, env["repo"], sentinel=sentinel)
    with _serve_built(env, transcript, remote=remote) as served:
        yield served

@contextlib.contextmanager
def _serve_built(env, transcript: Path, *, remote: bool = False):
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    st = Store(env["sm"])
    (row,) = st.rows()
    httpd = serve(0, store=st)
    if remote:
        base_handler = httpd.RequestHandlerClass

        class RemoteHandler(base_handler):  # type: ignore[valid-type, misc]
            def setup(self) -> None:
                super().setup()
                self.client_address = ("192.168.1.20", 51000)

        httpd.RequestHandlerClass = RemoteHandler
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield _Served(httpd, st, row, transcript)
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def pasted(env):
    with _serve_image_session(env) as served:
        yield served


def test_prompt_image_turns_line_up_with_the_builder_and_task_titles(pasted):
    """Turn k here is `taskTitles[k - 1]` and the k-th `user` event, whatever else is in the file."""
    doc = pasted.store.get(pasted.sid)
    user_events = [e for e in doc["events"] if e["kind"] == "user"]
    titles = pasted.row["taskTitles"]
    assert len(user_events) == len(titles) == 5  # sidechain and replayed prompts count; meta/interrupt/harness do not
    assert titles[2] == "sidechain note" and titles[3] == "compare the jpeg screenshot"

    status, headers, body = pasted.images()
    assert status == 200 and headers["Cache-Control"] == "no-store"
    turns = json.loads(body)["turns"]
    assert json.loads(body)["success"] is True
    assert [t["turn"] for t in turns] == [1, 2, 3, 4]
    for t in turns:  # the builder's own per-turn image count agrees
        assert len(t["images"]) == user_events[t["turn"] - 1]["images"]
    assert [im["pasteId"] for im in turns[0]["images"]] == [2, 3]
    assert [im["pasteId"] for im in turns[1]["images"]] == [2, 3]  # the replay, numbered by timestamp
    assert [im["pasteId"] for im in turns[2]["images"]] == [None]  # no imagePasteIds on that record
    assert turns[3]["images"] == [{"i": 0, "pasteId": 4, "mediaType": "image/jpeg", "available": True}]
    assert all(im["available"] for t in turns for im in t["images"])
    named = server_mod._with_task_titles(doc, pasted.row)
    assert all(t["name"] == titles[t["firstTurn"] - 1] for t in named["tasks"])
    assert {t["firstTurn"] for t in doc["tasks"]} <= {t["turn"] for t in turns} | {5}


def test_prompt_image_bytes_and_headers(pasted):
    for turn, images in _IMAGE_TURNS.items():
        for i, want in enumerate(images):
            status, headers, body = pasted.image(turn, i)
            assert status == 200 and body == want
            assert headers["Content-Type"] == ("image/jpeg" if want == _JPEG else "image/png")
            assert headers["Content-Length"] == str(len(want))
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
            assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
            assert headers["Cache-Control"] == "private, max-age=3600"


@pytest.mark.parametrize("via", ["remote", "host", "origin"])
def test_prompt_images_are_withheld_from_anyone_but_this_machine(env, via):
    """Images are the user's data: a network peer or a DNS-rebinding page gets 403 and nothing else."""
    headers = {"host": {"Host": "evil.example:7317"}, "origin": {"Origin": "https://evil.example"}}.get(via)
    with _serve_image_session(env, remote=via == "remote") as s:
        for status, _, body in (s.images(headers), s.image(1, 0, headers)):
            assert status == 403
            assert json.loads(body) == {"success": False, "code": "forbidden",
                                        "error": "Only the local viewer can read this."}
        assert s.image(1, 0)[0] == (403 if via == "remote" else 200)  # a local viewer is unaffected


@pytest.mark.parametrize(
    "query",
    [
        "", "id=", "turn=1&i=0",  # missing id
    ],
)
def test_prompt_images_need_a_session_id(pasted, query):
    for path in ("/api/prompt-images", "/api/prompt-image"):
        status, _, body = pasted.get(f"{path}?{query}")
        assert status == 400 and json.loads(body)["code"] == "invalid_input"


@pytest.mark.parametrize(
    ("turn", "i"),
    [("", "0"), ("1", ""), ("abc", "0"), ("1", "x"), ("-1", "0"), ("1", "-1"), ("1.5", "0"), ("+1", "0"), ("1", " 0"),
     ("\u0663", "0"), ("9" * 5000, "0")],
)
def test_prompt_image_rejects_malformed_turn_or_index(pasted, turn, i):
    status, _, body = pasted.get(
        f"/api/prompt-image?id={urllib.request.quote(pasted.sid, safe='')}&turn={urllib.request.quote(turn)}&i={urllib.request.quote(i)}"
    )
    assert status == 400 and json.loads(body)["code"] == "invalid_input"
    assert pasted.get(f"/api/prompt-image?id={urllib.request.quote(pasted.sid, safe='')}")[0] == 400


@pytest.mark.parametrize(("turn", "i"), [(0, 0), (5, 0), (6, 0), (1, 2), (3, 1), (99, 99)])
def test_prompt_image_unknown_turn_or_index_is_404(pasted, turn, i):
    status, _, body = pasted.image(turn, i)
    assert status == 404 and json.loads(body)["code"] == "not_found"
    assert str(pasted.transcript) not in body.decode()


def test_prompt_images_unknown_session_is_404(pasted):
    for path in ("/api/prompt-images?id=nope", "/api/prompt-image?id=nope&turn=1&i=0"):
        status, _, body = pasted.get(path)
        assert status == 404 and json.loads(body)["code"] == "not_found"


@pytest.mark.parametrize(
    "change",
    [{"imported": "m.json.gz"}, {"agentTool": "codex"}, {"agentTool": None}, {"sourcePath": None},
     {"sourcePath": "/etc/passwd"}, {"sourcePath": 7}],
)
def test_prompt_images_only_serve_a_local_claude_code_transcript(pasted, change):
    pasted.store.index[pasted.sid].update(change)
    for status, _, body in (pasted.images(), pasted.image(1, 0)):
        assert status == 404 and json.loads(body)["code"] == "not_found"
        assert str(pasted.transcript) not in body.decode()


def test_prompt_images_deleted_transcript_is_410(pasted):
    pasted.transcript.unlink()
    for status, _, body in (pasted.images(), pasted.image(1, 0)):
        assert status == 410 and json.loads(body)["code"] == "gone"
        assert str(pasted.transcript) not in body.decode() and str(pasted.transcript.parent) not in body.decode()


def test_prompt_image_422_for_a_bad_block_never_leaks_the_path(env):
    stem = "dddddddd-0000-0000-0000-00000000000d"
    f = _write_image_session(env["home"], stem, env["repo"])
    f.write_text(f.read_text().replace('"image/jpeg"', '"image/svg+xml"'))
    pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    st = Store(env["sm"])
    (row,) = st.rows()
    httpd = serve(0, store=st)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        s = _Served(httpd, st, row, f)
        assert json.loads(s.images()[2])["turns"][3]["images"][0]["available"] is False
        status, _, body = s.image(4, 0)
        assert status == 422 and json.loads(body)["code"] == "invalid_image"
        assert str(f) not in body.decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_pasted_image_data_never_reaches_a_map_the_index_or_an_export(env, monkeypatch, tmp_path):
    """Only the `images` count survives a build; the bytes are read from the transcript on demand."""
    sentinel = "SENTINELIMAGEBYTES"
    with _serve_image_session(env, sentinel=sentinel) as s:
        assert sentinel in s.transcript.read_text()
        map_raw = (env["sm"] / "maps" / f"{s.row['key']}.json").read_text()
        index_raw = (env["sm"] / "index.json").read_text()
        monkeypatch.chdir(tmp_path)
        assert cli.main(["export", s.sid[-12:], "-o", "m.json.gz"]) == 0
        export_raw = gzip.decompress((tmp_path / "m.json.gz").read_bytes()).decode()
    for raw in (map_raw, index_raw, export_raw):
        assert sentinel not in raw
        assert base64.b64encode(_PNG_B).decode() not in raw
    assert [e["images"] for e in json.loads(map_raw)["events"] if e["kind"] == "user"] == [2, 2, 1, 1, 0]


def test_piped_output_in_a_legacy_code_page_does_not_crash(env):
    """Windows pipes fall back to cp1252, which has no `●` or `→`.

    `PYTHONIOENCODING=cp1252` reproduces that on any OS: `agents` crashed
    before, and so did `view` before it ever served.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "agentplay", "agents"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "●".encode() in proc.stdout or "○".encode() in proc.stdout


def test_utf8_stdio_reencodes_only_a_non_utf8_stream(monkeypatch):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    cli._utf8_stdio()
    assert sys.stdout.encoding == "utf-8"
    print("● → 日本語")
    sys.stdout.flush()
    assert stream.buffer.getvalue().decode("utf-8").startswith("● → 日本語")

    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", utf8)
    cli._utf8_stdio()
    assert utf8.errors == "strict"


# ---------------------------------------------------------------- hook failure details

_HOOK_BLOCK, _HOOK_WARN, _HOOK_STOP = (f"22222222-0000-0000-0000-00000000000{n}" for n in (1, 2, 3))
_HOOK_TEXT = ("SECRET-HOOK-MSG", "SECRET-HOOK-CMD", "SECRET-HOOK-STDERR", "SECRET-HOOK-WARN-CMD")

def _write_hook_session(home: Path, stem: str, cwd: Path) -> Path:
    proj = home / ".claude" / "projects" / "-work-app"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{stem}.jsonl"
    day = "2026-09-10"
    hook = {"type": "attachment", "cwd": str(cwd), "sessionId": stem}
    f.write_text("".join([
        _line(type="user", timestamp=f"{day}T01:00:00Z", cwd=str(cwd), sessionId=stem, uuid="33333333-0000-0000-0000-000000000001",
              message={"role": "user", "content": "go"}),
        _line(type="assistant", timestamp=f"{day}T01:00:01Z", cwd=str(cwd), sessionId=stem,
              message={"id": "m1", "model": "claude-sonnet-5", "role": "assistant",
                       "usage": {"input_tokens": 1, "output_tokens": 1},
                       "content": [{"type": "tool_use", "id": "r1", "name": "Read",
                                    "input": {"file_path": f"{cwd}/src/app.py"}}]}),
        _line(type="user", timestamp=f"{day}T01:00:02Z", cwd=str(cwd), sessionId=stem,
              message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": "x = 1"}]}),
        _line(**hook, timestamp=f"{day}T01:00:03Z", uuid=_HOOK_BLOCK, attachment={
            "type": "hook_blocking_error", "hookName": "PostToolUse:Read", "hookEvent": "PostToolUse", "toolUseID": "r1",
            "blockingError": {"blockingError": "SECRET-HOOK-MSG" + "x" * 5000, "command": "SECRET-HOOK-CMD"}}),
        _line(**hook, timestamp=f"{day}T01:00:04Z", uuid=_HOOK_WARN, attachment={
            "type": "hook_non_blocking_error", "hookName": "PostToolUse:Read", "hookEvent": "PostToolUse", "toolUseID": "r1",
            "command": "SECRET-HOOK-WARN-CMD", "stderr": "SECRET-HOOK-STDERR", "stdout": "", "exitCode": 1}),
        _line(**hook, timestamp=f"{day}T01:00:05Z", uuid=_HOOK_STOP, attachment={
            "type": "hook_blocking_error", "hookName": "Stop", "hookEvent": "Stop", "toolUseID": "turn-1",
            "blockingError": {"blockingError": "stop msg", "command": "stop cmd"}}),
    ]))
    return f

@contextlib.contextmanager
def _serve_hook_session(env, *, remote: bool = False):
    transcript = _write_hook_session(env["home"], "eeeeeeee-0000-0000-0000-00000000000e", env["repo"])
    with _serve_built(env, transcript, remote=remote) as served:
        yield served

@pytest.fixture()
def hooked(env):
    with _serve_hook_session(env) as served:
        yield served

def test_hook_detail_reads_message_and_command_from_the_transcript(hooked):
    status, _, body = hooked.hook(_HOOK_BLOCK)
    got = json.loads(body)
    assert status == 200 and got["success"] is True and set(got) == {"success", "message", "command"}
    assert got["message"].startswith("SECRET-HOOK-MSG") and len(got["message"]) == 4000  # capped
    assert got["command"] == "SECRET-HOOK-CMD"
    # A non-blocking failure's message is its stderr.
    assert json.loads(hooked.hook(_HOOK_WARN)[2]) == {
        "success": True, "message": "SECRET-HOOK-STDERR", "command": "SECRET-HOOK-WARN-CMD"}

def test_hook_detail_matches_the_map_events(hooked):
    evs = [e for e in hooked.store.get(hooked.sid)["events"] if e.get("source") == "hook"]
    assert {e["srcUuid"] for e in evs} == {_HOOK_BLOCK, _HOOK_WARN, _HOOK_STOP}
    read = next(e for e in hooked.store.get(hooked.sid)["events"] if e["kind"] == "read")
    assert {e["srcUuid"]: e.get("ref") for e in evs} == {
        _HOOK_BLOCK: read["seq"], _HOOK_WARN: read["seq"], _HOOK_STOP: None}
    assert all(hooked.hook(e["srcUuid"])[0] == 200 for e in evs)

@pytest.mark.parametrize("via", ["remote", "host", "origin"])
def test_hook_detail_is_withheld_from_anyone_but_this_machine(env, via):
    headers = {"host": {"Host": "evil.example:7317"}, "origin": {"Origin": "https://evil.example"}}.get(via)
    with _serve_hook_session(env, remote=via == "remote") as s:
        status, _, body = s.hook(_HOOK_BLOCK, headers)
        assert status == 403
        assert json.loads(body) == {"success": False, "code": "forbidden",
                                    "error": "Only the local viewer can read this."}
        assert not any(t in body.decode() for t in _HOOK_TEXT)
        assert s.hook(_HOOK_BLOCK)[0] == (403 if via == "remote" else 200)

@pytest.mark.parametrize("uuid", ["", "a%20b", "..%2Fx", "x" * 65, "%00"])
def test_hook_detail_rejects_a_malformed_uuid(hooked, uuid):
    status, _, body = hooked.hook(uuid)
    assert status == 400 and json.loads(body)["code"] == "invalid_input"

def test_hook_detail_needs_a_session_id(hooked):
    assert hooked.get(f"/api/hook-detail?uuid={_HOOK_BLOCK}")[0] == 400

@pytest.mark.parametrize("uuid", ["33333333-0000-0000-0000-000000000001", "44444444-0000-0000-0000-000000000009"])
def test_hook_detail_unknown_uuid_or_a_non_hook_record_is_404(hooked, uuid):
    status, _, body = hooked.hook(uuid)  # the first is a real record, but a user turn
    assert status == 404 and json.loads(body)["code"] == "not_found"
    assert str(hooked.transcript) not in body.decode()

@pytest.mark.parametrize("change", [{"imported": "m.json.gz"}, {"agentTool": "codex"}, {"sourcePath": None},
                                    {"sourcePath": "/etc/passwd"}])
def test_hook_detail_only_serves_a_local_claude_code_transcript(hooked, change):
    hooked.store.index[hooked.sid].update(change)
    status, _, body = hooked.hook(_HOOK_BLOCK)
    assert status == 404 and json.loads(body)["code"] == "not_found"

def test_hook_detail_deleted_transcript_is_410_without_a_path(hooked):
    hooked.transcript.unlink()
    status, _, body = hooked.hook(_HOOK_BLOCK)
    assert status == 410 and json.loads(body)["code"] == "gone"
    assert str(hooked.transcript.parent) not in body.decode()

def test_hook_detail_follows_a_transcript_that_changes(hooked):
    assert hooked.hook(_HOOK_STOP)[0] == 200
    lines = hooked.transcript.read_text().splitlines(keepends=True)
    hooked.transcript.write_text("".join(lines[:-1]))  # drop the Stop record: the cached scan must not serve it
    assert hooked.hook(_HOOK_STOP)[0] == 404

def test_hook_text_never_reaches_a_map_the_index_or_an_export(env, monkeypatch, tmp_path):
    """Only the hook name, its event and the opaque record id survive a build."""
    with _serve_hook_session(env) as s:
        assert all(t in s.transcript.read_text() for t in _HOOK_TEXT)
        map_raw = (env["sm"] / "maps" / f"{s.row['key']}.json").read_text()
        index_raw = (env["sm"] / "index.json").read_text()
        monkeypatch.chdir(tmp_path)
        assert cli.main(["export", s.sid[-12:], "-o", "m.json.gz"]) == 0
        export_raw = gzip.decompress((tmp_path / "m.json.gz").read_bytes()).decode()
    for raw in (map_raw, index_raw, export_raw):
        assert not any(t in raw for t in _HOOK_TEXT)
        assert "stop msg" not in raw and "stop cmd" not in raw
    assert _HOOK_BLOCK in map_raw and _HOOK_BLOCK not in index_raw

@pytest.mark.parametrize("folder", ["subagents", "subagents/workflows/wf_0a1b2c3d-4e5"])
def test_hook_detail_finds_a_subagent_hook_in_its_own_transcript(hooked, folder):
    sub = hooked.transcript.with_suffix("") / folder
    sub.mkdir(parents=True)
    uuid = "55555555-0000-0000-0000-000000000005"
    (sub / "agent-abc.jsonl").write_text(_line(type="attachment", uuid=uuid, attachment={
        "type": "hook_blocking_error", "hookName": "Stop", "hookEvent": "Stop",
        "blockingError": {"blockingError": "sub msg", "command": "sub cmd"}}))
    assert json.loads(hooked.hook(uuid)[2]) == {"success": True, "message": "sub msg", "command": "sub cmd"}


def test_maps_built_by_another_version_are_rebuilt_once(env, monkeypatch):
    """A redaction fix must reach maps already on disk, not only new sessions."""
    _write_claude_session(env["home"], "12121212-0000-0000-0000-000000000012", env["repo"])
    assert pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"])).built == 1
    assert pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"])).unchanged == 1

    # An index written by an older agentplay (no builtWith, or a different one).
    st = Store(env["sm"])
    for row in st.index.values():
        row.pop("builtWith", None)
    st.save_index()
    rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert (rep.built, rep.unchanged) == (1, 0)

    monkeypatch.setattr(pipeline, "__version__", "9.9.9")
    rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert (rep.built, rep.unchanged) == (1, 0)
    assert all(r["builtWith"] == "9.9.9" for r in Store(env["sm"]).index.values())
    assert pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"])).unchanged == 1


def test_a_new_path_mode_re_redacts_fresh_maps(env, monkeypatch):
    """Asking for dirs_only (or hashed without a key) must reach maps already built."""
    monkeypatch.delenv("AGENTPLAY_HMAC_KEY", raising=False)
    _write_claude_session(env["home"], "13131313-0000-0000-0000-000000000013", env["repo"])
    assert pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"])).built == 1
    for mode in ("dirs_only", "hashed"):
        rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]), path_mode=mode)
        st = Store(env["sm"])
        doc = st.get(next(iter(st.index)))
        assert doc["session"]["pathMode"] == "dirs_only"
        assert doc["files"] and all("•" in f["path"] for f in doc["files"]), mode
        assert rep.built == (1 if mode == "dirs_only" else 0)  # hashed w/o key == dirs_only: fresh
    rep = pipeline.build_all(agents={"claude-code"}, store=Store(env["sm"]))
    assert rep.built == 1  # back to relative
