"""The one table that maps each coding agent's raw tool names to our canonical names.

Every parser reads its aliases from here, so a new agent (or a renamed vendor tool)
is one row in one file, and every consumer -- metrics, segmentation -- sees the same
answer. Canonical names are Claude Code's own tool names (``Bash``, ``Read``,
``Edit``, ``Grep``, ``Task``...), so Claude transcripts need no aliasing and every
downstream name set keeps its meaning.
"""

from __future__ import annotations

# Codex emits raw agent-internal tool names that differ from the canonical
# Claude vocabulary every downstream consumer keys off by `tool.name`
# (read/edit/search/shell counters in metrics.py, blind-edit/full-rewrite
# ordering, tool_distribution, produced-output extraction). Without this
# alias table those buckets never match, so a Codex session shows tools/turn
# but zero read/edit/shell counts. Mirrors `cursor_store.py::_TOOL_ALIASES`
# (the same fix for Cursor's raw names). Names surveyed across 19 real Codex
# transcripts: `exec_command`/`write_stdin` (unified exec) and `apply_patch`
# dominate; `shell`/`read_file` are older Codex releases' names for the same
# operations. Desktop's wrapper exposes the same host command capability as
# `exec`, so it must share the shell alias to preserve PR-create provenance.
# Meta/MCP tools (`update_plan`, `list_mcp_resources`,
# `mcp__<server>__<tool>`, ...) intentionally pass through unaliased — they
# don't belong to a read/edit/shell bucket and tool_distribution should show
# their real names.
_CODEX: dict[str, str] = {
    "exec_command": "Bash",
    "exec": "Bash",
    "shell": "Bash",
    # stdin writes into a running exec session — shell interaction, same
    # bucket as Cursor's `AwaitShell`.
    "write_stdin": "Bash",
    "apply_patch": "Edit",
    "read_file": "Read",
    # Not a tool name but a `response_item` payload TYPE — Codex's built-in
    # browsing runs as unpaired `web_search_call` lines (no call_id, no
    # output line). `_group_into_turns` emits them as point-in-time tools
    # under this raw name; canonicalized here like Cursor's `web_search`.
    "web_search_call": "WebSearch",
}

# Cursor emits raw agent-internal tool names that differ from the canonical
# Claude/Codex vocabulary every downstream consumer keys off by
# `tool.name` (read/edit/search/shell counters in metrics.py, blind-edit/
# full-rewrite ordering, tool_distribution, subagent adoption). Without this
# alias table those buckets never match, so a Cursor session would show
# tools/turn but zero read/edit/search/shell counts. Includes the IDE
# `*_v2` names from live composers. Meta tools (TodoWrite, AskQuestion, CreatePlan,
# update_current_step, ...) intentionally pass through unaliased — they
# don't belong to a read/edit/write bucket.
_CURSOR: dict[str, str] = {
    # edits (Claude-ish + Cursor ACP)
    "StrReplace": "Edit",
    "StrConfReplace": "Edit",
    "ApplyPatch": "Edit",
    # reads / search (Claude-ish + Cursor ACP)
    "ReadFile": "Read",
    "ReadLints": "Read",
    "rg": "Grep",
    "SemanticSearch": "Search",
    # shell / execute (Claude-ish + Cursor ACP)
    "Shell": "Bash",
    "AwaitShell": "Bash",
    # subagent
    "Subagent": "Task",
    # IDE composer `*_v2` names (live state.vscdb / NDJSON) — without these,
    # tools/turn still counts but read/edit/search/shell buckets stay 0.
    "read_file_v2": "Read",
    "read_file": "Read",
    "read_lints": "Read",
    "edit_file_v2": "Edit",
    "edit_file": "Edit",
    "write_file_v2": "Write",
    "write_file": "Write",
    "run_terminal_command_v2": "Bash",
    "run_terminal_cmd": "Bash",
    "await": "Bash",
    "ripgrep_raw_search": "Grep",
    "glob_file_search": "Glob",
    "list_dir": "ListDir",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "task_v2": "Task",
}

