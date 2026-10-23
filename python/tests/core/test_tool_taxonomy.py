"""The shared raw -> canonical tool table.

Every parser reads ``tool_taxonomy``. These tests pin that the rows resolve
the way each parser expects.
"""

from __future__ import annotations

import pytest

from agentplay._core.ingestion.coding_agent import tool_taxonomy as tt
from agentplay._core.ingestion.coding_agent._sources.local.parsers import (
    codex,
    copilot,
    cursor_agent_transcript,
    cursor_store,
    kiro,
    opencode,
    windsurf,
)


@pytest.mark.parametrize(
    ("module", "agent"),
    [
        (codex, "codex"),
        (cursor_store, "cursor"),
        (cursor_agent_transcript, "cursor"),
        (copilot, "copilot"),
        (kiro, "kiro"),
        (opencode, "opencode"),
        (windsurf, "windsurf"),
    ],
)
def test_every_parser_reads_the_shared_table(module, agent) -> None:
    # Same object, not a copy: a new row in tool_taxonomy reaches the parser.
    assert module._TOOL_ALIASES is tt.aliases_for(agent)


@pytest.mark.parametrize(
    ("agent", "raw", "canonical"),
    [
        ("codex", "exec_command", "Bash"),
        ("codex", "apply_patch", "Edit"),
        ("cursor", "run_terminal_cmd", "Bash"),
        ("cursor", "SemanticSearch", "Search"),
        ("cursor", "Subagent", "Task"),
        ("copilot", "str_replace_editor", "Edit"),
        ("copilot", "view", "Read"),
        ("kiro", "execute_bash", "Bash"),
        ("opencode", "todowrite", "TodoWrite"),
        ("windsurf", "read_file", "Read"),
        ("windsurf", "run_command", "Bash"),
        ("windsurf", "edit", "Edit"),
    ],
)
def test_known_raw_names_resolve(agent, raw, canonical) -> None:
    assert tt.aliases_for(agent)[raw] == canonical


def test_unknown_and_claude_names_pass_through() -> None:
    # Claude Code names are already canonical; MCP and unknown names are not rows,
    # so the parsers' ``.get(raw, raw)`` passes them through.
    assert tt.aliases_for("claude-code") == {}
    assert "brand_new_tool" not in tt.aliases_for("codex")
    assert not any(k.startswith("mcp__") for t in tt.TOOL_ALIASES.values() for k in t)


def test_table_sizes_are_pinned() -> None:
    # These counts are pinned; a changed count means a row was added or dropped on purpose -- update here.
    # copilot-vscode's 12 rows are pinned against real tool names.
    assert {a: len(t) for a, t in tt.TOOL_ALIASES.items()} == {
        "codex": 7,
        "cursor": 26,
        "copilot": 23,
        "copilot-vscode": 12,
        "kiro": 12,
        "opencode": 14,
        "windsurf": 12,
    }
