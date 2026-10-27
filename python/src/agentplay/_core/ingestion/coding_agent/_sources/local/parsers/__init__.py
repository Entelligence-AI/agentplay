"""Per-agent transcript parsers.

`detect_agent(data)` reads the head lines of a JSONL file and
returns the agent type by sniffing for marker lines:

- Claude Code: `type=user|assistant` + `sessionId` field.
- Codex: `session_meta` line with `originator` starting `codex`.
- Copilot: `session.start` line with `producer="copilot-agent"` (the CLI's own
  `~/.copilot/session-state/<id>/events.jsonl`, read in place).
- Copilot in VS Code (Chat/agent mode): a `.jsonl` file's first line is the
  `kind=0` snapshot envelope with a `requests` key. The sibling `.json`
  (single pretty-printed document, no self-contained line) isn't detected
  here at all -- `LocalSource._index_copilot_vscode_file` checks that shape
  directly.
- Cursor: synthesized header line with `type="cursor_store_session"`.
- Kiro: synthesized header line with `type="kiro_session"`.
- Gemini CLI: header line with `sessionId` + `projectHash` and no `type`, or a
  `type="gemini"` message (`~/.gemini/tmp/<project>/chats/session-*.jsonl`).
- Command Code: `type="session"` header with `id` + `cwd` + int `version`, or
  a `type="message"` record with `parentId` + `message.meta.source`
  (`~/.commandcode/projects/<project>/<session-id>.jsonl`).
- opencode: materialized records carrying a `message` object and a `parts` list.

Each parser exposes `parse_X(data, trace_id, user_id=None,
project_name=None) -> (TraceData, [SpanData])`. They all emit the
same span shape — a root Session span plus per-turn Turn spans plus
per-tool tool spans - so downstream code doesn't branch on agent.
"""

from __future__ import annotations

from agentplay._core.ingestion.coding_agent._sources.local.parsers.antigravity import parse_antigravity
from agentplay._core.ingestion.coding_agent._sources.local.parsers.base import (
    AgentType,
    detect_agent,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.claude_code import parse_claude_code
from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex
from agentplay._core.ingestion.coding_agent._sources.local.parsers.command_code import (
    parse_command_code,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.copilot import parse_copilot
from agentplay._core.ingestion.coding_agent._sources.local.parsers.copilot_vscode import (
    parse_copilot_vscode,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_agent_transcript import (
    parse_cursor_agent_transcript,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
    parse_cursor_store,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.gemini import parse_gemini
from agentplay._core.ingestion.coding_agent._sources.local.parsers.kiro import parse_kiro
from agentplay._core.ingestion.coding_agent._sources.local.parsers.opencode import parse_opencode

__all__ = [
    "AgentType",
    "detect_agent",
    "parse_antigravity",
    "parse_claude_code",
    "parse_codex",
    "parse_command_code",
    "parse_copilot",
    "parse_copilot_vscode",
    "parse_cursor_agent_transcript",
    "parse_cursor_store",
    "parse_gemini",
    "parse_kiro",
    "parse_opencode",
]
