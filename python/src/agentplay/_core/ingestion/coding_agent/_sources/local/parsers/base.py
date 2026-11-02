"""Agent detection from a JSONL file head.

Sniffs the head lines for marker shapes. Returns None when no
known agent matches — `LocalSource` skips those files rather than
guessing.
"""

from __future__ import annotations

from enum import StrEnum
import json

from agentplay._core.ingestion.coding_agent._sources.local.parsers.antigravity import is_antigravity_step


class AgentType(StrEnum):
    """Agent enum used by the parsers + LocalSource dispatch.

    The string values are the agent names stamped on sessions.
    """

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    COPILOT = "copilot"
    COPILOT_VSCODE = "copilot-vscode"
    OPENCODE = "opencode"
    CURSOR = "cursor"
    KIRO = "kiro"
    GEMINI_CLI = "gemini-cli"
    ANTIGRAVITY_CLI = "antigravity-cli"
    COMMAND_CODE = "command-code"


def detect_agent(data: bytes) -> AgentType | None:
    """Detect the agent that produced this transcript.

    Returns None when no marker matches — the caller treats that as
    "skip this file" rather than guessing.
    """
    # Every line the caller handed us, not a fixed count: see the note in
    # `_read_head`. It returns complete lines and already bounds the read.
    # Transcripts can open with many lines of `ai-title` / `agent-name` /
    # `permission-mode` metadata before the first `user` record, so a line cap
    # here would miss valid files.
    for raw_line in data.split(b"\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            # UnicodeDecodeError: a fixed-size byte head can split a
            # multi-byte UTF-8 char at the boundary (unicode
            # in transcripts). RecursionError: a deeply nested line.
            # Skip the unparseable line, don't crash the whole index walk.
            continue

        # A bare JSON primitive/array is valid JSON but not an agent header
        # line; `.get()` on it would raise AttributeError and abort the whole
        # index walk and drop every session in the directory.
        if not isinstance(obj, dict):
            continue

        line_type = obj.get("type", "")

        # Cursor CLI/agent: synthesized header line, always first (see
        # agentplay.materialize.find_cursor_store_candidates).
        # Distinct enough (no other agent emits this `type`) that checking
        # it anywhere in the head, not just line 1, is safe.
        if line_type == "cursor_store_session":
            return AgentType.CURSOR

        # Kiro: synthesized header line, always first (see
        # agentplay.materialize.find_kiro_candidates). Same
        # rationale as Cursor's — no other agent emits this `type`.
        if line_type == "kiro_session":
            return AgentType.KIRO

        # Gemini CLI: the first line is a header with `sessionId`, a
        # `projectHash` string and NO `type` (Claude Code's records all carry
        # one), and messages are typed `user` / `gemini`. `gemini` is a type no
        # other agent emits, so it identifies a file whose header was cut off.
        if (
            "type" not in obj
            and isinstance(obj.get("sessionId"), str)
            and isinstance(obj.get("projectHash"), str)
        ):
            return AgentType.GEMINI_CLI
        if line_type == "gemini" and isinstance(obj.get("id"), str):
            return AgentType.GEMINI_CLI

        if is_antigravity_step(obj):
            return AgentType.ANTIGRAVITY_CLI

        # Command Code: the first line is a `type="session"` header with a
        # string `id` and `cwd` and an int format `version`; no other agent
        # emits that `type`. Its messages are `type="message"` records whose
        # `parentId` (Claude Code's is `parentUuid`) and `message.meta.source`
        # identify a file whose header was cut off.
        if (
            line_type == "session"
            and isinstance(obj.get("id"), str)
            and isinstance(obj.get("cwd"), str)
            and isinstance(obj.get("version"), int)
        ):
            return AgentType.COMMAND_CODE
        if line_type == "message" and "parentId" in obj:
            message = obj.get("message")
            meta = message.get("meta") if isinstance(message, dict) else None
            if isinstance(meta, dict) and isinstance(meta.get("source"), str):
                return AgentType.COMMAND_CODE

        # Codex: session_meta with codex originator.
        if line_type == "session_meta":
            payload = obj.get("payload")
            # `.get("payload", {})` only defaults a MISSING key: an explicit null
            # (or a list) came through as-is and `.get` on it aborted detection,
            # and with it the whole index walk, on one malformed line.
            payload = payload if isinstance(payload, dict) else {}
            originator = payload.get("originator", "")
            originator = originator if isinstance(originator, str) else ""
            # Case-insensitive: the Codex CLI/TUI write `codex_cli_rs` / `codex-tui`,
            # but the Codex Desktop app writes `Codex Desktop` (capital C) — a
            # case-sensitive check would miss every Desktop session.
            if originator.lower().startswith("codex"):
                return AgentType.CODEX
            # A launcher wrapping the Codex CLI overwrites
            # `originator` with its own name, so the check above misses it.
            # `model_provider` is a field Codex's own session_meta schema
            # always carries (see codex.py's parse_codex, which relies on it
            # being present there and only there); no other agent's `type`
            # is `session_meta`, so its presence here is Codex-specific
            # without hardcoding a launcher name.
            if isinstance(payload.get("model_provider"), str) and payload["model_provider"]:
                return AgentType.CODEX

        # Copilot: session.start with producer=copilot-agent.
        if line_type == "session.start":
            data_field = obj.get("data")
            if isinstance(data_field, dict) and data_field.get("producer") == "copilot-agent":
                return AgentType.COPILOT

        # Copilot in VS Code (Chat/agent mode): the .jsonl snapshot-and-patch
        # log's FIRST line is always the kind=0 snapshot envelope, {"kind": 0,
        # "v": {<session doc with "requests">}}. No other agent uses a bare
        # top-level "kind" int (every branch above keys on "type", "role" or
        # "message"), so this is unambiguous without needing the nested check
        # -- the "requests" key is still required to reject an unrelated format
        # that happens to reuse "kind": 0 for something else entirely. The
        # sibling .json (single whole document, pretty-printed) format can't be
        # detected here at all -- this scanner parses per LINE, and a
        # pretty-printed document has no single self-contained JSON line -- so
        # `_index_copilot_vscode_file` checks that shape directly instead.
        if obj.get("kind") == 0:
            snapshot = obj.get("v")
            if isinstance(snapshot, dict) and "requests" in snapshot:
                return AgentType.COPILOT_VSCODE

        # Claude Code: type=user|assistant + sessionId field.
        if line_type in ("user", "assistant") and "sessionId" in obj:
            return AgentType.CLAUDE_CODE

        # opencode: materialized records carry a `message` object + `parts` list (and a
        # capital-ID `sessionID`); no top-level `type`, so this can't collide with Claude Code.
        message = obj.get("message")
        if (
            isinstance(message, dict)
            and message.get("role") in ("user", "assistant")
            and isinstance(obj.get("parts"), list)
        ):
            return AgentType.OPENCODE

        # Cursor agent-transcript (third Cursor format): a TOP-LEVEL `role` +
        # a `message.content` object, with NO `type`/`sessionId` (Claude Code /
        # Codex / cursor_store / kiro all carry `type`) and NO `parts` (opencode).
        # Files live at `~/.cursor/projects/<slug>/agent-transcripts/<uuid>/*.jsonl`.
        # Returns CURSOR so it shares the Cursor home/index; `_index_cursor_store_file`
        # re-sniffs the head to pick the agent-transcript vs store.db parser.
        if (
            obj.get("role") in ("user", "assistant")
            and "type" not in obj
            and "sessionId" not in obj
            and "parts" not in obj
            and isinstance(message, dict)
            and "content" in message
        ):
            return AgentType.CURSOR

    return None


__all__ = ["AgentType", "detect_agent"]
