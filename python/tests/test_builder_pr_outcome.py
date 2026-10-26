"""Tests for PR-creation / outcome / tool-category / file-op detection in builder.py.

Scope: gh/glab/GitHub-API/MCP PR creation detection (URL-
confirmed only), a closed-vocabulary tool-call category per event, file create/modify/
delete/rename detection, and a per-agent/session outcome label.
"""

from __future__ import annotations

import json

import pytest

from agentplay import builder as sm


def _line(**d):
    return json.dumps(d) + "\n"


def _repo(tmp_path, name="backend", remote="acme/backend"):
    repo = tmp_path / "work" / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = git@github.com:{remote}.git\n'
    )
    return repo


def _bash(seq_id, ts, cwd, command, result, *, is_error=False):
    return [
        _line(
            type="assistant",
            timestamp=ts[0],
            cwd=cwd,
            message={
                "id": "m" + seq_id,
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t" + seq_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        ),
        _line(
            type="user",
            timestamp=ts[1],
            cwd=cwd,
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t" + seq_id,
                        "content": result,
                        "is_error": is_error,
                    }
                ],
            },
        ),
    ]


def _write_session(tmp_path, lines, name="s"):
    path = tmp_path / "proj" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line for group in lines for line in group))
    return path


# ---------------------------------------------------------------- PR creation
def test_successful_pr_create_resolves_from_url(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = [
        _bash(
            "1",
            ("2026-09-10T01:00:00Z", "2026-09-10T01:00:02Z"),
            cwd,
            "gh pr create --repo acme/backend --base main --head feat/x "
            '--title "feat: add the thing" --body "## Why\\n\\nBecause it helps.\\n\\n- point one\\n- point two"',
            "https://github.com/acme/backend/pull/101",
        )
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    ev = next(e for e in doc["events"] if e.get("vcs", {}).get("action") == "pr_create")
    assert ev["vcs"] == {
        "provider": "github",
        "action": "pr_create",
        "repo": "acme/backend",
        "number": 101,
    }
    assert doc["prs"] == [
        {
            "provider": "github",
            "repo": "acme/backend",
            "number": 101,
            "url": "https://github.com/acme/backend/pull/101",
            "seq": ev["seq"],
            "state": "open",
        }
    ]
    assert doc["totals"]["prsCreated"] == 1
    assert doc["session"]["outcome"] == "pr_created"


def test_failed_pr_create_is_not_counted_as_created(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = [
        _bash(
            "1",
            ("2026-09-10T01:00:00Z", "2026-09-10T01:00:02Z"),
            cwd,
            'gh pr create --repo acme/backend --title "x" --body "some free text with a 1 in it"',
            "pull request create failed: GraphQL: already exists",
            is_error=True,
        )
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    ev = next(e for e in doc["events"] if e.get("vcs"))
    assert ev["vcs"]["action"] == "pr_create_failed"
    assert doc["prs"] == [] and doc["totals"]["prsCreated"] == 0
    assert doc["session"]["outcome"] != "pr_created"


def test_pr_create_with_no_url_in_output_is_an_attempt_not_a_success(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = [
        _bash(
            "1",
            ("2026-09-10T01:00:00Z", "2026-09-10T01:00:02Z"),
            cwd,
            'gh pr create --repo acme/backend --title "x" --body "free text mentioning 5 things"',
            "Opening in your browser.",
        )
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    ev = next(e for e in doc["events"] if e.get("vcs"))
    assert ev["vcs"]["action"] == "pr_create_attempt"
    assert doc["prs"] == []


def test_ordinary_gh_pr_commands_never_read_as_create(tmp_path):
    """A generic noun/verb pair like ('pr','create') from vcs_action must not leak the
    literal sentinel 'pr_create' when pr_create_intent finds no create shape at all —
    regression for the collision between the generic label and the resolved-PR marker."""
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = (
        _bash(
            "1",
            ("2026-09-10T01:00:00Z", "2026-09-10T01:00:02Z"),
            cwd,
            "gh pr view 101",
            '{"state":"OPEN"}',
        )
        + _bash(
            "2",
            ("2026-09-10T01:00:03Z", "2026-09-10T01:00:04Z"),
            cwd,
            "gh pr checks 101",
            "all pass",
        )
        + _bash(
            "3",
            ("2026-09-10T01:00:05Z", "2026-09-10T01:00:06Z"),
            cwd,
            "gh pr merge 101 --merge",
            "Merged",
        )
    )
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    actions = [e["vcs"]["action"] for e in doc["events"] if e.get("vcs")]
    assert actions == ["pr_view", "pr_checks", "pr_merge"]
    assert all(e["vcs"].get("number") == 101 for e in doc["events"] if e.get("vcs"))
    assert "pr_create" not in actions


def test_interrupted_pr_create_never_got_a_result(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = [
        _line(
            type="assistant",
            timestamp="2026-09-10T01:00:00Z",
            cwd=cwd,
            message={
                "id": "m1",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {
                            "command": 'gh pr create --repo acme/backend --title "x" --body "y"'
                        },
                    }
                ],
            },
        )
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    ev = next(e for e in doc["events"] if e.get("tool") == "Bash")
    assert ev["err"] is True and ev["errClass"] == "no_result"
    assert ev["vcs"]["action"] == "pr_create_failed"
    assert doc["prs"] == []


def test_bitbucket_and_gitlab_create_detected_by_shape(tmp_path):
    repo = _repo(tmp_path, "svc", "myorg/svc")
    cwd = str(repo)
    lines = _bash(
        "1",
        ("2026-09-10T01:00:00Z", "2026-09-10T01:00:02Z"),
        cwd,
        "glab mr create --title x --description y",
        "https://gitlab.com/myorg/svc/-/merge_requests/12",
    ) + _bash(
        "2",
        ("2026-09-10T01:00:03Z", "2026-09-10T01:00:04Z"),
        cwd,
        "bb pr create --title x",
        "https://bitbucket.org/myorg/svc/pull-requests/7",
    )
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    assert {(p["provider"], p["number"]) for p in doc["prs"]} == {("gitlab", 12), ("bitbucket", 7)}


def test_mcp_pr_tool_sets_vcs_category(tmp_path):
    cwd = str(_repo(tmp_path))
    lines = [
        _line(
            type="assistant",
            timestamp="2026-09-10T01:00:00Z",
            cwd=cwd,
            message={
                "id": "m1",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "mcp__github__create_pull_request",
                        "input": {"owner": "acme", "repo": "backend", "title": "x"},
                    }
                ],
            },
        ),
        _line(
            type="user",
            timestamp="2026-09-10T01:00:02Z",
            cwd=cwd,
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "https://github.com/acme/backend/pull/102",
                    }
                ],
            },
        ),
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    ev = next(e for e in doc["events"] if e["kind"] == "mcp")
    assert ev["cat"] == "vcs" and ev["vcs"]["action"] == "pr_create" and ev["vcs"]["number"] == 102


_PR_URL_OUT = "https://github.com/acme/backend/pull/7"


def _one_bash_call(tmp_path, command, result, *, is_error=False):
    cwd = str(_repo(tmp_path))
    times = ("2026-09-10T01:00:00Z", "2026-09-10T01:00:02Z")
    lines = [_bash("1", times, cwd, command, result, is_error=is_error)]
    return sm.build_claude_code_map(_write_session(tmp_path, lines))


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --fill",
        "export GH_TOKEN=$(gh auth token --user bob) && gh pr create --fill",
        "git push -q origin feat/x\ngh pr create --fill",
        "GH_TOKEN=$(gh auth token --user bob) gh pr create --title SECRET-TITLE --body x",
        "cd /tmp && GH_TOKEN=$(gh auth token --user bob) gh pr create -R acme/backend "
        '--title "SECRET-TITLE" --body "runs `make`" 2>&1 | tail -2',
        "git push -u origin feat/x 2>&1 | tail -3; gh pr create --fill",
        "git diff --quiet || gh pr create --fill",
        "env GH_TOKEN=x gh pr create --fill",
        "cat > /tmp/b.md <<'EOF'\nIt's done\nEOF\ngh pr create --body-file /tmp/b.md | tail -1",
    ],
)
def test_pr_create_found_anywhere_in_a_compound_command(tmp_path, command):
    doc = _one_bash_call(tmp_path, command, _PR_URL_OUT)
    assert [(p["repo"], p["number"], p["url"]) for p in doc["prs"]] == [
        ("acme/backend", 7, _PR_URL_OUT)
    ]
    assert doc["totals"]["prsCreated"] == 1
    assert doc["session"]["outcome"] == "pr_created"
    assert "SECRET-TITLE" not in json.dumps(doc)


