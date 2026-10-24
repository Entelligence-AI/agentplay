"""Autotune across runs: retiring, revoked repos, approved changes, the hook file and the CLI around it."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from typing import Any

import pytest

from agentplay import autotune, cli, fixes, health
from agentplay.store import Store

MD = {"kind": "claude_md", "file": "CLAUDE.md", "lines": ["Run `pytest -q`."]}
HOOK = {"kind": "stop_hook", "label": "Add Stop hook", "file": ".claude/settings.json", "command": "pytest"}
METRIC = {"name": "verified_share"}


def _fix(fix_id: str, action: dict[str, Any]) -> dict[str, Any]:
    return {"id": fix_id, "repo": "acme/api", "title": f"fix {fix_id}", "rule": "r", "pattern": "",
            "points": 1.0, "metric": METRIC, "actions": [action]}


@pytest.fixture()
def tuned(tmp_path, monkeypatch):
    """A granted, enabled repo whose fixes never move their metric; ``due`` makes every applied fix up for review."""
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    base = tmp_path / "home"
    base.mkdir()
    fixes.record_roots(base, {"acme/api": str(root)})
    fixes.grant(base, "acme/api")
    autotune.set_repo(base, "acme/api", enabled=True)
    found: list[dict[str, Any]] = []
    due: list[bool] = []
    monkeypatch.setattr(health, "fixes", lambda _rows: found)
    monkeypatch.setattr(autotune, "root_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(autotune, "metric", lambda *_a: 0.5)
    monkeypatch.setattr(autotune, "effects", lambda st: [
        {**e, "sessionsSince": autotune.RETIRE_AFTER if due else 0, "current": 0.5} for e in fixes.active(st.base)
    ])
    return Store(base), root, found, due


def test_a_retired_fix_is_not_applied_again(tuned):
    st, root, found, due = tuned
    found.append(_fix("md0001", MD))
    assert autotune.run(st, force=True)["applied"] == ["fix md0001"]
    due.append(True)
    assert autotune.run(st, force=True)["retired"] == ["fix md0001"]
    assert autotune.run(st, force=True)["applied"] == []
    assert not (root / "CLAUDE.local.md").exists()
    # Only Autotune skips it: the person can still apply it by hand.
    fixes.apply(st.base, found[0], MD, rel="CLAUDE.local.md")
    assert "pytest" in (root / "CLAUDE.local.md").read_text()

def test_switching_autotune_off_leaves_its_fixes_alone(tuned):
    st, root, found, due = tuned
    found.append(_fix("md0001", MD))
    autotune.run(st, force=True)
    autotune.set_repo(st.base, "acme/api", enabled=False)
    due.append(True)
    assert autotune.run(st, force=True)["retired"] == []
    assert "pytest" in (root / "CLAUDE.local.md").read_text()

def test_an_unreadable_config_is_reported_and_never_overwritten(tuned):
    st, *_ = tuned
    path = st.base / autotune.CONFIG
    path.write_text("{not json")
    with pytest.raises(fixes.FixError) as exc:
        autotune.run(st, force=True)
    assert exc.value.code == "bad_json" and path.read_text() == "{not json"


def test_a_revoked_or_broken_repo_does_not_abort_the_run(tuned, monkeypatch):
    st, root, found, due = tuned
    found.append(_fix("md0001", MD))
    autotune.run(st, force=True)
    due.append(True)
    fixes.revoke(st.base, "acme/api")
    # A clock of its own, so the assertion proves this run moved the daily marker.
    monkeypatch.setattr(autotune, "_now", lambda: "2099-01-01T00:00:00Z")
    res = autotune.run(st, force=True, today="2099-01-01")
    assert res["retired"] == [] and autotune.config(st.base)["lastRun"] == "2099-01-01T00:00:00Z"
    assert not [x for x in autotune.config(st.base)["log"] if x["kind"] == "error"]
    # Granted again but the checkout lost .git: logged once, not on every daily run.
    fixes.grant(st.base, "acme/api")
    shutil.rmtree(root / ".git")
    autotune.run(st, force=True)
    autotune.run(st, force=True)
    errors = [x for x in autotune.config(st.base)["log"] if x["kind"] == "error"]
    assert len(errors) == 1 and "git checkout" in errors[0]["detail"]
    assert len(fixes.active(st.base)) == 1  # still there to retire once the checkout is back


def test_autotune_never_removes_a_change_the_person_approved(tuned):
    st, root, found, due = tuned
    found.append(_fix("hook01", HOOK))
    assert autotune.run(st, force=True)["queued"] == ["fix hook01"]
    autotune.approve(st, "hook01")
    due.append(True)
    assert autotune.run(st, force=True)["retired"] == []
    assert [e["fixId"] for e in fixes.active(st.base)] == ["hook01"]
    assert "pytest" in (root / ".claude" / "settings.json").read_text()


def _fake_home(monkeypatch: pytest.MonkeyPatch, path: Any) -> None:
    # Path.home() reads USERPROFILE on Windows, HOME elsewhere; set both so a test
    # never touches the real ~/.claude/settings.json.
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


def test_hook_install_writes_through_a_symlinked_settings_file(tmp_path, monkeypatch):
    _fake_home(monkeypatch, tmp_path)
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"theme": "dark"}))
    (tmp_path / ".claude").mkdir()
    link = tmp_path / ".claude" / "settings.json"
    link.symlink_to(real)
    autotune.install_hook()
    assert link.is_symlink() and autotune.hook_installed()
    assert json.loads(real.read_text())["theme"] == "dark"
    autotune.uninstall_hook()
    assert link.is_symlink() and not autotune.hook_installed()
    assert json.loads(real.read_text())["theme"] == "dark"


@pytest.mark.parametrize("settings", [{"hooks": None}, {"hooks": []}, {"hooks": {"SessionStart": "x"}}, [1]])
def test_a_hand_broken_settings_file_is_reported_not_crashed_or_rewritten(tmp_path, monkeypatch, settings):
    _fake_home(monkeypatch, tmp_path)
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps(settings))
    assert autotune.hook_installed() is False
    autotune.uninstall_hook()
    with pytest.raises(fixes.FixError) as exc:
        autotune.install_hook()
    assert exc.value.code == "bad_json" and json.loads(path.read_text()) == settings


def test_cron_line_names_an_absolute_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Store", lambda: Store(tmp_path))
    monkeypatch.setattr(fixes, "status", lambda _b: {"repos": []})
    monkeypatch.setattr(autotune, "uninstall_hook", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda _n: "/opt/tools/bin/agentplay")

    class A:
        action, at, repo = "on", "09:30", []

    assert cli.cmd_autotune(A()) == 0
    exe = shlex.quote(os.path.abspath("/opt/tools/bin/agentplay"))  # a drive-qualified path on Windows
    assert f"30 9 * * * {exe} autotune run --daily --quiet" in capsys.readouterr().out


def test_fix_variant_out_of_range_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Store", lambda: Store(tmp_path))
    monkeypatch.setattr(cli, "_health_fixes", lambda *_a, **_k: [_fix("md0001", MD)])
    applied = []
    monkeypatch.setattr(fixes, "apply", lambda *a, **k: applied.append(a))

    class A:
        action, target, to = "apply", "1", None

    for variant in (1, -1):
        A.variant = variant
        assert cli.cmd_fix(A()) == 1
        assert "--variant" in capsys.readouterr().err
    assert not applied

def test_undo_works_even_when_the_health_report_cannot_be_computed(tuned, monkeypatch, capsys):
    st, root, found, _ = tuned
    entry = fixes.apply(st.base, _fix("md0001", MD), MD)
    monkeypatch.setattr(cli, "Store", lambda: st)

    def broken(*_a, **_k):
        raise ValueError("a stored map is malformed")

    monkeypatch.setattr(cli, "_health_fixes", broken)

    class A:
        action, target = "undo", entry["id"]

    assert cli.cmd_fix(A()) == 0 and not (root / "CLAUDE.md").exists()

def test_approving_a_change_after_its_repo_is_revoked_writes_nothing(tuned):
    st, root, found, _ = tuned
    found.append(_fix("hook01", HOOK))
    assert autotune.run(st, force=True)["queued"] == ["fix hook01"]
    fixes.revoke(st.base, "acme/api")
    with pytest.raises(fixes.FixError) as exc:
        autotune.approve(st, "hook01")
    assert exc.value.code == "not_granted" and not (root / ".claude" / "settings.json").exists()


def test_cron_line_escapes_percent_signs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Store", lambda: Store(tmp_path))
    monkeypatch.setattr(fixes, "status", lambda _b: {"repos": []})
    monkeypatch.setattr(autotune, "uninstall_hook", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda _n: "/opt/100%/agentplay")

    class A:
        action, at, repo = "on", "09:30", []

    assert cli.cmd_autotune(A()) == 0
    line = capsys.readouterr().out.splitlines()[-1]
    assert "100\\%" in line and "100%/" not in line
