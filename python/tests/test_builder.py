"""Builder: redaction and document shape."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentplay import builder as sm
from agentplay import facts, search

SECRET_PROMPT = "please fix the SECRET-PROMPT-TEXT in billing"
SECRET_ARG = "--token=hunter2-SECRET-ARG"
# Shell lines whose quoted or substituted values once leaked into `verb`: a naive
# whitespace split handed a fragment of the value to the verb. Values are fake.
LEAKY_COMMANDS = (
    ('AUTH="Bearer sk-ant-FAKE123456" curl https://x', "curl"),
    ('F="/Users/me/Application Support/secret-plan.pdf"; open "$F"', "open"),
    ('MSG="fix secret stuff" git commit', "git commit"),
    ('x=$(gh api repos/acme/LEAKREPO --jq ".a | select(.b == \\"c d\\")") && echo "$x"', "echo"),
    ("A=$'line one\\nANSI-SECRET two' make build", "make build"),
    ("env TOKEN='abc ENV-SECRET' FOO=1 npm test", "npm test"),
    ("sudo -u sudo-secret-user systemctl restart x", "systemctl"),
    ('cd "/x y/CD-SECRET" && A="p QUOTED-SECRET" pytest -q', "pytest"),
    ('"$SECRET_BIN" --flag', "sh"),
    ("$(which SUBST-SECRET) run.py", "sh"),
    ("leak(){ echo FN-SECRET; }; leak", "sh"),
    ('echo "never closes SECRET-OPEN', "echo"),
)


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


@pytest.fixture()
def transcript(tmp_path):
    repo = tmp_path / "work" / "backend"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:acme/backend.git\n'
    )
    (repo / "billing_core").mkdir()
    (repo / "billing_core" / "controller.py").write_text("x = 1\n")
    (repo / "README.md").write_text("hi\n")
    cwd = str(repo)
    ts = "2026-09-10T01:00:0{}Z"
    lines = [
        _line(
            type="user",
            timestamp=ts.format(0),
            cwd=cwd,
            message={"role": "user", "content": SECRET_PROMPT},
        ),
        _line(
            type="assistant",
            timestamp=ts.format(1),
            cwd=cwd,
            message={
                "id": "m1",
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 1000,
                    "output_tokens": 50,
                },
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": f"{cwd}/billing_core/controller.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": f"cd {cwd} && git log -5 {SECRET_ARG} README.md"},
                    },
                    *(
                        {"type": "tool_use", "id": f"tl{i}", "name": "Bash", "input": {"command": c}}
                        for i, (c, _) in enumerate(LEAKY_COMMANDS)
                    ),
                    {
                        "type": "tool_use",
                        "id": "t3",
                        "name": "mcp__claude-in-chrome__navigate",
                        "input": {"url": "https://secret.example/x?token=abc"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t4",
                        "name": "Skill",
                        "input": {"skill": "entloop", "args": SECRET_ARG},
                    },
                    {
                        "type": "tool_use",
                        "id": "t5",
                        "name": "Agent",
                        "input": {
                            "description": "SECRET-SUBAGENT-DESC",
                            "prompt": SECRET_PROMPT,
                            "subagent_type": "Explore",
                        },
                    },
                ],
            },
        ),
        _line(
            type="user",
            timestamp=ts.format(2),
            cwd=cwd,
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "file body SECRET-OUTPUT",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": "log",
                        "is_error": True,
                    },
                    {"type": "tool_result", "tool_use_id": "t3", "content": "ok"},
                    {"type": "tool_result", "tool_use_id": "t4", "content": "ok"},
                    {"type": "tool_result", "tool_use_id": "t5", "content": "done"},
                ],
            },
            toolUseResult={"agentId": "abc123def456789"},
        ),
        _line(
            type="assistant",
            timestamp=ts.format(3),
            cwd=cwd,
            message={
                "id": "m2",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 5, "output_tokens": 5},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t6",
                        "name": "Edit",
                        "input": {
                            "file_path": f"{cwd}/billing_core/controller.py",
                            "old_string": "x",
                            "new_string": "SECRET-EDIT",
                        },
                    }
                ],
            },
        ),
        _line(
            type="user",
            timestamp=ts.format(4),
            cwd=cwd,
            message={
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t6", "content": "ok"}],
            },
        ),
    ]
    path = tmp_path / "proj" / "11111111-2222-3333-4444-555555555555.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(lines))
    sub = path.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc123def456789.jsonl").write_text(
        _line(
            type="assistant",
            timestamp=ts.format(2),
            cwd=cwd,
            isSidechain=True,
            message={
                "id": "s1",
                "model": "claude-haiku-4-5-20251001",
                "usage": {"input_tokens": 7, "output_tokens": 3},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "u1",
                        "name": "Grep",
                        "input": {"pattern": "SECRET-PATTERN", "path": f"{cwd}/billing_core"},
                    }
                ],
            },
        )
    )
    return path


# ---------------------------------------------------------------- builder


def test_acompact_snapshot_is_not_a_subagent_lane(transcript):
    before = sm.build_claude_code_map(transcript)
    parent_llm = next(
        json.loads(line)
        for line in transcript.read_text().splitlines()
        if json.loads(line).get("type") == "assistant"
    )
    (transcript.with_suffix("") / "subagents" / "agent-acompact-0a1b2c3d.jsonl").write_text(
        _line(**{**parent_llm, "isSidechain": True})
    )
    doc = sm.build_claude_code_map(transcript)
    assert [a["id"] for a in doc["agents"]] == [a["id"] for a in before["agents"]]
    assert not any("acompact" in a["id"] for a in doc["agents"])
    assert doc["totals"]["subagents"] == 1
    assert doc["totals"]["tokens"] == before["totals"]["tokens"]


def test_builder_never_leaks_free_text(transcript):
    doc = sm.build_claude_code_map(transcript)
    # Session facts and search postings are held to the same contract as the map.
    raw = json.dumps(doc) + json.dumps(facts.compute(doc)) + json.dumps(search.postings(doc))
    for secret in (
        "SECRET-PROMPT-TEXT",
        "hunter2",
        "SECRET-ARG",
        "SECRET-OUTPUT",
        "SECRET-SUBAGENT-DESC",
        "SECRET-EDIT",
        "SECRET-PATTERN",
        "token=abc",
        "secret.example/x",
        "sk-ant-FAKE123456",
        "secret-plan",
        "Application Support",
        "fix secret stuff",
        "LEAKREPO",
        "ANSI-SECRET",
        "ENV-SECRET",
        "sudo-secret-user",
        "CD-SECRET",
        "QUOTED-SECRET",
        "SECRET_BIN",
        "SUBST-SECRET",
        "FN-SECRET",
        "SECRET-OPEN",
    ):
        assert secret not in raw, secret
    verbs = [e.get("verb") for e in doc["events"] if e.get("tool") == "Bash"]
    for _, verb in LEAKY_COMMANDS:
        assert verb in verbs, verb


_TRICKY_COMMANDS = (
    *(c for c, _ in LEAKY_COMMANDS),
    "TOKEN=\"$(cat ~/.creds | jq -r '.a b')\" ./deploy.sh --env prod",
    "X='a;b && c' Y=\"d|e\" git push origin HEAD",
    "for f in $(ls 'my dir'); do B=\"x y\" python3 \"$f\"; done",
    "if [ \"$(git status --porcelain)\" ]; then MSG='wip stuff' git stash; fi",
    "OUT=`date +%s`; A=\"`whoami` here\" ls -la",
    "P=${HOME}/a\\ b/c; cat \"$P\"",
    "K=\"unterminated value with words",
    "sudo -u deploy env A='b c' timeout 30 pytest -q",
    "(cd 'some where' && N=\"q r\" make test)",
    "git commit -m \"$(cat <<'EOF'\nDon't leak this body\nEOF\n)\"",
)


_QUOTED = r"'[^']*'|\"(?:\\.|[^\"\\])*\"|\$\((?:[^()]|\([^()]*\))*\)|`[^`]*`"
_VALUE_RE = re.compile(rf"\w+=(?:{_QUOTED}|[^\s;&|])*|{_QUOTED}")


@pytest.mark.parametrize("cmd", _TRICKY_COMMANDS)
def test_shell_verb_never_contains_an_argument_value(cmd):
    verb = sm._shell_verb(cmd)
    assert sm._VERB_SHAPE.fullmatch(verb), verb
    if verb == "sh":
        return
    # Every verb word is a word written outside any quoted value, substitution or
    # assignment — never a piece of one.
    outside = {w.rsplit("/", 1)[-1].strip("();") for w in _VALUE_RE.sub(" ", cmd).split()}
    assert set(verb.split()) <= outside, (cmd, verb)
    for m in _VALUE_RE.finditer(cmd):
        for piece in re.split(r"[\s'\"`$()=;|&{}\\/]+", m.group(0)):
            if len(piece) >= 3 and piece not in outside:
                assert piece not in verb, (cmd, verb, piece)


def test_commit_shas_are_never_file_paths(tmp_path):
    sha = "0123456789abcdef0123456789abcdef01234567"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    cwd = str(tmp_path)
    for cmd in (
        f"git show {sha}",
        f"git show {sha[:7]} -- src",
        # A merge-conflict marker inside a heredoc is not a redirect to a file.
        f"python3 - <<'EOF'\ns = s.index('>>>>>>> {sha}')\nEOF",
    ):
        _, paths, _, _ = sm.classify("Bash", {"command": cmd}, cwd)
        ops = sm.file_ops("Bash", {"command": cmd}, cwd)
        for p in [*paths, *ops]:
            assert sha[:7] not in p, (cmd, p)
    # Real paths still come through.
    _, paths, _, _ = sm.classify("Bash", {"command": f"git show {sha} -- src/app.py"}, cwd)
    assert paths == [str(tmp_path / "src" / "app.py")]
    assert sm.file_ops("Bash", {"command": "echo x > out.txt"}, cwd) == {str(tmp_path / "out.txt"): "create"}
    # `git rm` only takes file names, so a hex-named file is a real delete.
    assert sm.file_ops("Bash", {"command": f"git rm {sha[:12]}"}, cwd) == {str(tmp_path / sha[:12]): "delete"}


def test_content_hash_ignores_cli_version(transcript):
    a = sm.build_claude_code_map(transcript, cli_version="1")
    b = sm.build_claude_code_map(transcript, cli_version="2")
    assert sm.content_hash(a) == sm.content_hash(b)


def test_outside_repo_buckets():
    b = sm.MapBuilder()
    t = b.target("/tmp/claude-1000/x/y.html", "read")
    assert t == {"scope": "outside", "label": "/tmp (scratch)", "touch": "read"}


def test_posix_paths_stay_absolute_where_the_os_disagrees(monkeypatch):
    # Python 3.13 on Windows: a path with no drive or UNC root is not absolute.
    monkeypatch.setattr(sm.os.path, "isabs", lambda p: p[1:3] == ":\\" or p.startswith("\\\\"))
    b = sm.MapBuilder()
    t = b.target("/tmp/claude-1000/x/y.html", "read")
    assert t == {"scope": "outside", "label": "/tmp (scratch)", "touch": "read"}


# ---------------------------------------------------------------- heartbeat plan
def _gen(v):
    return lambda which, org, user: v














# ---------------------------------------------------------------- signals (scope extension)
def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_error_taxonomy_retries_delays_and_compaction(tmp_path):
    ts = lambda s: f"2026-09-10T01:{s // 60:02d}:{s % 60:02d}Z"  # noqa: E731

    def u(s, c, **k):
        return {"type": "user", "timestamp": ts(s), "message": {"role": "user", "content": c}, **k}

    def a(s, mid, content, **k):
        return {
            "type": "assistant",
            "timestamp": ts(s),
            "message": {
                "id": mid,
                "model": "claude-sonnet-5",
                "stop_reason": k.pop("stop", "tool_use"),
                "usage": {"input_tokens": 1, "cache_read_input_tokens": 1000, "output_tokens": 9},
                "content": content,
            },
            **k,
        }

    bash = lambda i: {  # noqa: E731
        "type": "tool_use",
        "id": i,
        "name": "Bash",
        "input": {"command": "pytest -x"},
    }  # noqa: E731
    rows = [
        u(0, "go"),
        a(
            2,
            "m1",
            [
                {"type": "thinking", "thinking": "SECRET-THOUGHT"},
                {"type": "text", "text": "SECRET-REPLY"},
                bash("b1"),
            ],
        ),
        u(
            40,
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "b1",
                    "content": "Exit code 2\nSECRET-TRACE",
                    "is_error": True,
                }
            ],
        ),
        a(41, "m2", [bash("b2")]),
        u(45, [{"type": "tool_result", "tool_use_id": "b2", "content": "ok"}]),
        {
            "type": "assistant",
            "timestamp": ts(50),
            "isApiErrorMessage": True,
            "error": "rate_limit",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "SECRET-API"}],
            },
        },
        {
            "type": "attachment",
            "timestamp": ts(51),
            "uuid": "11111111-aaaa-bbbb-cccc-000000000001",
            "attachment": {
                "type": "hook_non_blocking_error",
                "hookName": "PostToolUse:Bash",
                "hookEvent": "PostToolUse",
                "toolUseID": "b1",
                "command": "SECRET-HOOK-CMD",
                "stderr": "SECRET-HOOK",
            },
        },
        {
            "type": "attachment",
            "timestamp": ts(51),
            "uuid": "11111111-aaaa-bbbb-cccc-000000000002",
            "attachment": {
                "type": "hook_blocking_error",
                "hookName": "Stop",
                "hookEvent": "Stop",
                "toolUseID": "turn-1",
                "blockingError": {"blockingError": "SECRET-BLOCK", "command": "SECRET-BLOCK-CMD"},
            },
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "timestamp": ts(52),
            "compactMetadata": {
                "trigger": "auto",
                "preTokens": 900000,
                "postTokens": 20000,
                "durationMs": 9000,
            },
        },
        {
            "type": "system",
            "subtype": "model_refusal_fallback",
            "timestamp": ts(53),
            "direction": "retry",
        },
        u(54, "[Request interrupted by user]"),
        a(300, "m3", [{"type": "text", "text": "done"}], stop="end_turn"),
    ]
    doc = sm.build_claude_code_map(_jsonl(tmp_path / "p" / "s.jsonl", rows))
    evs = doc["events"]
    first, retry = [e for e in evs if e.get("tool") == "Bash"]
    assert first["err"] and first["errClass"] == "exit_code" and first["exitCode"] == 2
    assert retry["retryOf"] == first["seq"] and retry["attempt"] == 2 and not retry["err"]
    sig = {(e["source"], e["errClass"]) for e in evs if e["kind"] == "error"}
    assert sig == {
        ("llm", "rate_limit"),
        ("hook", "hook_error"),
        ("hook", "hook_blocking"),
        ("llm", "refusal_fallback"),
        ("orchestration", "interrupted"),
    }
    comp = next(e for e in evs if e["kind"] == "compaction")
    assert (comp["trigger"], comp["preTokens"], comp["postTokens"], comp["ms"]) == (
        "auto",
        900000,
        20000,
        9000,
    )
    llm1 = next(e for e in evs if e["kind"] == "llm")
    assert (
        llm1["thinking"] is True
        and llm1["toolCalls"] == 1
        and llm1["textChars"] == len("SECRET-REPLY")
    )
    assert llm1["latency"] == 2000 and llm1["stop"] == "tool_use"
    delays = [e for e in evs if e["kind"] == "delay"]
    assert {d["cause"] for d in delays} >= {"tool", "llm"}  # 38 s pytest, 246 s to the last reply
    t = doc["totals"]
    assert t["retries"] == 1 and t["compactions"] == 1 and t["errorsBySource"]["llm"] == 2
    assert t["errors"] == sum(t["errorsBySource"].values())
    hooks = {e["hook"]: e for e in evs if e.get("source") == "hook"}
    post, stop = hooks["PostToolUse:Bash"], hooks["Stop"]
    assert (post["hookEvent"], post["srcUuid"]) == ("PostToolUse", "11111111-aaaa-bbbb-cccc-000000000001")
    assert post["ref"] == first["seq"]  # the failed Bash call that triggered it
    assert (stop["hookEvent"], stop["srcUuid"]) == ("Stop", "11111111-aaaa-bbbb-cccc-000000000002")
    assert "ref" not in stop  # a turn id, not one of our tool events
    raw = json.dumps(doc)
    for secret in (
        "SECRET-THOUGHT",
        "SECRET-REPLY",
        "SECRET-TRACE",
        "SECRET-API",
        "SECRET-HOOK",
        "SECRET-BLOCK",
    ):
        assert secret not in raw, secret


def test_tool_without_result_is_flagged(tmp_path):
    rows = [
        {
            "type": "assistant",
            "timestamp": "2026-09-10T01:00:00Z",
            "message": {
                "id": "m",
                "model": "claude-sonnet-5",
                "usage": {"output_tokens": 1},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": "Bash",
                        "input": {"command": "sleep 999"},
                    }
                ],
            },
        }
    ]
    doc = sm.build_claude_code_map(_jsonl(tmp_path / "q" / "s.jsonl", rows))
    ev = next(e for e in doc["events"] if e.get("tool") == "Bash")
    assert ev["err"] and ev["errClass"] == "no_result"


@pytest.mark.parametrize(
    ("text", "cls", "code"),
    [
        ("Exit code 137", "exit_code", 137),
        ("<tool_use_error>InputValidationError: x", "validation", None),
        ("The user doesn't want to proceed", "permission_denied", None),
        ("Command timed out", "timeout", None),
        ("claude-opus-5 is temporarily unavailable", "model_unavailable", None),
        ("weird", "other", None),
    ],
)
def test_classify_tool_error(text, cls, code):
    assert sm.classify_tool_error(text) == (cls, code)


# ---------------------------------------------------------------- misc detection
@pytest.mark.parametrize(
    ("cmd", "kind"),
    [
        ("pytest -q", "verify"),
        ("uv run pytest tests/", "verify"),
        ("python -m pytest -x", "verify"),
        (".venv/bin/pytest", "verify"),
        ("cd /x && FOO=1 npm test", "verify"),
        ("timeout 300 cargo test", "verify"),
        ("uvx ruff check src", "verify"),
        ("echo pytest", "exec"),
        ('git commit -m "run pytest; ruff"', "exec"),
        ("git commit -m 'fix pytest'", "exec"),
        ("grep -r pytest .", "search"),
    ],
)
def test_verify_only_matches_the_command_word(cmd, kind):
    assert sm.classify("Bash", {"command": cmd}, None)[0] == kind


def test_repo_dot_claude_stays_repo_relative(tmp_path, fake_home):
    repo = tmp_path / "work" / "svc"
    (repo / ".git").mkdir(parents=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "rules.md").write_text("x")
    b = sm.MapBuilder()
    t = b.target(str(repo / ".claude" / "rules.md"), "read")
    assert "file" in t and b.files[t["_key"]]["path"] == ".claude/rules.md"
    home_t = b.target(str(fake_home / ".claude" / "skills" / "x" / "SKILL.md"), "read")
    assert home_t["scope"] == "outside" and "file" not in home_t


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://SECRET:pw@api.example.com:8443/x?q=1#f", "api.example.com"),
        ("https://SECRET@api.example.com/x", "api.example.com"),
        ("http://docs.python.org/3/", "docs.python.org"),
        ("how to parse urls", "search"),
        ("https://[::1", "search"),
    ],
)
def test_web_host_never_carries_credentials(url, host):
    b = sm.MapBuilder()
    ev = b.tool_event(tool="WebFetch", inp={"url": url}, cwd=None, agent="main", ts=1.0)
    assert ev["host"] == host and "SECRET" not in json.dumps(ev)


def test_derived_delays_respect_max_events(monkeypatch):
    monkeypatch.setattr(sm, "MAX_EVENTS", 10)
    b = sm.MapBuilder()
    for i in range(10):
        ev = b.tool_event(
            tool="Bash", inp={"command": "make"}, cwd=None, agent="main", ts=100.0 * i
        )
        ev["dur"] = sm.TOOL_DELAY_MS * 2
    doc = b.finish(session={"id": "s", "agentTool": "claude-code"})
    assert len(doc["events"]) == 10
    assert doc["session"]["truncated"] is True
    assert all(e["kind"] != "delay" or isinstance(e.get("ref"), int) for e in doc["events"])


def _span(span_id, name, start, end=None, parent=None, **attrs):
    return SimpleNamespace(
        span_id=span_id,
        name=name,
        start_time=start,
        end_time=end,
        parent_id=parent,
        status_code="OK",
        attributes=attrs,
    )


def test_spans_zero_usage_turn_counts_and_tool_duration():
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 9, 10, 1, 0, tzinfo=UTC)
    spans = [
        _span("t1", "Turn 1", t0, **{"llm.model_name": "cursor-fast"}),
        _span(
            "c1",
            "Shell",
            t0 + timedelta(seconds=1),
            t0 + timedelta(seconds=1 + sm.TOOL_DELAY_MS / 1000 * 2),
            parent="t1",
            **{"tool.name": "Bash", "input.value": json.dumps({"command": "make"})},
        ),
    ]
    doc = sm.build_map_from_spans(spans, session_id="local:cursor:x", agent_tool="cursor")
    assert doc["totals"]["requests"] == 1
    llm = next(e for e in doc["events"] if e["kind"] == "llm")
    assert (llm["in"], llm["out"], llm["model"]) == (0, 0, "cursor-fast")
    tool = next(e for e in doc["events"] if e.get("tool") == "Bash")
    assert tool["dur"] == sm.TOOL_DELAY_MS * 2
    assert any(e["kind"] == "delay" and e.get("cause") == "tool" for e in doc["events"])


def test_spans_all_untimestamped_falls_back_to_started_at_hint():
    """A tiny Cursor/Codex session where no span carries a timestamp (for example a
    3-event Cursor session) must still get a real
    ``startedAt``. Without one the map has no start time, even though the
    build itself "succeeded"."""
    spans = [
        _span("t1", "Turn 1", None, **{"llm.model_name": "default"}),
        _span("c1", "Shell", None, None, parent="t1", **{"tool.name": "Bash", "input.value": "{}"}),
    ]
    hint = 1_757_000_000.0  # arbitrary fixed epoch seconds, stands in for the source file's mtime
    doc = sm.build_map_from_spans(
        spans, session_id="local:cursor:x", agent_tool="cursor", started_at_hint=hint
    )
    assert doc["session"]["startedAt"] == sm._iso(hint)
    # Without the hint the old (broken) behaviour is preserved — never silently
    # invent a timestamp when nothing at all is known about when this happened.
    doc_no_hint = sm.build_map_from_spans(spans, session_id="local:cursor:y", agent_tool="cursor")
    assert doc_no_hint["session"]["startedAt"] is None














def test_estimate_cost_applies_the_long_context_tier_per_request():
    # gpt-6-luna: $0.10 in / $0.50 out / $0.01 read / $0.125 write per 1M;
    # >272K cache-inclusive input bills the WHOLE request at 2x in, 1.5x out.
    at_threshold = sm.estimate_cost_usd("gpt-6-luna", 272_000, 0, 0, 1_000_000)
    assert at_threshold == pytest.approx(0.0272 + 0.5)

    long = sm.estimate_cost_usd("gpt-6-luna", 200_000, 50_000, 30_000, 1_000_000)
    # input 200K*0.20 + write 50K*0.25 + read 30K*0.02 + out 1M*0.75, per 1M
    assert long == pytest.approx(0.04 + 0.0125 + 0.0006 + 0.75)

    # A card without a long-context tier keeps its base rates at any size.
    assert sm.estimate_cost_usd("gpt-5.6-luna", 400_000, 0, 0, 0) == pytest.approx(0.08)


def test_a_turn_aggregate_is_not_billed_at_the_long_context_tier():
    # Two 200K gpt-6-luna requests in one turn: each is below 272K, so neither
    # is long-context -- the 400K turn sum must not trip the tier (review finding).
    events = [
        {"model": "gpt-6-luna", "input_tokens": 200_000, "output_tokens": 0},
        {"model": "gpt-6-luna", "input_tokens": 200_000, "output_tokens": 0},
    ]
    assert sm._cost_from_events("gpt-6-luna", events) == pytest.approx(0.04)
    # Without per-request events, an aggregate is priced at base rates.
    assert sm.estimate_cost_usd(
        "gpt-6-luna", 400_000, 0, 0, 0, single_request=False
    ) == pytest.approx(0.04)
    # One genuinely long request inside the turn still gets the tier.
    events.append({"model": "gpt-6-luna", "input_tokens": 300_000, "output_tokens": 0})
    assert sm._cost_from_events("gpt-6-luna", events) == pytest.approx(0.04 + 0.06)








@pytest.mark.parametrize(
    ("cmd", "verb"),
    [
        ("for a in 1; do " * 5 + "pytest; " + "done; " * 5, "for"),  # deeper than the walk: no crash
        ("cd a; " * 8 + "pytest -q", "pytest"),  # the walk's last step still reaches the command
        ("A=1; " * 3 + "cd x && make test", "make test"),
    ],
)
def test_shell_verb_survives_deep_nesting_and_long_setup(cmd, verb):
    assert sm._shell_verb(cmd) == verb


def test_hex_named_files_are_still_file_operations():
    ops = sm.file_ops("Bash", {"command": "rm deadbeef1 && echo x > cafe1234"}, "/repo")
    assert ops.get(os.path.normpath("/repo/deadbeef1")) == "delete"
    assert ops.get(os.path.normpath("/repo/cafe1234")) == "create"
