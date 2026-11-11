"""A tool input the parsers cut at 2,000 characters still names the files it touched.

Every local parser truncates `input.value` (`_TRUNCATE = 2000` / `[:2000]`) and
writes the path keys first so they survive the cut — `codex._apply_patch_input`
spells out why, and so does `opencode._normalize_file_path_arg`. The builder used
to be the one consumer that ignored that guarantee: a cut input is not JSON, so
`json.loads` raised and the call reached the map with no target at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentplay import builder

T0 = datetime(2026, 9, 23, 15, 44, tzinfo=UTC)
CAP = 2000  # every parser's `input.value` cap


def _at(sec: float) -> datetime:
    return T0 + timedelta(seconds=sec)


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


def _repo(tmp_path, name="project", remote="acme/project"):
    repo = tmp_path / "work" / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(f'[remote "origin"]\n\turl = git@github.com:{remote}.git\n')
    return repo


def _write_spans(repo, content_chars: int):
    """One `Write` span whose input the parser would cut at the 2,000-char cap."""
    path = repo / "out.txt"
    raw = json.dumps({"file_path": str(path), "content": "x" * content_chars})[:CAP]
    return [
        _span("root", "Session", _at(0), **{"harness.agent_session_id": "g1"}),
        _span("t1", "Turn 1", _at(0), _at(60), parent="root", **{"input.value": "write it"}),
        _span(
            "a",
            "Write",
            _at(1),
            _at(2),
            parent="t1",
            **{
                "tool.name": "Write",
                "cwd": str(repo),
                "input.value": raw,
                "output.value": "OK",
            },
        ),
    ]


def _files(doc):
    return sorted(f["path"] for f in doc["files"])


def _targets(doc):
    return [
        (e.get("tool"), [(t.get("file"), t.get("op")) for t in e["targets"]])
        for e in doc["events"]
        if e["kind"] in ("edit", "write")
    ]


def _ops_by_path(doc):
    paths = {f["id"]: f["path"] for f in doc["files"]}
    return {
        paths[t["file"]]: t.get("op")
        for e in doc["events"]
        if e["kind"] in ("edit", "write")
        for t in e["targets"]
    }


def test_a_short_write_keeps_its_file(tmp_path):
    """Control: an input under the cap decodes, and did before the fix too."""
    doc = builder.build_map_from_spans(
        _write_spans(_repo(tmp_path), 100), session_id="local:gemini-cli:short", agent_tool="gemini-cli"
    )
    assert _files(doc) == ["out.txt"]
    assert doc["totals"]["filesTouched"] == 1
    assert _targets(doc) == [("Write", [(0, "create")])]


def test_a_write_cut_at_the_cap_keeps_its_file(tmp_path):
    """The file a long write touched is in files[] whatever the input's length."""
    doc = builder.build_map_from_spans(
        _write_spans(_repo(tmp_path), 5000), session_id="local:gemini-cli:long", agent_tool="gemini-cli"
    )
    assert _files(doc) == ["out.txt"]
    assert doc["totals"]["filesTouched"] == 1
    assert _targets(doc) == [("Write", [(0, "create")])]


# ---------------------------------------------------------------- codex apply_patch
SMALL_PATCH = (
    "*** Begin Patch\n"
    "*** Update File: a.py\n"
    "@@ -1 +1 @@\n"
    "-x = 1\n"
    "+x = 2\n"
    "*** Add File: b.py\n"
    "+print(x)\n"
    "*** End Patch\n"
)
# Long enough that `input.value` is cut mid-patch, which is the common case for
# a real refactor: `codex._apply_patch_input` writes both `file_path` keys ahead
# of the body precisely so they land in the retained prefix.
BIG_PATCH = (
    "*** Begin Patch\n"
    "*** Update File: big.py\n"
    "@@ -1 +1 @@\n"
    + "-old line\n" * 240
    + "*** Add File: added.py\n"
    "+print('hi')\n"
    "*** End Patch\n"
)


def _codex_rollout(patch: str) -> bytes:
    lines = [
        {
            "type": "session_meta",
            "timestamp": "2026-09-08T10:00:00Z",
            "payload": {"id": "t", "originator": "codex_cli_rs", "cwd": "/repo"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-08T10:00:01Z",
            "payload": {"role": "user", "content": [{"type": "input_text", "text": "patch it"}]},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-08T10:00:02Z",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "name": "apply_patch",
                "input": patch,
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-08T10:00:03Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "Success. Updated the following files:\nM a.py\nA b.py\n",
            },
        },
    ]
    return b"\n".join(json.dumps(line).encode() for line in lines) + b"\n"


class _OneRepo:
    """Every path belongs to one repo, so a target gets a repo-relative id."""

    def resolve(self, path):
        return ("/repo", "repo", "acme/repo")


def _codex_map(patch: str, tmp_path):
    from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex

    spans = parse_codex(_codex_rollout(patch), "local:codex:patch")[1]
    return builder.build_map_from_spans(
        spans, session_id="local:codex:patch", agent_tool="codex", resolver=_OneRepo()
    )


def test_a_codex_apply_patch_keeps_every_file_it_names(tmp_path):
    """Repeated `file_path` keys collapse to the last one in a decoded dict."""
    doc = _codex_map(SMALL_PATCH, tmp_path)
    assert _files(doc) == ["a.py", "b.py"]
    # `Add File` is a create and `Update File` a modify; the span is named `Edit`
    # (`tool_taxonomy` maps codex's `apply_patch`), so the headers say which.
    assert _ops_by_path(doc) == {"a.py": "modify", "b.py": "create"}
    assert doc["totals"]["filesTouched"] == 2


def test_an_apply_patch_cut_at_the_cap_keeps_its_files(tmp_path):
    """A cut patch decodes to nothing, so every path comes from the retained prefix."""
    doc = _codex_map(BIG_PATCH, tmp_path)
    assert _files(doc) == ["added.py", "big.py"]
    assert doc["totals"]["filesTouched"] == 2
