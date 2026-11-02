"""Cline session discovery, indexing, and the built map's free-text contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentplay import builder, discover
from agentplay._core.ingestion.coding_agent._sources.local.parsers.cline import parse_cline
from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource

FIXTURES = Path(__file__).parent / "fixtures" / "cline"
NAMES = ("edit_and_read", "failed_shell", "read_search_edit")
CWD = "/work/project"
# Every placeholder the fixtures use for text a session must never carry into the map.
PLACEHOLDERS = ("PROMPT_", "REPLY_", "THINKING", "OUTPUT", "QUERY", "OLD-TEXT", "NEW-TEXT", "FILE-CONTENT")


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.messages.json").read_bytes()


def _map(name: str) -> dict:
    sid = f"local:cline:{name}"
    _, spans = parse_cline(_fixture(name), sid)
    return builder.build_map_from_spans(spans, session_id=sid, agent_tool="cline")


def _strings(value) -> set[str]:
    if isinstance(value, str):
        return {value}
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return set().union(*(_strings(v) for v in items)) if items else set()


@pytest.mark.parametrize("name", NAMES)
def test_built_map_never_leaks_fixture_text(name):
    doc = _map(name)
    raw = json.dumps(doc)
    for text in _strings(json.loads(_fixture(name))):
        if text.startswith(PLACEHOLDERS):
            assert text not in raw, text
    # Outside a git repository a path collapses to a bucket label, so neither the
    # absolute path nor the file name may appear.
    for part in (*PLACEHOLDERS, CWD, "file1", "file2"):
        assert part not in raw, part
    assert doc["session"]["agentTool"] == "cline"


def test_built_map_classifies_reads_edits_shell_and_the_failures():
    def kinds(name: str) -> list[tuple]:
        return [(e["kind"], e.get("verb"), e.get("err")) for e in _map(name)["events"] if e.get("tool")]

    assert kinds("edit_and_read") == [
        ("read", None, True),
        ("search", "grep", False),
        ("edit", None, False),
        ("exec", "source", False),
        ("exec", "unset", False),
    ]
    assert {k[0] for k in kinds("read_search_edit")} == {"read", "search", "edit"}
    assert _map("failed_shell")["totals"]["errors"] == 2


def _cline_home(tmp_path: Path) -> Path:
    home = tmp_path / ".cline"
    sessions = home / "data" / "sessions"
    for name in NAMES:
        folder = sessions / f"100_{name}"
        folder.mkdir(parents=True)
        (folder / f"100_{name}.messages.json").write_bytes(_fixture(name))
        (folder / f"100_{name}.json").write_text(json.dumps({"cwd": CWD}), encoding="utf-8")
        # A spawned subagent's conversation is a component of its parent, not a session.
        (folder / "agent_1.messages.json").write_bytes(_fixture(name))
    empty = sessions / "200_empty"
    empty.mkdir()
    (empty / "200_empty.messages.json").write_text('{\n  "sessionId": "200_empty",\n  "messages": []\n}\n')
    other = sessions / "300_other"
    other.mkdir()
    (other / "300_other.messages.json").write_text('{"hello": "world"}')
    return home


def test_local_source_indexes_cline_sessions(tmp_path):
    source = LocalSource(claude_code_homes=[], codex_homes=[], cline_homes=[_cline_home(tmp_path)])
    index = source._build_index()
    assert sorted(index) == [f"local:cline:100_{name}" for name in NAMES]
    stats = source.discovery_stats()["cline"]
    assert stats["skipped"] == {"empty_events": 1, "different_agent": 1}
    sid = "local:cline:100_failed_shell"
    assert source.get_agent_type(sid) == "cline"
    assert source.get_cwd(sid) == CWD
    assert source.get_project_name(sid) == "project"
    spans = source.get_spans_by_session(sid)
    assert {s.attributes["cwd"] for s in spans if s.parent_id} == {CWD}
    assert [s.name for s in spans if ":tool:" in s.span_id] == ["Bash"] * 24


def test_session_without_metadata_still_indexes(tmp_path):
    home = _cline_home(tmp_path)
    (home / "data" / "sessions" / "100_failed_shell" / "100_failed_shell.json").unlink()
    index = LocalSource(claude_code_homes=[], codex_homes=[], cline_homes=[home])._build_index()
    ref = index["local:cline:100_failed_shell"]
    assert (ref.cwd, ref.project_name) == (None, None)


@pytest.mark.parametrize("text", ["[]", '"x"', "null", '{"cwd": 3}', '{"cwd": ""}', "{bad json", "[" * 100_000])
def test_malformed_metadata_file_does_not_abort_indexing(tmp_path, text):
    home = _cline_home(tmp_path)
    (home / "data" / "sessions" / "100_failed_shell" / "100_failed_shell.json").write_text(text)
    index = LocalSource(claude_code_homes=[], codex_homes=[], cline_homes=[home])._build_index()
    assert sorted(index) == [f"local:cline:100_{name}" for name in NAMES]
    assert index["local:cline:100_failed_shell"].cwd is None


def test_discovery_lists_cline_with_env_override(tmp_path, monkeypatch):
    assert "cline" in discover.AGENTS
    monkeypatch.setenv("AGENTPLAY_CLINE_HOMES", str(tmp_path))
    homes = discover.Homes.default()
    assert homes.cline == [tmp_path]
    assert homes.present()["cline"] == [tmp_path]
    monkeypatch.delenv("AGENTPLAY_CLINE_HOMES")
    assert discover.Homes.default().cline == [Path.home() / ".cline"]