# Copilot's built-in tool vocabulary -> the canonical Claude names every
# consumer keys off (read/edit/search/shell counters in metrics.py, blind-edit
# and full-rewrite ordering, tool_distribution, sub-agent + skill capability
# detection). Names come from the Copilot CLI bundle's own tool classifier
# (bash/local_shell/stop_bash = execute; view/glob/grep/read_bash = read;
# edit/create/str_replace_editor/str_replace/write_bash = edit; web_search =
# search; web_fetch = fetch; update_todo = think; task/skill = other) plus the
# names seen in real transcripts. MCP tools (``github-mcp-server-*``) and
# meta tools (``report_intent``, ``ask_user``, ``sql``) pass through unaliased:
# they belong to no bucket and tool_distribution should show their real names.
_COPILOT: dict[str, str] = {
    # shell
    "bash": "Bash",
    "shell": "Bash",
    "local_shell": "Bash",
    "powershell": "Bash",
    # interaction with a still-running shell command: same bucket as Cursor's
    # ``AwaitShell`` and Codex's ``write_stdin``.
    "read_bash": "Bash",
    "write_bash": "Bash",
    "stop_bash": "Bash",
    # reads / search
    "view": "Read",
    "grep": "Grep",
    "glob": "Glob",
    # edits / writes. ``create`` writes a whole new file, so it is the
    # full-rewrite bucket; the str_replace family is an in-place edit.
    "edit": "Edit",
    "str_replace_editor": "Edit",
    "str_replace": "Edit",
    "apply_patch": "Edit",
    "create": "Write",
    # web
    "web_fetch": "WebFetch",
    "fetch_copilot_cli_documentation": "WebFetch",
    "web_search": "WebSearch",
    "github-mcp-server-web_search": "WebSearch",
    # semantic code search: same mapping Cursor's SemanticSearch gets.
    "search_code_subagent": "Search",
    # planning
    "update_todo": "TodoWrite",
    # sub-agents: drives the ``subagents`` capability and sub-agent adoption
    # metrics, which look for the canonical ``Task`` name.
    "task": "Task",
    # skills: ``_jsonl_utils._SKILL_TOOL_NAMES`` matches case-insensitively,
    # so the canonical casing keeps Copilot skill loads counted the same way.
    "skill": "Skill",
}

# Kiro emits its own tool vocabulary, which the consumers (read/edit/
# search/shell counters in metrics.py, tool_distribution, subagent adoption)
# don't recognise — they key off the canonical Claude/Codex names. Without this
# table a Kiro session would show tools-per-turn but zero read/edit/search
# buckets, exactly the failure `cursor_store.py::_TOOL_ALIASES` exists to
# prevent. Names verified against real ~/.kiro transcripts. Meta tools
# (`kiro_powers`, `subagent_response`) intentionally pass through unaliased —
# they belong to no bucket.
_KIRO: dict[str, str] = {
    # reads
    "read_file": "Read",
    "read_files": "Read",
    "readFile": "Read",
    # edits / writes
    "fs_write": "Write",
    "fsWrite": "Write",
    "fs_append": "Edit",
    "str_replace": "Edit",
    # search
    "grep_search": "Grep",
    "file_search": "Glob",
    "list_directory": "ListDir",
    # shell
    "execute_bash": "Bash",
    # subagent — drives the `subagents` capability + sub-agent adoption metrics,
    # which look for the canonical `Task`/`Agent` names.
    "invoke_sub_agent": "Task",
}

# opencode names its built-in tools in lowercase (`read`, `edit`, `bash`,
# ...) while every downstream consumer keys off the canonical
# Claude-cased vocabulary by `tool.name` (read/edit/search/shell counters in
# metrics.py, blind-edit/full-rewrite ordering, tool_distribution). Without
# this casing table those buckets never match, so an opencode session shows
# tools/turn but zero read/edit/shell counts. Mirrors
# `cursor_store.py::_TOOL_ALIASES` (same fix for Cursor's raw names). Names
# surveyed across two real multi-MB opencode transcripts (read/edit/grep/
# bash/webfetch/write/todowrite/glob/websearch/codesearch/task) plus the
# rest of opencode's core toolset (`patch`, `list`, `todoread`). Unknown
# names (MCP tools, future built-ins) pass through unchanged.
_OPENCODE: dict[str, str] = {
    "bash": "Bash",
    "edit": "Edit",
    # opencode's `patch` is its apply-a-diff mutation tool — same Edit
    # bucket as Codex's `apply_patch` / Cursor's `ApplyPatch`.
    "patch": "Edit",
    "write": "Write",
    "read": "Read",
    "grep": "Grep",
    "glob": "Glob",
    "list": "ListDir",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    # semantic code search — same mapping cursor_store gives SemanticSearch.
    "codesearch": "Search",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "task": "Task",
}

