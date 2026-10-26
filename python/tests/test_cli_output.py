"""Terminal output: log lines, the progress bar, and the end-of-run summary."""

from __future__ import annotations

import io
import json
import logging
from collections import Counter
from datetime import UTC, datetime

import pytest

from agentplay import cli, pipeline


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch):
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    monkeypatch.setattr(cli, "_progress_live", False)
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _emit(handler: logging.Handler, msg: str = "session skipped", name: str = "agentplay.x.source",
          level: int = logging.WARNING) -> None:
    handler.handle(logging.LogRecord(name, level, __file__, 1, msg, None, None))


def test_default_format_is_user_facing_with_no_module_path() -> None:
    out = io.StringIO()
    _emit(cli._log_handler(out, verbose=False))
    assert out.getvalue() == "agentplay: warning: session skipped\n"


def test_verbose_format_names_the_logger_and_level() -> None:
    out = io.StringIO()
    _emit(cli._log_handler(out, verbose=True), name="agentplay.x.source", level=logging.DEBUG)
    assert out.getvalue() == "agentplay: debug: agentplay.x.source: session skipped\n"


def test_live_progress_line_is_cleared_before_the_message() -> None:
    out = _TTY()
    cli._progress_live = True
    _emit(cli._log_handler(out, verbose=False))
    assert out.getvalue() == "\r\x1b[2Kagentplay: warning: session skipped\n"


def test_no_clear_sequence_when_no_progress_is_live() -> None:
    out = _TTY()
    _emit(cli._log_handler(out, verbose=False))
    assert "\x1b" not in out.getvalue()


