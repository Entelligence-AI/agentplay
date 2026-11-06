"""Builder: outputs, models, tasks, roles,
Cursor/Codex coverage and worker hardening.

Golden fixtures per harness (Claude Code JSONL + subagent file, Codex spans incl. code
mode and a spawned child, Cursor spans) carry every one of these signals plus the negative
cases that must NOT produce an output (failed commit, un-mergeable PR, errored ticket
call, errored artifact publish).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentplay import builder as sm


# ---------------------------------------------------------------- helpers
def _line(**d):
    return json.dumps(d) + "\n"


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Windows expands ~ from USERPROFILE, and VS Code paths from APPDATA.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _repo(tmp_path, name="dashboard", remote="acme/dashboard"):
    repo = tmp_path / "work" / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = git@github.com:{remote}.git\n'
    )
    return repo


def _ts(sec: float) -> str:
    return (datetime(2026, 9, 23, 15, 44, tzinfo=UTC) + timedelta(seconds=sec)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _call(i, sec, cwd, name, inp, result, *, is_error=False, model="claude-opus-5-5"):
    return [
        _line(
            type="assistant",
            timestamp=_ts(sec),
            cwd=cwd,
            message={
                "id": f"m{i}",
                "model": model,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}],
            },
        ),
        _line(
            type="user",
            timestamp=_ts(sec + 1),
            cwd=cwd,
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{i}",
                        "content": result,
                        "is_error": is_error,
                    }
                ],
            },
        ),
    ]


def _prompt(sec, cwd, text, images=0):
    content = [{"type": "text", "text": text}] + [
        {"type": "image", "source": {"type": "base64", "data": "x"}} for _ in range(images)
    ]
    return [
        _line(
            type="user",
            timestamp=_ts(sec),
            cwd=cwd,
            message={"role": "user", "content": content},
        )
    ]


def _write(tmp_path, groups, name="s"):
    path = tmp_path / "proj" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line for g in groups for line in g))
    return path


def _kinds(doc, kind):
    return [e for e in doc["events"] if e["kind"] == kind]


# ---------------------------------------------------------------- Claude Code golden
@pytest.fixture()
def claude_doc(tmp_path, fake_home):
    repo = _repo(tmp_path)
    cwd = str(repo)
    skills = fake_home / ".claude" / "skills"
    (skills / "old-skill").mkdir(parents=True)
    (skills / "old-skill" / "SKILL.md").write_text("x")
    page = tmp_path / "page.html"
    page.write_text("<html>" + "x" * 994 + "</html>")  # 1007 bytes
    groups = [
        _prompt(0, cwd, "look at https://app.example.com/x and https://github.com/a/b", images=1),
        # commit: confirmed, then failed (nothing to commit), then errored
        _call(
            1,
            5,
            cwd,
            "Bash",
            {"command": f"cd {cwd} && git add -A && git commit -m 'fix csp'"},
            "[main 75f3d6d9a] fix csp\n 2 files changed, 10 insertions(+), 3 deletions(-)",
        ),
        _call(
            2,
            10,
            cwd,
            "Bash",
            {"command": "git commit -m x"},
            "nothing to commit, working tree clean",
        ),
        _call(
            3,
            15,
            cwd,
            "Bash",
            {"command": "git commit -m y"},
            "Exit code 1\nhook failed",
            is_error=True,
        ),
        # PR create → merge (confirmed) + a refused merge + a comment
        _call(
            4,
            20,
            cwd,
            "Bash",
            {"command": "gh pr create --title t --body b"},
            "https://github.com/acme/dashboard/pull/102",
        ),
        _prompt(30, cwd, "merge it"),
        _call(
            5,
            35,
            cwd,
            "Bash",
            {"command": "gh pr merge 3890 --squash"},
            "X Pull request acme/dashboard#3890 is not mergeable: the base branch policy prohibits the merge.",
        ),
        _call(
            6,
            40,
            cwd,
            "Bash",
            {"command": "gh pr merge 102 --squash --delete-branch --admin"},
            "✓ Squashed and merged pull request acme/dashboard#102 (fix csp)",
        ),
        _call(
            7,
            45,
            cwd,
            "Bash",
            {"command": "gh pr comment 102 --body 'thanks'"},
            "https://github.com/acme/dashboard/pull/102#issuecomment-1234",
        ),
        _call(
            8,
            47,
            cwd,
            "Bash",
            {"command": "gh pr merge 102 --auto --squash"},
            "! Pull request acme/dashboard#102 will be automatically merged via squash when all requirements are met",
        ),
        # tickets: create (confirmed), update (confirmed), create that errored in its result
        _call(
            9,
            50,
            cwd,
            "mcp__claude_ai_Linear__save_issue",
            {"team": "ENT", "title": "t"},
            json.dumps({"id": "ACME-4787", "title": "t"}),
        ),
        _call(
            10,
            55,
            cwd,
            "mcp__claude_ai_Linear__save_issue",
            {"id": "ACME-4787", "state": "Done"},
            json.dumps({"id": "ACME-4787", "state": "Done"}),
        ),
        _call(
            11,
            60,
            cwd,
            "mcp__claude_ai_Linear__save_issue",
            {"team": "Nope", "title": "t"},
            'Error: Could not find team "Nope"',
        ),
        _call(
            12,
            62,
            cwd,
            "mcp__claude_ai_Linear__get_issue",
            {"id": "ACME-1"},
            json.dumps({"id": "ACME-1"}),
        ),
        # artifact publish: confirmed, and an errored one
        _call(
            13,
            65,
            cwd,
            "Artifact",
            {"file_path": str(page)},
            "Published page.html at https://claude.ai/artifact/AbCdEf0123456789xyz (Version 3)",
        ),
        _call(14, 70, cwd, "Artifact", {"file_path": str(page)}, "publish failed", is_error=True),
        _call(
            15,
            72,
            cwd,
            "Artifact",
            {"action": "read", "url": "https://claude.ai/artifact/x"},
            "https://claude.ai/artifact/x",
        ),
        # skills: create a new one, update an existing one
        _call(
            16,
            75,
            cwd,
            "Write",
            {"file_path": str(skills / "new-skill" / "SKILL.md"), "content": "x"},
            "File created",
        ),
        _call(
            17,
            80,
            cwd,
            "Edit",
            {
                "file_path": str(skills / "old-skill" / "SKILL.md"),
                "old_string": "x",
                "new_string": "y",
            },
            "ok",
        ),
        # model switch on the main agent
        _call(18, 90, cwd, "Bash", {"command": "ls"}, "a", model="claude-sonnet-5"),
    ]
    path = _write(tmp_path, groups)
    return sm.build_claude_code_map(path)


def test_claude_commit_confirmed_only(claude_doc):
    commits = _kinds(claude_doc, "commit")
    assert len(commits) == 1
    c = commits[0]
    assert (c["sha"], c["files"], c["ins"], c["del"], c["repo"]) == (
        "75f3d6d9a",
        2,
        10,
        3,
        "dashboard",
    )
    assert claude_doc["events"][c["ref"]]["tool"] == "Bash"
    assert claude_doc["totals"]["commits"] == 1


def test_claude_pr_merge_state_and_comment(claude_doc):
    merges = _kinds(claude_doc, "pr_merge")
    assert [(m.get("repo"), m.get("number")) for m in merges] == [("acme/dashboard", 102)]
    assert claude_doc["prs"][0]["state"] == "merged"
    assert [c.get("number") for c in _kinds(claude_doc, "pr_comment")] == [102]
    t = claude_doc["totals"]
    assert (t["prsCreated"], t["prsMerged"], t["prsClosed"], t["prComments"]) == (1, 1, 0, 1)


def test_claude_tickets_create_update_and_errors_dropped(claude_doc):
    tickets = _kinds(claude_doc, "ticket")
    assert [(t["tracker"], t["action"], t.get("id")) for t in tickets] == [
        ("linear", "create", "ACME-4787"),
        ("linear", "update", "ACME-4787"),
    ]
    assert claude_doc["totals"]["ticketsCreated"] == 1
    assert claude_doc["totals"]["ticketsUpdated"] == 1


def test_claude_artifact_publish_confirmed_with_size_and_version(claude_doc):
    arts = _kinds(claude_doc, "artifact")
    assert len(arts) == 1
    assert (arts[0]["bytes"], arts[0]["version"]) == (1007, 3)


def test_claude_skill_writes(claude_doc):
    got = [(s["name"], s["action"]) for s in _kinds(claude_doc, "skill_write")]
    assert got == [("new-skill", "create"), ("old-skill", "update")]
    t = claude_doc["totals"]
    assert (t["skillsCreated"], t["skillsUpdated"]) == (1, 1)


def test_claude_models_on_every_event_and_switch(claude_doc):
    assert all(e.get("model") for e in claude_doc["events"])
    sw = _kinds(claude_doc, "model_switch")
    assert [(s["from"], s["to"]) for s in sw] == [("claude-opus-5-5", "claude-sonnet-5")]
    assert claude_doc["totals"]["modelSwitches"] == 1


def test_claude_prompt_counts_never_text(claude_doc):
    u = _kinds(claude_doc, "user")[0]
    assert (u["images"], u["urls"]) == (1, 2)
    raw = json.dumps(claude_doc)
    assert "app.example.com" not in raw and "fix csp" not in raw and "thanks" not in raw
    assert claude_doc["totals"]["promptImages"] == 1


def test_claude_tasks_fallback_one_per_prompt(claude_doc):
    tasks = claude_doc["tasks"]
    assert [t["n"] for t in tasks] == [1, 2]
    assert all(t["source"] == "turns" for t in tasks)
    assert tasks[0]["startSeq"] == 0 and tasks[-1]["endSeq"] == len(claude_doc["events"]) - 1
    assert {e["task"] for e in claude_doc["events"]} == {1, 2}
    merge = _kinds(claude_doc, "pr_merge")[0]
    assert merge["task"] == 2
    assert claude_doc["totals"]["tasks"] == 2


def test_claude_role_and_summary(claude_doc):
    assert claude_doc["session"]["role"] == "root"
    s = sm.summary(claude_doc)
    assert s["role"] == "root" and s["tasks"] == 2
    assert s["outputs"]["prsMerged"] == 1 and s["outputs"]["commits"] == 1
    assert s["prs"] == [
        {
            "repo": "acme/dashboard",
            "number": 102,
            "url": "https://github.com/acme/dashboard/pull/102",
            "state": "merged",
        }
    ]
    assert json.dumps(s)  # serializable


def test_no_private_keys_leak(claude_doc):
    for e in claude_doc["events"]:
        assert not [k for k in e if k.startswith("_")], e


def test_claude_tasks_from_segmentation_cache(tmp_path, fake_home):
    repo = _repo(tmp_path)
    cwd = str(repo)
    groups = [
        _prompt(0, cwd, "a"),
        _call(1, 5, cwd, "Bash", {"command": "ls"}, "x"),
        _prompt(10, cwd, "b"),
        _call(2, 15, cwd, "Bash", {"command": "ls"}, "x"),
        _prompt(20, cwd, "c"),
        _call(3, 25, cwd, "Bash", {"command": "ls"}, "x"),
    ]
    path = _write(tmp_path, groups)
    tasks = [
        {"n": 1, "name": "Diagnose", "startAt": _ts(0), "firstTurn": 1, "lastTurn": 2},
        {"n": 2, "name": "Fix", "startAt": _ts(20), "firstTurn": 3, "lastTurn": 3},
    ]
    doc = sm.build_claude_code_map(path, tasks=tasks)
    got = [(t["n"], t["name"], t["source"], t["firstTurn"], t["lastTurn"]) for t in doc["tasks"]]
    assert got == [(1, "Diagnose", "segments", 1, 2), (2, "Fix", "segments", 3, 3)]
    last_ls = [e for e in doc["events"] if e.get("tool") == "Bash"][-1]
    assert last_ls["task"] == 2


def test_claude_subagent_launch_keeps_child_model(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    groups = [
        _prompt(0, cwd, "go"),
        _call(1, 5, cwd, "Agent", {"subagent_type": "Explore", "model": "haiku"}, "done"),
    ]
    path = _write(tmp_path, groups)
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc.jsonl").write_text(
        "".join(
            _call(
                9, 6, cwd, "Read", {"file_path": str(repo / "a.py")}, "x", model="claude-haiku-4-5"
            )
        )
    )
    doc = sm.build_claude_code_map(path)
    launch = next(e for e in doc["events"] if e["kind"] == "subagent")
    assert launch["model"] == "haiku"  # the requested child model the viewer shows
    read = next(e for e in doc["events"] if e.get("tool") == "Read")
    assert read["model"] == "claude-haiku-4-5" and read["agent"] != "main"


# ---------------------------------------------------------------- spans (Codex / Cursor)
def _span(span_id, name, start, end=None, parent=None, status="OK", **attrs):
    return SimpleNamespace(
        span_id=span_id,
        name=name,
        start_time=start,
        end_time=end,
        parent_id=parent,
        status_code=status,
        attributes=attrs,
    )


T0 = datetime(2026, 9, 23, 15, 44, tzinfo=UTC)


def _at(sec):
    return T0 + timedelta(seconds=sec)


def test_codex_code_mode_outputs_status_ok_is_not_trusted(tmp_path):
    repo = _repo(tmp_path, "backend", "acme/backend")
    wrap = lambda cmd: (  # noqa: E731
        "const r = await tools.exec_command("
        + json.dumps({"cmd": cmd, "workdir": str(repo)})
        + "); text(r.output);"
    )
    spans = [
        _span("root", "Session", _at(0), **{"harness.agent_session_id": "c1"}),
        _span(
            "t1",
            "Turn 1",
            _at(0),
            _at(60),
            parent="root",
            **{"input.value": "ship it", "llm.model_name": "gpt-6-astra"},
        ),
        _span(
            "a",
            "Bash",
            _at(1),
            _at(2),
            parent="t1",
            **{
                "tool.name": "Bash",
                "input.value": wrap("git commit -m x"),
                "output.value": "Script completed Wall time 0.3 seconds Output: [feat deadbee] x\n 1 file changed, 2 insertions(+)",
            },
        ),
        _span(
            "b",
            "Bash",
            _at(3),
            _at(4),
            parent="t1",
            **{
                "tool.name": "Bash",
                "input.value": wrap("gh pr merge 103 --squash"),
                "output.value": "Script completed Output: X Pull request acme/app#103 is not mergeable",
            },
        ),
        _span(
            "c",
            "save_issue",
            _at(5),
            _at(6),
            parent="t1",
            **{
                "tool.name": "save_issue",
                "input.value": json.dumps({"team": "X", "title": "t"}),
                "output.value": 'Wall time: 0.7 seconds Output: Error: Could not find team "X"',
            },
        ),
        _span(
            "d",
            "save_issue",
            _at(7),
            _at(8),
            parent="t1",
            **{
                "tool.name": "save_issue",
                "input.value": json.dumps({"team": "ENT", "title": "t"}),
                "output.value": 'Output: {"id":"ACME-4800"}',
            },
        ),
        _span(
            "t2",
            "Turn 2",
            _at(60),
            _at(90),
            parent="root",
            **{"input.value": "next", "llm.model_name": "gpt-5.6-terra"},
        ),
        _span(
            "e",
            "js",
            _at(61),
            _at(62),
            parent="t2",
            **{
                "tool.name": "js",
                "input.value": 'text(await tools.exec_command({cmd:"gh pr comment 5 --body hi", workdir:"/w"}));',
                "output.value": "https://github.com/acme/backend/pull/5#issuecomment-9",
            },
        ),
    ]
    doc = sm.build_map_from_spans(
        spans,
        session_id="local:codex:x",
        agent_tool="codex",
        role="subagent",
        parent_session_id="p1",
    )
    assert [(c["sha"], c["repo"]) for c in _kinds(doc, "commit")] == [("deadbee", "backend")]
    assert _kinds(doc, "pr_merge") == []
    assert [(t["tracker"], t["action"], t["id"]) for t in _kinds(doc, "ticket")] == [
        ("linear", "create", "ACME-4800")
    ]
    assert [c.get("number") for c in _kinds(doc, "pr_comment")] == [5]
    commit_call = next(e for e in doc["events"] if e.get("verb") == "git commit")
    assert commit_call["kind"] == "exec"
    assert [(s["from"], s["to"]) for s in _kinds(doc, "model_switch")] == [
        ("gpt-6-astra", "gpt-5.6-terra")
    ]
    assert (doc["session"]["role"], doc["session"]["parentSessionId"]) == ("subagent", "p1")
    assert [t["n"] for t in doc["tasks"]] == [1, 2]


def test_cursor_default_model_mcp_names_and_truncated_input(tmp_path):
    repo = _repo(tmp_path)
    spans = [
        _span("root", "Session", _at(0), **{"llm.model_name": "default"}),
        _span(
            "t1",
            "Turn 1",
            _at(0),
            _at(30),
            parent="root",
            **{"input.value": "see https://x.io", "llm.model_name": ""},
        ),
        _span(
            "a",
            "Bash",
            _at(1),
            _at(2),
            parent="t1",
            **{
                "tool.name": "Bash",
                "cwd": str(repo),
                "input.value": '{"command": "cd ' + str(repo) + ' && git commit -m \\"x',
                "output.value": '{"output":"[fix/a c0ffee1] x\\n 3 files changed, 913 insertions(+)"}',
            },
        ),
        _span(
            "b",
            "mcp",
            _at(3),
            _at(4),
            parent="t1",
            **{
                "tool.name": "mcp-linear-create_issue",
                "input.value": json.dumps({"title": "t"}),
                "output.value": '{"identifier":"ACME-9"}',
            },
        ),
    ]
    doc = sm.build_map_from_spans(spans, session_id="local:cursor:x", agent_tool="cursor")
    assert {e["model"] for e in doc["events"]} == {"default"}
    assert [c["sha"] for c in _kinds(doc, "commit")] == ["c0ffee1"]
    mcp = next(e for e in doc["events"] if e["kind"] == "mcp")
    assert (mcp["server"], mcp["tool"]) == ("linear", "create_issue")
    assert [(t["tracker"], t["id"]) for t in _kinds(doc, "ticket")] == [("linear", "ACME-9")]
    assert _kinds(doc, "user")[0]["urls"] == 1


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("✓ Merged pull request #12 (x)", True),
        ('{"sha":"a","merged":true,"message":"Pull Request successfully merged"}', True),
        (
            "! Pull request #12 will be automatically merged via squash when all requirements are met",
            False,
        ),
        ("X Pull request #12 is not mergeable", False),
        ("! Pull request #12 was already merged", False),
    ],
)
def test_merge_confirmation(text, ok):
    got = sm.confirm_output({"type": "pr_merge", "repo": None, "number": 12}, text, False)
    assert bool(got) is ok


def test_unwrap_command_variants():
    assert sm.unwrap_command('x = tools.exec_command({"cmd":"ls -l","workdir":"/w"})') == (
        "ls -l",
        "/w",
    )
    assert sm.unwrap_command('tools.exec_command({cmd:"git commit -m \\"a\\"", workdir:"/w"})') == (
        'git commit -m "a"',
        "/w",
    )
    assert sm.unwrap_command("git status") == ("git status", None)


# ---------------------------------------------------------------- task cache






def _cand(sid, agent, path, source=None):
    return {"session_id": sid, "agent": agent, "path": str(path), "mtime": 1.0, "_source": source}
















@pytest.mark.parametrize(
    ("text", "err", "extra", "ok"),
    [
        # gh prints its success line only to a TTY: a clean, silent exit IS the merge
        ("(Bash completed with no output)", False, {}, True),
        ("From https://github.com/a/b\n * branch main\nFast-forward\n x.py | 3 +", False, {}, True),
        # merged on GitHub, then gh's local branch switch failed (exit 1): still a merge
        (
            "Exit code 1\nfailed to run git: fatal: 'main' is already used by worktree at '/w'",
            True,
            {},
            True,
        ),
        (
            "X Pull request #5 is not mergeable: the base branch policy prohibits the merge.",
            False,
            {},
            False,
        ),
        ("Exit code 1\nGraphQL: Pull Request is not mergeable", True, {}, False),
        ("! Pull request #5 was already merged", False, {}, False),
        ("", False, {"auto": True}, False),  # --auto only queues it
    ],
)
def test_cli_merge_confirmation_non_tty(text, err, extra, ok):
    intent = {"type": "pr_merge", "repo": None, "number": 5, "cli": True, **extra}
    assert bool(sm.confirm_output(intent, text, err)) is ok


def test_cli_merge_intent_flags_auto():
    [it] = sm.output_intents("Bash", {"command": "gh pr merge 5 --auto --squash"}, None, None)
    assert it["auto"] is True and it["cli"] is True and it["number"] == 5


def test_pr_merge_without_repo_flag_takes_the_checkouts_origin(tmp_path):
    repo = _repo(tmp_path, "backend", "acme/backend")
    cwd = str(repo)
    path = _write(
        tmp_path,
        [
            _prompt(0, cwd, "merge"),
            _call(
                1,
                5,
                cwd,
                "Bash",
                {"command": f"cd {cwd} && gh pr merge 42 --squash"},
                "(Bash completed with no output)",
            ),
        ],
    )
    [m] = _kinds(sm.build_claude_code_map(path), "pr_merge")
    assert (m["repo"], m["number"]) == ("acme/backend", 42)




def test_skill_write_with_ok_status_but_failed_result_is_dropped(tmp_path):
    spans = [
        _span("t1", "Turn 1", _at(0), _at(30), **{"input.value": "x", "llm.model_name": "m"}),
        _span(
            "a",
            "Edit",
            _at(1),
            _at(2),
            parent="t1",
            **{
                "tool.name": "Edit",
                "input.value": json.dumps(
                    {
                        "file_path": "/h/.claude/skills/s1/SKILL.md",
                        "old_string": "a",
                        "new_string": "b",
                    }
                ),
                "output.value": "<tool_use_error>String to replace not found in file.</tool_use_error>",
            },
        ),
        _span(
            "b",
            "Edit",
            _at(3),
            _at(4),
            parent="t1",
            **{
                "tool.name": "Edit",
                "input.value": json.dumps(
                    {
                        "file_path": "/h/.claude/skills/s2/SKILL.md",
                        "old_string": "a",
                        "new_string": "b",
                    }
                ),
                "output.value": "The file /h/.claude/skills/s2/SKILL.md has been updated successfully.",
            },
        ),
    ]
    doc = sm.build_map_from_spans(spans, session_id="local:codex:x", agent_tool="codex")
    assert [s["name"] for s in _kinds(doc, "skill_write")] == ["s2"]


def test_pr_state_requires_the_exact_repo(tmp_path):
    repo = _repo(tmp_path, "service", "org-a/service")
    cwd = str(repo)
    path = _write(
        tmp_path,
        [
            _prompt(0, cwd, "x"),
            _call(
                1,
                5,
                cwd,
                "Bash",
                {"command": "gh pr create -t t -b b"},
                "https://github.com/org-a/service/pull/7",
            ),
            _call(
                2,
                9,
                cwd,
                "Bash",
                {"command": "gh pr merge 7 --repo org-b/service --squash"},
                "(Bash completed with no output)",
            ),
        ],
    )
    doc = sm.build_claude_code_map(path)
    assert _kinds(doc, "pr_merge")[0]["repo"] == "org-b/service"
    assert doc["prs"][0]["state"] == "open"


def test_codex_failed_tool_spans_get_an_error_class():
    spans = [
        _span("root", "Session", _at(0), **{"harness.agent_session_id": "c2"}),
        _span("t1", "Turn 1", _at(0), _at(60), parent="root", **{"input.value": "go"}),
        _span(
            "a", "Bash", _at(1), _at(2), parent="t1", status="ERROR",
            **{
                "tool.name": "Bash",
                "input.value": json.dumps({"command": "pnpm test"}),
                "output.value": "Chunk ID: 1\nWall time: 2 seconds\nProcess exited with code 1\nOutput:\nFAIL",
            },
        ),
        _span(
            "b", "apply_patch", _at(3), _at(4), parent="t1", status="ERROR",
            **{
                "tool.name": "apply_patch",
                "input.value": "{}",
                "output.value": "apply_patch verification failed: Failed to find expected lines in a.py",
            },
        ),
        _span(
            "c", "Bash", _at(5), _at(6), parent="t1", status="ERROR",
            **{
                "tool.name": "Bash",
                "input.value": json.dumps({"command": "pnpm lint"}),
                "output.value": "Script completed\nWall time 0.4 seconds\nOutput:\nlint failed",
                "process.exit_code": 2,
            },
        ),
    ]
    doc = sm.build_map_from_spans(spans, session_id="local:codex:y", agent_tool="codex")
    errs = [e for e in doc["events"] if e.get("err")]
    assert [(e["errClass"], e.get("exitCode")) for e in errs] == [("exit_code", 1), ("patch_failed", None), ("exit_code", 2)]


def test_codex_rollout_exit_codes_survive_output_truncation():
    from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex

    long = "x" * 3000
    calls = [
        # Code mode: the exit code lives in the structured result, not the text.
        ("cm", "custom_tool_call", "exec", 'text(await tools.exec_command({cmd:"pnpm lint"}));',
         [{"type": "text", "text": json.dumps({"output": long, "exit_code": 2, "wall_time_seconds": 1})}]),
        # apply_patch JSON: metadata.exit_code comes after a long output text.
        ("ap", "function_call", "apply_patch", json.dumps({"input": "*** Begin Patch\n*** End Patch"}),
         json.dumps({"output": long + " not found", "metadata": {"exit_code": 3}})),
        # exec_command text: the exit line sits after the header lines.
        ("ex", "function_call", "exec_command", json.dumps({"cmd": "pytest"}),
         "Chunk ID: 1\nWall time: 1 seconds\nProcess exited with code 1\nOutput:\n" + long),
    ]
    rows = [
        {"type": "session_meta", "payload": {"id": "s", "cwd": "/repo"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "go"}]}},
    ]
    for cid, kind, name, args, out in calls:
        key = "input" if kind == "custom_tool_call" else "arguments"
        rows.append({"type": "response_item", "payload": {"type": kind, "name": name, "call_id": cid, key: args}})
        rows.append({"type": "response_item", "payload": {"type": kind + "_output", "call_id": cid, "output": out}})
    for i, r in enumerate(rows):
        r["timestamp"] = f"2026-09-21T01:00:{i:02d}Z"
    _, spans = parse_codex(data="\n".join(json.dumps(r) for r in rows).encode(), trace_id="local:codex:s")
    doc = sm.build_map_from_spans(spans, session_id="local:codex:s", agent_tool="codex")
    errs = [(e["errClass"], e.get("exitCode")) for e in doc["events"] if e.get("err")]
    assert errs == [("exit_code", 2), ("patch_failed", None), ("exit_code", 1)]


@pytest.mark.parametrize(
    ("agent", "fixture", "parse", "codes"),
    [
        ("gemini-cli", "gemini/failed_shell.jsonl", "gemini:parse_gemini", {1}),
        ("command-code", "command_code/failed_shell.jsonl", "command_code:parse_command_code", {128}),
        # A Windows crash code (0xC0000005) still counts as an exit code.
        ("command-code-win", "command_code/failed_shell.jsonl", "command_code:parse_command_code", {3221225477}),
        ("antigravity-cli", "antigravity/failed_command.jsonl", "antigravity:parse_antigravity", {1}),
    ],
)
def test_span_agents_keep_exit_codes_past_long_output(agent, fixture, parse, codes):
    import importlib
    from pathlib import Path

    mod, fn = parse.split(":")
    parser = getattr(
        importlib.import_module(f"agentplay._core.ingestion.coding_agent._sources.local.parsers.{mod}"), fn
    )
    data = (Path(__file__).parent / "core" / "fixtures" / fixture).read_bytes()
    # Program output long enough to push a trailing exit line past truncation,
    # full of words that would otherwise pick a wrong class.
    noisy = "FAIL test_timeout.py rate limit not found Permission denied " + "x" * 2100
    data = data.replace(b"OUTPUT-PLACEHOLDER", noisy.encode())
    if agent.endswith("-win"):
        data = data.replace(b"Exit code: 128", b"Exit code: 3221225477")
    _, spans = parser(data, trace_id=f"local:{agent}:t")
    doc = sm.build_map_from_spans(spans, session_id=f"local:{agent}:t", agent_tool=agent)
    errs = [e for e in doc["events"] if e.get("err") and e.get("kind") != "error"]
    assert errs and {e["errClass"] for e in errs} == {"exit_code"}
    assert {e["exitCode"] for e in errs} == codes


def test_codex_code_mode_poll_keeps_its_exit_code():
    from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex

    calls = [
        ("launch", 'text(await tools.exec_command({cmd:"pytest"}));',
         {"output": "", "session_id": 77, "wall_time_seconds": 1}),
        ("poll", 'text(await tools.write_stdin({session_id:77, chars:""}));',
         {"output": "httpx.ReadTimeout: timeout", "exit_code": 1, "wall_time_seconds": 1}),
    ]
    rows = [
        {"type": "session_meta", "payload": {"id": "p", "cwd": "/repo"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "go"}]}},
    ]
    for cid, code, result in calls:
        rows.append({"type": "response_item",
                     "payload": {"type": "custom_tool_call", "name": "exec", "call_id": cid, "input": code}})
        rows.append({"type": "response_item",
                     "payload": {"type": "custom_tool_call_output", "call_id": cid,
                                 "output": [{"type": "text", "text": json.dumps(result)}]}})
    for i, r in enumerate(rows):
        r["timestamp"] = f"2026-09-21T01:00:{i:02d}Z"
    _, spans = parse_codex(data="\n".join(json.dumps(r) for r in rows).encode(), trace_id="local:codex:p")
    doc = sm.build_map_from_spans(spans, session_id="local:codex:p", agent_tool="codex")
    errs = [(e["errClass"], e.get("exitCode")) for e in doc["events"] if e.get("err")]
    assert errs and set(errs) == {("exit_code", 1)}


def test_codex_polled_process_exiting_minus_one_fails_its_launch():
    from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex

    calls = [
        ("launch", "exec_command", {"cmd": "pnpm dev"}, "Chunk ID: 1\nProcess running with session ID 5\nOutput:\n"),
        ("poll", "write_stdin", {"session_id": 5, "chars": ""},
         "Chunk ID: 2\nWall time: 1 seconds\nProcess exited with code -1\nOutput:\nboom"),
    ]
    rows = [
        {"type": "session_meta", "payload": {"id": "m", "cwd": "/repo"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "go"}]}},
    ]
    for cid, name, args, out in calls:
        rows.append({"type": "response_item", "payload": {"type": "function_call", "name": name,
                                                          "call_id": cid, "arguments": json.dumps(args)}})
        rows.append({"type": "response_item", "payload": {"type": "function_call_output", "call_id": cid,
                                                          "output": out}})
    for i, r in enumerate(rows):
        r["timestamp"] = f"2026-09-21T01:00:{i:02d}Z"
    _, spans = parse_codex(data="\n".join(json.dumps(r) for r in rows).encode(), trace_id="local:codex:m")
    doc = sm.build_map_from_spans(spans, session_id="local:codex:m", agent_tool="codex")
    errs = [(e["errClass"], e.get("exitCode")) for e in doc["events"] if e.get("err")]
    assert len(errs) == 2 and set(errs) == {("exit_code", -1)}