# Copilot IN VS CODE (Chat / agent mode), NOT the `copilot` CLI above -- a
# different product with its own tool schema. Rows are VS Code's own built-in
# tool identifiers, taken from `workspaceStorage/*/chatSessions/*.jsonl`
# request `toolCallRounds[].toolCalls[].name` values.
#
# Deliberately unmapped (pass through as their real name, same convention as
# an unrecognised or MCP tool): `get_errors`, `open_browser_page`,
# `read_page`, `run_playwright_code` (browser/diagnostic tools with no Claude
# equivalent) and `memory` (ambiguous -- an MCP server or a VS Code built-in,
# not yet confirmed).
_COPILOT_VSCODE: dict[str, str] = {
    # shell
    "run_in_terminal": "Bash",
    "get_terminal_output": "BashOutput",
    # reads / search
    "read_file": "Read",
    "read_notebook_cell_output": "NotebookRead",
    "grep_search": "Grep",
    "file_search": "Glob",
    "list_dir": "ListDir",
    # edits / writes. `create_file` writes a whole new file (full-rewrite
    # bucket); the replace family is an in-place edit, same split as Copilot
    # CLI's `create` vs `edit`/`str_replace_editor` above.
    "replace_string_in_file": "Edit",
    "multi_replace_string_in_file": "MultiEdit",
    "create_file": "Write",
    # planning / sub-agents
    "manage_todo_list": "TodoWrite",
    "runSubagent": "Task",
}

# Gemini CLI's built-in tool names. `read_file`, `replace`, `write_file` and
# `run_shell_command` were seen in real Gemini CLI 0.65 sessions; the rest are
# the `*_TOOL_NAME` constants in the same release's bundle. `replace` is an
# in-place old_string/new_string edit and `write_file` writes the whole file,
# the same Edit/Write split as Copilot's `edit` vs `create`. Meta tools
# (`update_topic`, `ask_user`, `enter_plan_mode`, `tracker_*`, `activate_skill`,
# ...) and MCP tools (`mcp_<server>_<tool>`) pass through unaliased.
_GEMINI_CLI: dict[str, str] = {
    "run_shell_command": "Bash",
    "read_file": "Read",
    "read_many_files": "Read",
    "replace": "Edit",
    "write_file": "Write",
    "grep_search": "Grep",
    "glob": "Glob",
    "list_directory": "ListDir",
    "web_fetch": "WebFetch",
    "google_web_search": "WebSearch",
    "write_todos": "TodoWrite",
    "invoke_agent": "Task",
}

# Antigravity CLI (`agy`) built-in tool names. `read_file`, `view_file`,
# `write_file`, `edit_file`, `run_command` and `invoke_subagent` were seen in
# real agy 1.4.6 sessions (`view_file` in a subagent); `edit_file` is an
# in-place Target/Replacement edit and `write_file` writes the whole file.
# `write_to_file`, `replace_file_content`, `multi_replace_file_content`,
# `grep_search`, `find_by_name`, `list_dir`, `search_web` and
# `read_url_content` are names in the 1.4.6 binary that no captured session
# used. `send_message` (a subagent reporting to its parent) and other meta
# tools pass through unaliased.
_ANTIGRAVITY_ALIASES: dict[str, str] = {
    "run_command": "Bash",
    "read_file": "Read",
    "view_file": "Read",
    "edit_file": "Edit",
    "replace_file_content": "Edit",
    "multi_replace_file_content": "Edit",
    "write_file": "Write",
    "write_to_file": "Write",
    "grep_search": "Grep",
    "find_by_name": "Glob",
    "list_dir": "ListDir",
    "search_web": "WebSearch",
    "read_url_content": "WebFetch",
    "invoke_subagent": "Task",
}

TOOL_ALIASES: dict[str, dict[str, str]] = {
    "antigravity-cli": _ANTIGRAVITY_ALIASES,
    "codex": _CODEX,
    "gemini-cli": _GEMINI_CLI,
    "cursor": _CURSOR,
    "copilot": _COPILOT,
    "copilot-vscode": _COPILOT_VSCODE,
    "kiro": _KIRO,
    "opencode": _OPENCODE,
}


def aliases_for(agent: str) -> dict[str, str]:
    """The raw-name -> canonical-name table for one agent (empty for Claude Code)."""
    return TOOL_ALIASES.get(agent, {})