@pytest.mark.parametrize(
    ("command", "result"),
    [
        ('echo "gh pr create --fill" && gh pr view 7', _PR_URL_OUT),
        ("cat > /tmp/notes.md <<'EOF'\ngh pr create --fill\nEOF\ngh pr view 7", _PR_URL_OUT),
        (
            "gh pr create --dry-run --fill",
            f"Would have created a Pull Request with:\nbody: see {_PR_URL_OUT}",
        ),
        ("gh pr create --help", "Create a pull request on GitHub."),
        (
            "gh pr create --fill 2>&1 | tail -2",
            'a pull request for branch "feat/x" into branch "main" already exists:\n' + _PR_URL_OUT,
        ),
        (
            "gh pr create --fill 2>&1 | tail -2",
            "GraphQL: A pull request already exists for acme:feat/x\n" + _PR_URL_OUT,
        ),
        ("false && gh pr create --fill; gh pr view 7 --json url --jq .url", _PR_URL_OUT),
        ("gh pr create --fill || gh pr view 7 --json url --jq .url", _PR_URL_OUT),
        ("gh pr view 3; gh pr create --fill", f"{_PR_URL_OUT[:-1]}3\n{_PR_URL_OUT}"),
        ("echo see && gh pr create --fill", f"see {_PR_URL_OUT[:-1]}3\n{_PR_URL_OUT}"),
        ("cat >\"/tmp/n\"<<'EOF'\ngh pr create --fill\nEOF\ngh pr view 7 --json url", _PR_URL_OUT),
        ("cat > /tmp/notes.md <<'EOF'\ngh pr create --fill\nEOF\ncat /tmp/url.txt", _PR_URL_OUT),
    ],
)
def test_pr_create_text_dry_run_or_refusal_is_not_a_created_pr(tmp_path, command, result):
    doc = _one_bash_call(tmp_path, command, result)
    assert doc["prs"] == [] and doc["totals"]["prsCreated"] == 0
    assert doc["session"]["outcome"] != "pr_created"


