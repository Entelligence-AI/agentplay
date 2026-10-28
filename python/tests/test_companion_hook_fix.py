"""A hook that answers the wrong event: found from Claude Code's own record of the failure, fixed in Claude Code's
settings (outside any repo) through the same journaled writer, undone the same way. Plus the two checks that
feed the menu honestly: a live record only counts while its process is the one that wrote it, and a correction
that says "still not working" is told apart from one that changes direction."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentplay import companion, discover, fixes, usage

CMD = '"$HOME"/.claude/collab/guard.sh prompt'
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _homes(home: Path) -> discover.Homes:
    fields = dataclasses.fields(discover.Homes)
    return discover.Homes(*([home] if i == 0 else [home / "none"] for i in range(len(fields))))


def _settings(home: Path, hooks: dict) -> Path:
    p = home / "settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": hooks}, indent=2) + "\n")
    return p


def _failure_transcript(home: Path, command: str = CMD) -> None:
    proj = home / "projects" / "-Users-me-repo"
    proj.mkdir(parents=True)
    line = {"type": "system", "attachment": {
        "type": "hook_non_blocking_error", "hookName": "SessionStart:startup", "hookEvent": "SessionStart",
        "command": command, "exitCode": 1,
        "stderr": "Failed to run: Hook returned incorrect event name: expected 'SessionStart' but got 'UserPromptSubmit'.",
    }}
    (proj / "s.jsonl").write_text(json.dumps(line) + "\n")


def test_hook_cause_names_the_remedy_and_keeps_the_command_out(tmp_path):
    home = tmp_path / ".claude"
    _settings(home if home.mkdir() is None else home, {
        "SessionStart": [{"hooks": [{"type": "command", "command": CMD}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": CMD}]}],
    })
    _failure_transcript(home)
    hc = companion.hook_cause(_homes(home), "SessionStart:startup")
    assert "wrong event" in hc["cause"] and "when a session starts" in hc["cause"] and "when you send a message" in hc["cause"]
    assert hc["remedy"].startswith("Apply takes its entry out of SessionStart")
    assert hc["_action"] == {"kind": "hook_event", "file": "settings.json", "root": str(home), "command": CMD,
                             "from": "SessionStart", "to": None}
    # What the snapshot carries: the name of the command, never its arguments or path.
    public = json.dumps({k: v for k, v in hc.items() if not k.startswith("_")})
    assert "prompt" not in public and "/collab/" not in public and "guard.sh" in public


def test_hook_cause_moves_the_entry_when_it_is_not_registered_at_the_right_event(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    _settings(home, {"SessionStart": [{"hooks": [{"type": "command", "command": CMD}]}]})
    _failure_transcript(home)
    hc = companion.hook_cause(_homes(home), "SessionStart:startup")
    assert hc["_action"]["to"] == "UserPromptSubmit"
    assert hc["remedy"] == (f"Apply moves its entry from SessionStart to UserPromptSubmit in "
                            f"{companion._home_label(home / 'settings.json')}, so it runs when you send a message.")


def test_hook_event_fix_applies_to_claude_home_and_undoes(tmp_path):
    base = tmp_path / "store"
    base.mkdir()
    home = tmp_path / ".claude"
    home.mkdir()
    other = {"type": "command", "command": "echo start"}
    p = _settings(home, {
        "SessionStart": [{"hooks": [other, {"type": "command", "command": CMD}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": CMD}]}],
    })
    original = p.read_text()
    fix = {"id": "hookfix", "rule": "broken_hook", "repo": fixes.HOME_REPO, "title": "Fix the hook"}
    action = {"kind": "hook_event", "file": "settings.json", "root": str(home), "command": CMD,
              "from": "SessionStart", "to": None}
    # Before any grant, the preview still shows the change, from the root the fix found.
    pv = fixes.preview(base, fixes.HOME_REPO, fix, action, root=str(home))
    assert any(h["command"] == CMD for g in json.loads(pv["before"])["hooks"]["SessionStart"] for h in g["hooks"])
    assert json.loads(pv["after"])["hooks"]["SessionStart"] == [{"hooks": [other]}]
    with pytest.raises(fixes.FixError):
        fixes.apply(base, fix, action)  # not granted yet
    fixes.grant(base, fixes.HOME_REPO, str(home))  # no .git needed for Claude's own folder
    entry = fixes.apply(base, fix, action)
    after = json.loads(p.read_text())
    assert after["hooks"]["SessionStart"] == [{"hooks": [other]}]
    assert after["hooks"]["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": CMD}]}]
    assert after["model"] == "opus"
    fixes.undo(base, entry["id"])
    # Back where it was, at both events. Undo re-adds the entry in a group of its own, so compare what runs.
    def commands(settings: dict) -> dict:
        return {ev: sorted(h["command"] for g in groups for h in g["hooks"]) for ev, groups in settings["hooks"].items()}
    assert commands(json.loads(p.read_text())) == commands(json.loads(original))


def test_hook_event_fix_moves_and_undo_takes_it_back(tmp_path):
    base = tmp_path / "store"
    base.mkdir()
    home = tmp_path / ".claude"
    home.mkdir()
    p = _settings(home, {"SessionStart": [{"hooks": [{"type": "command", "command": CMD}]}]})
    original = json.loads(p.read_text())
    fixes.grant(base, fixes.HOME_REPO, str(home))
    fix = {"id": "hookmove", "rule": "broken_hook", "repo": fixes.HOME_REPO, "title": "Fix the hook"}
    action = {"kind": "hook_event", "file": "settings.json", "root": str(home), "command": CMD,
              "from": "SessionStart", "to": "UserPromptSubmit"}
    entry = fixes.apply(base, fix, action)
    after = json.loads(p.read_text())
    assert "SessionStart" not in after["hooks"]
    assert after["hooks"]["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": CMD}]}]
    fixes.undo(base, entry["id"])
    assert json.loads(p.read_text()) == original


def test_hook_event_fix_refuses_files_outside_claude_settings(tmp_path):
    base = tmp_path / "store"
    base.mkdir()
    home = tmp_path / ".claude"
    home.mkdir()
    fixes.grant(base, fixes.HOME_REPO, str(home))
    fix = {"id": "x", "rule": "broken_hook", "repo": fixes.HOME_REPO, "title": "t"}
    with pytest.raises(fixes.FixError):
        fixes.apply(base, fix, {"kind": "hook_event", "file": "CLAUDE.md", "command": CMD, "from": "SessionStart"})
    with pytest.raises(fixes.FixError):
        fixes.apply(base, fix, {"kind": "hook_event", "file": "../settings.json", "command": CMD, "from": "SessionStart"})


def test_live_record_counts_only_while_its_process_is_the_same_one(monkeypatch, tmp_path):
    home = tmp_path / ".claude"
    (home / "sessions").mkdir(parents=True)
    import os
    me = os.getpid()
    rec = {"pid": me, "sessionId": "abc", "status": "idle", "cwd": "/Users/me/repo", "startedAt": 1_792_000_000_000}
    (home / "sessions" / f"{me}.json").write_text(json.dumps({**rec, "procStart": "Sat Oct 24 11:50:26 2026"}))
    # The same pid, started at a different time: a reboot gave the number to something else.
    monkeypatch.setattr(companion, "_process_starts", lambda pids: {me: {"Sat Oct 24 17:20:22 2026"}})
    assert companion.live_claude_sessions(_homes(home), NOW) == []
    # Claude Code writes the start in UTC; ps answers in UTC and local time, either matches.
    monkeypatch.setattr(companion, "_process_starts", lambda pids: {me: {"Sat Oct 24 11:50:26 2026", "Sat Oct 24 17:20:26 2026"}})
    assert [r["pid"] for r in companion.live_claude_sessions(_homes(home), NOW)] == [me]
    # No record of the start: the alive check alone decides.
    (home / "sessions" / f"{me}.json").write_text(json.dumps(rec))
    monkeypatch.setattr(companion, "_process_starts", lambda pids: {me: {"whenever"}})
    assert [r["pid"] for r in companion.live_claude_sessions(_homes(home), NOW)] == [me]
    # ps unavailable: trust the alive check.
    monkeypatch.setattr(companion, "_process_starts", lambda pids: None)
    assert [r["pid"] for r in companion.live_claude_sessions(_homes(home), NOW)] == [me]


def test_process_starts_reads_ps_for_this_process():
    import os
    starts = companion._process_starts([os.getpid()])
    assert starts is not None and os.getpid() in starts
    assert any(s.endswith(("2026", "2027", "2025")) for s in starts[os.getpid()])


def _user(text: str, uid: str) -> bytes:
    return json.dumps({"type": "user", "uuid": uid, "timestamp": "2026-10-26T10:00:00Z",
                       "message": {"role": "user", "content": text}}).encode()


def test_corrections_that_say_it_still_does_not_work_are_told_apart():
    kinds = lambda out: sorted(k for _, k, _ in out)  # noqa: E731
    first, seen = usage._signal_line(_user("please add a test", "u0"), False)
    assert kinds(first) == ["msg"] and seen
    redirect, _ = usage._signal_line(_user("no, use the other file", "u1"), True)
    assert kinds(redirect) == ["cor", "msg"]
    broken, _ = usage._signal_line(_user("the button is still not working!!!", "u2"), True)
    assert kinds(broken) == ["cor", "cornw", "fru", "msg"]
    sworn, _ = usage._signal_line(_user("revert that, wtf", "u3"), True)
    assert kinds(sworn) == ["cor", "fru", "fruw", "msg"]
    rates = usage.signals(redirect + broken + sworn, NOW)["current"]
    assert rates["corrections"] == 3 and rates["notWorking"] == 1
    assert rates["frustrated"] == 2 and rates["swearing"] == 1