def test_progress_tracks_whether_a_bar_is_on_screen(monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stderr", _TTY())
    cli._progress(1, 10, "")
    assert cli._progress_live
    cli._progress(10, 10, "")
    assert not cli._progress_live


def test_progress_is_not_live_when_stderr_is_not_a_tty(monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stderr", io.StringIO())
    cli._progress(1, 10, "")
    assert not cli._progress_live


def _report(**kw) -> pipeline.BuildReport:
    return pipeline.BuildReport(found=563, built=557, **kw)


def test_summary_lists_skip_reasons_by_count(monkeypatch, capsys) -> None:
    rep = _report(skipped=7, skip_reasons=Counter(no_conversation=4, too_large=2, empty_or_unreadable=1))
    monkeypatch.setattr(cli.pipeline, "build_all", lambda **_kw: rep)
    assert cli.main(["build"]) == 0
    assert "7 skipped (4 no conversation, 2 too large, 1 empty or unreadable)" in capsys.readouterr().out


def test_summary_has_no_breakdown_when_nothing_was_skipped(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.pipeline, "build_all", lambda **_kw: _report())
    assert cli.main(["build"]) == 0
    assert "0 skipped in" in capsys.readouterr().out


def test_main_ignores_python_warnings_unless_the_user_opted_in(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(cli.warnings, "simplefilter", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(cli.pipeline, "build_all", lambda **_kw: _report())
    monkeypatch.setattr(cli.sys, "warnoptions", [])
    cli.main(["build"])
    assert calls == [("ignore",)]
    calls.clear()
    monkeypatch.setattr(cli.sys, "warnoptions", ["error"])
    cli.main(["build"])
    assert calls == []


def test_build_all_tallies_discovery_skips_but_not_subagents_or_other_agents(monkeypatch, tmp_path) -> None:
    class _Source:
        def discovery_stats(self):
            skipped = {"no_conversation": 2, "subagent_component": 9, "different_agent": 5}
            return {"claude-code": {"seen": 16, "indexed": 0, "skipped": skipped}}

    monkeypatch.setattr(pipeline.discover, "materialize_all", lambda *_a: {})
    monkeypatch.setattr(pipeline.discover, "local_source", lambda *_a: _Source())
    monkeypatch.setattr(pipeline.discover, "candidates", lambda *_a, **_k: [])
    rep = pipeline.build_all(store=pipeline.Store(tmp_path))
    assert rep.skipped == 2
    assert rep.skip_reasons == {"no_conversation": 2}


@pytest.mark.parametrize("scope", [{"since": datetime(2026, 1, 1, tzinfo=UTC)}, {"limit": 5}])
def test_scoped_build_does_not_count_skips_from_the_whole_disk(monkeypatch, tmp_path, scope) -> None:
    """The walk sees every file on disk; a --since/--limit run must not report those as its skips."""
    class _Source:
        def discovery_stats(self):
            return {"claude-code": {"seen": 3, "indexed": 0, "skipped": {"no_conversation": 3}}}

    monkeypatch.setattr(pipeline.discover, "materialize_all", lambda *_a: {})
    monkeypatch.setattr(pipeline.discover, "local_source", lambda *_a: _Source())
    monkeypatch.setattr(pipeline.discover, "candidates", lambda *_a, **_k: [])
    rep = pipeline.build_all(store=pipeline.Store(tmp_path), **scope)
    assert rep.skipped == 0
    assert not rep.skip_reasons


def test_agent_breakdown_adds_up_to_found(monkeypatch, capsys) -> None:
    rep = pipeline.BuildReport(found=52, built=49, skipped=3, by_agent=Counter({"claude-code": 44, "codex": 5}))
    monkeypatch.setattr(cli.pipeline, "build_all", lambda **_kw: rep)
    assert cli.main(["build"]) == 0
    assert "claude-code 44 · codex 5 · 3 not built" in capsys.readouterr().out


def test_agent_breakdown_has_no_remainder_when_everything_was_built() -> None:
    rep = pipeline.BuildReport(found=5, built=5, by_agent=Counter({"codex": 5}))
    assert cli._agent_breakdown(rep) == "codex 5"
    assert cli._agent_breakdown(pipeline.BuildReport()) == "none"


class _FakeServer:
    """What ``serve`` returns, bound to ``port``, without opening a socket."""

    def __init__(self, port: int) -> None:
        self.server_address = ("127.0.0.1", port)

    def serve_forever(self) -> None:
        pass

    def server_close(self) -> None:
        pass


@pytest.mark.parametrize(("requested", "bound", "notice"), [(7317, 62505, True), (7317, 7317, False), (0, 50123, False)])
def test_view_says_when_the_requested_port_is_busy(monkeypatch, capsys, requested, bound, notice) -> None:
    from agentplay import server

    monkeypatch.setattr(server, "serve", lambda _port, **_kw: _FakeServer(bound))
    assert cli.main(["view", "--port", str(requested), "--no-open", "--no-build"]) == 0
    out, err = capsys.readouterr()
    assert (f"Port {requested} is busy; using {bound} instead." in err) is notice
    assert f"127.0.0.1:{bound}/" in out


@pytest.fixture
def list_store(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTPLAY_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_TTY", False)
    st = cli.Store()
    recent = datetime(2026, 10, 20, tzinfo=UTC).timestamp()
    old = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    for sid, agent, started, modified, role in [
        ("other", "claude-code", "2026-10-23T00:00:00Z", recent, None),
        ("child", "codex", "2026-10-22T00:00:00Z", recent, "subagent"),
        ("helper", "codex", "2026-10-21T00:00:00Z", recent, "helper"),
        ("imported", "codex", "2026-10-20T01:00:00+02:00", None, None),
        ("stale", "codex", "2026-10-19T00:00:00Z", old, None),
        ("updated", "codex", "2026-09-01T00:00:00Z", recent, None),
        ("old-import", "codex", "2026-08-01T00:00:00Z", None, None),
        ("undated", "codex", "", None, None),
    ]:
        st.index[sid] = {"sessionId": sid, "agentTool": agent, "startedAt": started, "role": role}
        if modified is not None:
            st.index[sid]["sourceMtime"] = modified
    st.save_index()
    return st


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("since", ["7d", "168h", "1w", "2026-10-16"])
def test_list_filters_before_limit(list_store, monkeypatch, capsys, as_json, since):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 23, tzinfo=UTC)

    monkeypatch.setattr(cli, "datetime", Clock)
    flags = ["--json"] if as_json else []
    assert cli.main(["list", "--agents", "codex", "--since", since, "--limit", "2", *flags]) == 0
    out = capsys.readouterr().out
    ids = [r["sessionId"] for r in json.loads(out)] if as_json else [s.strip() for s in out.splitlines()[1::2]]
    assert ids == ["imported", "updated"]


@pytest.mark.parametrize(("flags", "expected"), [
    ([], ["other", "imported", "stale", "updated", "old-import", "undated"]),
    (["--agents", "codex, claude-code", "--limit", "1"], ["other"]),
    (["--agents", "cursor"], []),
    (["--since", "2026-10-20T00:00:00Z"], ["other", "updated"]),
])
def test_list_filters_independently(list_store, capsys, flags, expected):
    assert cli.main(["list", "--json", *flags]) == 0
    assert [r["sessionId"] for r in json.loads(capsys.readouterr().out)] == expected


@pytest.mark.parametrize(("flags", "message"), [
    (["--agents", "unknown"], "unknown agent"),
    (["--since", "yesterday"], "can't read --since"),
])
def test_list_rejects_invalid_filters(list_store, flags, message):
    with pytest.raises(SystemExit, match=message):
        cli.main(["list", *flags])


@pytest.mark.parametrize("filtered", [False, True])
def test_list_empty_message(list_store, capsys, filtered):
    flags = ["--agents", "cursor"] if filtered else []
    if not filtered:
        list_store.index.clear()
        list_store.save_index()
    assert cli.main(["list", *flags]) == 0
    expected = "No sessions match these filters." if filtered else "No maps yet. Run `agentplay build`."
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize(("started", "expected"), [
    ("2026-10-20T00:00:00Z", ["session"]),
    ("2026-09-01T00:00:00Z", []),
])
def test_list_uses_start_time_for_zero_source_mtime(list_store, capsys, started, expected):
    list_store.index.clear()
    list_store.index["session"] = {
        "sessionId": "session", "agentTool": "codex", "startedAt": started, "sourceMtime": 0.0,
    }
    list_store.save_index()
    assert cli.main(["list", "--since", "2026-10-16", "--json"]) == 0
    assert [r["sessionId"] for r in json.loads(capsys.readouterr().out)] == expected