# ---------------------------------------------------------------- commits
_COMMIT_KEYS = {"t", "seq", "kind", "agent", "model", "task", "err", "ref"} | {
    "repo",
    "sha",
    "files",
    "ins",
    "del",
}


@pytest.mark.parametrize(
    ("command", "result", "commits"),
    [
        ('git commit -q -m "SECRET-MSG"', "", 1),
        ('git commit -qm "SECRET-MSG"', "(Bash completed with no output)", 1),
        ('git commit -qam "SECRET-MSG"', "", 1),
        ('git commit -a -q -m "SECRET-MSG"', "", 1),
        ("git commit --amend --no-edit -q", "", 1),
        ('git commit --no-verify -q -m "SECRET-MSG"', "", 1),
        ('git -C /tmp commit -q -m "SECRET-MSG"', "", 1),
        ('git add -A && git commit -q -m "SECRET-MSG" && git log --oneline -1', "1a2b3c4 x", 1),
        ('CODEX_SKIP_REVIEW=1 git commit -q -m "SECRET-MSG" && git push -q', "", 1),
        ('npx tsc --noEmit; git add -A && git commit -q -m "SECRET-MSG"', "", 1),
        ("export A=$(gh auth token) && git commit -q -m SECRET-MSG && git log | head -1", "x", 1),
        ("git add -A\ngit commit -q -m SECRET-MSG", "", 1),
        ("git commit -q -m \"$(cat <<'EOF'\nSECRET-MSG\nEOF\n)\"", "", 1),
        ("git commit -qm a && git add b && git commit -qm SECRET-MSG", "", 2),
        ('git commit -q -m "SECRET-MSG" && git status', "nothing to commit, working tree clean", 1),
        ('git commit -m "SECRET-MSG"', "[main 75f3d6d9a] x\n 1 file changed, 2 insertions(+)", 1),
    ],
)
def test_commit_counted_from_its_summary_or_quiet_clean_exit(tmp_path, command, result, commits):
    doc = _one_bash_call(tmp_path, command, result)
    found = [e for e in doc["events"] if e["kind"] == "commit"]
    assert doc["totals"]["commits"] == len(found) == commits
    assert all(e["repo"] == "backend" and set(e) <= _COMMIT_KEYS for e in found)
    assert "SECRET-MSG" not in json.dumps(doc)


@pytest.mark.parametrize(
    ("command", "result"),
    [
        ('git commit -m "SECRET-MSG" && git push -q', ""),
        ("git commit -mq", ""),
        ('git commit -q -m "SECRET-MSG"; echo done', "done"),
        ('git commit -q -m "SECRET-MSG" && git push -q; echo done', "done"),
        ('git diff --cached --quiet || git commit -q -m "SECRET-MSG"', ""),
        ('git commit -q -m "SECRET-MSG" || true', ""),
        ('git commit -q -m "SECRET-MSG" | tail -1', ""),
        ("git commit -q -m SECRET-MSG\ngit push -q", ""),
        ("git commit -q -m SECRET-MSG", "Command running in background with ID: b1."),
        ('git commit --dry-run -m "SECRET-MSG"', "On branch main\nChanges to be committed:"),
        ("git commit --help", "GIT-COMMIT(1)"),
        ('echo "git commit -q -m SECRET-MSG"', "git commit -q -m SECRET-MSG"),
        ("cat > /tmp/notes.md <<'EOF'\ngit commit -q -m SECRET-MSG\nEOF", ""),
    ],
)
def test_commit_not_counted_when_nothing_proves_it_ran(tmp_path, command, result):
    doc = _one_bash_call(tmp_path, command, result)
    assert doc["totals"]["commits"] == 0


def test_failed_quiet_commit_is_not_counted(tmp_path):
    doc = _one_bash_call(
        tmp_path,
        "git add -A && git commit -q -m x && git push -q",
        "Exit code 1\nOn branch main\nnothing to commit, working tree clean",
        is_error=True,
    )
    assert doc["totals"]["commits"] == 0


# ---------------------------------------------------------------- shell verbs
@pytest.mark.parametrize(
    ("command", "verb"),
    [
        # Setup prefixes hand the verb to what follows, through any sequence operator.
        ("cd /x && make setup", "make setup"),
        ("cd /x; make setup", "make setup"),
        ("export AGENTPLAY_HOME=/x && python -m agentplay build", "python"),
        ("export FOO=1; pytest -q", "pytest"),
        ("cd /x && export Y=1 && pnpm dev", "pnpm dev"),
        # A newline sequences commands just as `;` does.
        ("cd /x\nmake setup", "make setup"),
        ("cd /x\nexport Y=1\npnpm dev", "pnpm dev"),
        # Shell keywords are not commands; the body's verb is. `do`/`then` may follow
        # a `;` or a newline, and blocks nest.
        ("for i in $(seq 1 60); do curl -s x; done", "curl"),
        ("if [ -f x ]; then rm x; fi", "rm"),
        ("for i in 1 2 3\ndo\n  curl x\ndone", "curl"),
        ("for i in 1 2; do if [ -f x ]; then echo up; fi; done", "echo"),
        # `$(...)` and backticks run their own commands; their words aren't this verb.
        ('P=$(uv tool dir)/agentplay; find $P -name "*.py"', "find"),
        ("M=$(ls /x | head -1)\npython3 -c 'import json'", "python3"),
        ("D=`date`; echo $D", "echo"),
        # A pipe is not a sequence: the head still owns the verb.
        ("cat x | grep y", "cat"),
        # A separator inside quotes belongs to an argument, not to the shell. Splitting
        # on it hands the verb to a fragment of that argument — here, of a path.
        ('cd "/private;token" && pytest', "pytest"),
        ("cd '/private;token' && pytest", "pytest"),
        ("cd /x; echo 'a&&b'", "echo"),
        ("cd /x\\;token && pytest", "pytest"),  # a backslash quotes the next character
        # Same rule for the `do`/`then` that opens a block body.
        ('if [ -f "a;then token" ]; then pytest -q; fi', "pytest"),
        ("for i in 'a;do token'; do curl -s x; done", "curl"),
        # Unchanged behaviour.
        ("pytest -q", "pytest"),
        ("AGENTPLAY_HOME=/x pytest -q", "pytest"),
        ("git status", "git status"),
        ("cd /x", "cd"),
        ("export FOO=1", "export"),
    ],
)
def test_shell_verb_skips_setup_prefixes_and_block_keywords(command, verb):
    assert sm._shell_verb(command) == verb


# ---------------------------------------------------------------- tool categories
@pytest.mark.parametrize(
    ("command", "cat"),
    [
        ("pytest -x tests/", "test"),
        # `_VERIFY_RE` (the base classifier, unchanged) buckets test/build/lint together as
        # kind="verify" — CAT labels that bucket "Test / verify", so a build lands there too.
        ("npm run build", "test"),
        ("npm install", "package"),
        ("aws s3 ls", "cloud"),
        ("curl -s https://x", "http"),
        ("rm -rf build/", "fs"),
        ("cat README.md", "file"),
        ("echo hi", "shell"),
        ("git status", "vcs"),
    ],
)
def test_tool_category_from_shell_verb(command, cat):
    kind, paths, touch, verb = sm.classify("Bash", {"command": command}, "/tmp")
    ev = {"kind": kind, "tool": "Bash", "verb": verb}
    assert sm.tool_category(ev) == cat


def test_tool_category_mcp_vcs_vs_other():
    assert sm.tool_category({"kind": "mcp", "server": "github"}) == "vcs"
    assert sm.tool_category({"kind": "mcp", "server": "claude-in-chrome"}) == "mcp"


# ---------------------------------------------------------------- file ops
def test_write_to_new_path_is_create_existing_path_is_modify(tmp_path):
    repo = _repo(tmp_path)
    existing = repo / "README.md"
    existing.write_text("hi\n")
    cwd = str(repo)
    lines = [
        _line(
            type="assistant",
            timestamp="2026-09-10T01:00:00Z",
            cwd=cwd,
            message={
                "id": "m1",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": str(repo / "new_file.py"), "content": "x"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Write",
                        "input": {"file_path": str(existing), "content": "y"},
                    },
                ],
            },
        ),
        _line(
            type="user",
            timestamp="2026-09-10T01:00:01Z",
            cwd=cwd,
            message={
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
                ],
            },
        ),
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    by_path = {f["path"]: f["op"] for f in doc["files"]}
    assert by_path["new_file.py"] == "create" and by_path["README.md"] == "modify"
    assert doc["totals"]["filesCreated"] == 1 and doc["totals"]["filesDeleted"] == 0


def test_shell_rm_mv_touch_detected(tmp_path):
    repo = _repo(tmp_path)
    (repo / "old.py").write_text("x\n")
    cwd = str(repo)
    lines = [
        _bash(
            "1",
            ("2026-09-10T01:00:00Z", "2026-09-10T01:00:01Z"),
            cwd,
            "mv old.py new.py && touch fresh.py && rm stale.py",
            "",
        )
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    by_path = {f["path"]: f["op"] for f in doc["files"]}
    assert by_path["old.py"] == "rename-from" and by_path["new.py"] == "rename-to"
    assert by_path["fresh.py"] == "create" and by_path["stale.py"] == "delete"


def test_apply_patch_add_delete_update(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    patch = (
        f"*** Add File: {repo}/a.py\n+x\n"
        f"*** Delete File: {repo}/b.py\n"
        f"*** Update File: {repo}/c.py\n-x\n+y\n"
    )
    lines = [
        _line(
            type="assistant",
            timestamp="2026-09-10T01:00:00Z",
            cwd=cwd,
            message={
                "id": "m1",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "apply_patch",
                        "input": {"input": patch},
                    }
                ],
            },
        ),
        _line(
            type="user",
            timestamp="2026-09-10T01:00:01Z",
            cwd=cwd,
            message={
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "applied"}],
            },
        ),
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    by_path = {f["path"]: f["op"] for f in doc["files"]}
    assert by_path == {"a.py": "create", "b.py": "delete", "c.py": "modify"}


def test_shell_redirect_creates_file_but_numeric_comparison_does_not(tmp_path):
    """`> 1` inside a numeric/string test (`[ "$rc" > 1 ]`) must never be read as a
    redirect to a file literally named '1' — the two are only distinguishable by this
    kind of conservative guard, not by the redirect grammar alone."""
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = [
        _bash(
            "1",
            ("2026-09-10T01:00:00Z", "2026-09-10T01:00:01Z"),
            cwd,
            'echo hi > out.log\nrc=$?\nif [ "$rc" > 1 ]; then\n  echo bad\nfi',
            "",
        )
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    paths = {f["path"] for f in doc["files"]}
    assert "out.log" in paths
    assert "1" not in paths


def test_fstring_alignment_spec_not_read_as_redirect(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = [
        _bash(
            "1",
            ("2026-09-10T01:00:00Z", "2026-09-10T01:00:01Z"),
            cwd,
            "python3 -c \"print(f'{x:>7.0f} rows')\"",
            "  12 rows",
        )
    ]
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    assert doc["files"] == []


# ---------------------------------------------------------------- outcome
def test_outcome_verified_when_last_verify_has_no_later_edit(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = _bash(
        "1", ("2026-09-10T01:00:00Z", "2026-09-10T01:00:01Z"), cwd, "pytest -q", "12 passed"
    )
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    assert doc["session"]["outcome"] == "verified"


def test_outcome_unresolved_errors_when_session_ends_on_failures(tmp_path):
    repo = _repo(tmp_path)
    cwd = str(repo)
    lines = _bash(
        "1",
        ("2026-09-10T01:00:00Z", "2026-09-10T01:00:01Z"),
        cwd,
        "pytest -q",
        "3 failed",
        is_error=True,
    ) + _bash(
        "2",
        ("2026-09-10T01:00:02Z", "2026-09-10T01:00:03Z"),
        cwd,
        "pytest -q -k thing",
        "1 failed",
        is_error=True,
    )
    doc = sm.build_claude_code_map(_write_session(tmp_path, lines))
    assert doc["session"]["outcome"] == "unresolved_errors"


def test_outcome_incomplete_on_empty_session():
    assert sm.classify_outcome([], "") == "incomplete"
