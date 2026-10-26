"""Find coding-agent sessions on this machine and build maps for them.

Supported agents and where each keeps its sessions by default:

=================  =====================================================  ===========
agent              default location                                       read as
=================  =====================================================  ===========
Claude Code        ``~/.claude/projects/**/*.jsonl`` (+ ``subagents/``)   file
Codex              ``~/.codex/sessions/**/*.jsonl``                       file
Copilot CLI        ``~/.copilot/session-state/**``                        file
Copilot VS Code    VS Code ``User/workspaceStorage/*/chatSessions``       file
Cursor             ``~/.cursor`` agent transcripts, ``chats/*/store.db``,  decoded
                   IDE ``state.vscdb``
OpenCode           ``~/.local/share/opencode/opencode.db``                decoded
Kiro               ``~/.kiro/sessions``                                   decoded
Gemini CLI         ``~/.gemini/tmp/*/chats/session-*.jsonl``              file
Antigravity CLI    ``~/.gemini/antigravity-cli/brain/*`` step logs        file
Windsurf           ``~/.codeium/windsurf``                                file
=================  =====================================================  ===========

"Decoded" agents keep sessions in a database; :mod:`agentplay.materialize`
turns each one into an NDJSON transcript under ``~/.agentplay/cache`` first.

Every location can be overridden with a comma-separated environment variable,
``AGENTPLAY_<AGENT>_HOMES`` (for example ``AGENTPLAY_CLAUDE_HOMES`` for a WSL
``/mnt/c/Users/<you>/.claude``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

from . import builder, materialize
from ._util import captured

logger = logging.getLogger("agentplay.discover")

AGENTS = (
    "claude-code",
    "codex",
    "cursor",
    "copilot",
    "copilot-vscode",
    "opencode",
    "kiro",
    "gemini-cli",
    "antigravity-cli",
    "windsurf",
)

# Transcripts above this size are skipped rather than parsed (a 20k-event map cap
# applies anyway; this keeps one pathological file from stalling a run).
MAX_SOURCE_BYTES = 512 * 1024 * 1024


def _homes_from_env(var: str, defaults: Iterable[str | Path]) -> list[Path]:
    raw = os.environ.get(var, "").strip()
    if raw:
        return [Path(os.path.expanduser(p.strip())) for p in raw.split(",") if p.strip()]
    return [Path(os.path.expanduser(str(d))) for d in defaults]


def _vscode_user_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [base / app / "User" for app in ("Code", "Code - Insiders", "VSCodium")]


def _opencode_data_dirs() -> list[Path]:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return [base / "opencode"]


@dataclass(frozen=True)
class Homes:
    """Resolved agent home directories (the raw agent stores, before decoding)."""

    claude_code: list[Path]
    codex: list[Path]
    cursor: list[Path]
    copilot: list[Path]
    copilot_vscode: list[Path]
    opencode: list[Path]
    kiro: list[Path]
    gemini_cli: list[Path]
    antigravity_cli: list[Path]
    windsurf: list[Path]

    @classmethod
    def default(cls) -> Homes:
        copilot_default = os.environ.get("COPILOT_HOME", "").strip() or "~/.copilot"
        return cls(
            claude_code=_homes_from_env("AGENTPLAY_CLAUDE_HOMES", ["~/.claude"]),
            codex=_homes_from_env("AGENTPLAY_CODEX_HOMES", ["~/.codex"]),
            cursor=_homes_from_env("AGENTPLAY_CURSOR_HOMES", ["~/.cursor"]),
            copilot=_homes_from_env("AGENTPLAY_COPILOT_HOMES", [copilot_default]),
            copilot_vscode=_homes_from_env("AGENTPLAY_COPILOT_VSCODE_HOMES", _vscode_user_dirs()),
            opencode=_homes_from_env("AGENTPLAY_OPENCODE_HOMES", _opencode_data_dirs()),
            kiro=_homes_from_env("AGENTPLAY_KIRO_HOMES", ["~/.kiro"]),
            gemini_cli=_homes_from_env("AGENTPLAY_GEMINI_HOMES", ["~/.gemini"]),
            antigravity_cli=_homes_from_env("AGENTPLAY_ANTIGRAVITY_HOMES", ["~/.gemini/antigravity-cli"]),
            windsurf=_homes_from_env("AGENTPLAY_WINDSURF_HOMES", ["~/.codeium/windsurf"]),
        )

    def present(self) -> dict[str, list[Path]]:
        """Only the homes that exist on this machine, by agent name."""
        out: dict[str, list[Path]] = {}
        for agent, paths in (
            ("claude-code", self.claude_code),
            ("codex", self.codex),
            ("cursor", self.cursor),
            ("copilot", self.copilot),
            ("copilot-vscode", self.copilot_vscode),
            ("opencode", self.opencode),
            ("kiro", self.kiro),
            ("gemini-cli", self.gemini_cli),
            ("antigravity-cli", self.antigravity_cli),
            ("windsurf", self.windsurf),
        ):
            found = [p for p in paths if p.is_dir()]
            if found:
                out[agent] = found
        return out


def materialize_all(homes: Homes, agents: set[str]) -> dict[str, int]:
    """Decode database-backed agents into the local NDJSON cache.

    Args:
        homes: resolved agent homes.
        agents: agent names to include.

    Returns:
        Sessions decoded per agent. Failures are logged and counted as zero:
        one unreadable store never stops the run.
    """
    counts: dict[str, int] = {}
    kinds = materialize.AgentKind
    jobs: list[tuple[str, Any]] = []
    if "opencode" in agents:
        for h in homes.opencode:
            if h.is_dir():
                jobs.append(("opencode", lambda h=h: materialize.find_opencode_candidates(
                    materialize.HomeSpec(kind=kinds.OPENCODE, path=h))))
    if "kiro" in agents:
        for h in homes.kiro:
            if h.is_dir():
                jobs.append(("kiro", lambda h=h: materialize.find_kiro_candidates(
                    materialize.HomeSpec(kind=kinds.KIRO, path=h))))
    if "cursor" in agents:
        for h in homes.cursor:
            if h.is_dir():
                jobs.append(("cursor", lambda h=h: materialize.find_cursor_store_candidates(
                    materialize.HomeSpec(kind=kinds.CURSOR, path=h))))
        if materialize._default_state_db_path().is_file():  # noqa: SLF001
            jobs.append(("cursor", materialize.find_cursor_composer_candidates))
    for agent, job in jobs:
        n = 0
        try:
            for _ in job():
                n += 1
        except Exception as exc:  # noqa: BLE001 - decoding is best-effort per store
            logger.warning("could not decode %s sessions: %s", agent, exc)
        counts[agent] = counts.get(agent, 0) + n
    return counts


def _cache_dirs(agent: str) -> list[Path]:
    root = materialize.cache_root()
    if agent == "opencode":
        return [p for p in (root / "opencode").glob("*") if p.is_dir()]
    if agent == "kiro":
        return [p for p in (root / "kiro").glob("*") if p.is_dir()]
    if agent == "cursor":
        return [p for p in (root / "cursor-store").glob("*") if p.is_dir()] + [
            p for p in (root / "cursor-composer").glob("*") if p.is_dir()
        ]
    return []


def local_source(homes: Homes, agents: set[str]) -> Any:
    """The shared multi-agent parser over every present home plus the decoded cache."""
    from ._core.ingestion.coding_agent._sources.local.source import LocalSource

    def pick(agent: str, raw: list[Path]) -> list[Path]:
        if agent not in agents:
            return []
        return [p for p in raw if p.is_dir()]

    return LocalSource(
        claude_code_homes=pick("claude-code", homes.claude_code),
        codex_homes=pick("codex", homes.codex),
        opencode_homes=pick("opencode", _cache_dirs("opencode")),
        cursor_store_homes=pick("cursor", homes.cursor + _cache_dirs("cursor")),
        kiro_homes=pick("kiro", _cache_dirs("kiro")),
        copilot_homes=pick("copilot", homes.copilot),
        copilot_vscode_homes=pick("copilot-vscode", homes.copilot_vscode),
        gemini_homes=pick("gemini-cli", homes.gemini_cli),
        antigravity_homes=pick("antigravity-cli", homes.antigravity_cli),
        windsurf_homes=pick("windsurf", homes.windsurf),
    )


@dataclass
class Candidate:
    """One session found on disk."""

    session_id: str
    agent: str
    path: str
    mtime: float


def _agent_name(raw: Any) -> str:
    name = str(getattr(raw, "value", raw) or "")
    return {"claude_code": "claude-code", "copilot_vscode": "copilot-vscode"}.get(name, name)


def candidates(source: Any, *, since: datetime | None = None) -> list[Candidate]:
    """Every session the parser can see, newest first.

    Args:
        source: a ``LocalSource`` from :func:`local_source`.
        since: only sessions whose transcript changed at or after this time.
    """
    index = source._build_index()  # noqa: SLF001 - the only path -> session id map
    out: list[Candidate] = []
    floor = since.timestamp() if since else None
    for sid, ref in index.items():
        with captured():
            mtime = ref.mtime.timestamp() if isinstance(ref.mtime, datetime) else float(ref.mtime or 0)
            if floor is not None and mtime < floor:
                continue
            out.append(Candidate(sid, _agent_name(ref.agent), str(ref.path), mtime))
    out.sort(key=lambda c: c.mtime, reverse=True)
    return out


def source_bytes(c: Candidate) -> int:
    """On-disk size of a session's transcript plus Claude Code subagent files."""
    from ._core.ingestion.coding_agent._sources.local.source import claude_code_subagent_files

    total = 0
    with captured():
        p = Path(c.path)
        if p.is_file():
            total += p.stat().st_size
            total += sum(f.stat().st_size for f in claude_code_subagent_files(p))
    return total


class SkipSession(Exception):
    """A session that cannot or should not be mapped (too large, empty)."""


def build_map(c: Candidate, source: Any, *, path_mode: str = "relative",
              hmac_key: bytes | None = None, version: str = "",
              resolver: builder.RepoResolver | None = None) -> dict[str, Any]:
    """Build the ``agentplay/v1`` document for one candidate.

    Claude Code is read straight from its JSONL (the only way to see subagent
    tool calls and per-request tokens). Every other agent goes through the
    shared parser's spans.

    Raises:
        SkipSession: the transcript is too large or has no parseable spans.
    """
    if source_bytes(c) > MAX_SOURCE_BYTES:
        raise SkipSession("too_large")
    if c.agent == "claude-code" and c.path.endswith(".jsonl"):
        return builder.build_claude_code_map(
            Path(c.path), path_mode=path_mode, hmac_key=hmac_key, cli_version=version, resolver=resolver
        )
    spans = source.get_spans_by_session(c.session_id)
    if not spans:
        raise SkipSession("no_spans")
    # A subagent stored as its own conversation (Antigravity CLI) carries its role and parent on the root span.
    root = next((s for s in spans if s.parent_id is None), None)
    root_attrs = root.attributes if root is not None else {}
    return builder.build_map_from_spans(
        spans,
        session_id=c.session_id,
        agent_tool=c.agent or "unknown",
        path_mode=path_mode,
        hmac_key=hmac_key,
        cli_version=version,
        role=root_attrs.get("harness.session_role") or None,
        parent_session_id=root_attrs.get("harness.parent_session_id") or None,
        resolver=resolver,
        started_at_hint=c.mtime or None,
    )


# --------------------------------------------------------------------- titles
_TITLE_MAX = 90
# Task labels sit in a narrow sidebar column, so they get less room than a title.
_TASK_TITLE_MAX = 60

# Blocks the harness wraps around a turn. They often *prefix* a real prompt, so
# dropping the whole turn on sight of a leading "<" throws the prompt away with
# them: a worktree `<system-reminder>` would hide "set up the staging deploy
# script" entirely.
_HARNESS_TAGS = (
    "system-reminder", "task-notification", "ci-monitor-event",
    "local-command-caveat", "local-command-stdout", "local-command-stderr",
    "command-name", "command-message", "command-args",
    "user-prompt-submit-hook",
)
_HARNESS_BLOCK_RE = re.compile(
    r"<(" + "|".join(_HARNESS_TAGS) + r")\b[^>]*>.*?</\1>|<(" + "|".join(_HARNESS_TAGS) + r")\b[^>]*/?>",
    re.S | re.I,
)
# A whole turn the harness wrote on the user's behalf: no prompt in it at all.
_HARNESS_MARKER_RE = re.compile(r"^\[Request interrupted by user[^\]]*\]$", re.I)
# Attachment placeholders sit in front of real prose ("[Image #1] yesterday is ...").
_ATTACHMENT_RE = re.compile(r"^(?:\[(?:Image|Pasted text|Attachment)[^\]]*\]\s*)+", re.I)


def _clean_title(text: str, limit: int = _TITLE_MAX) -> str | None:
    text = _HARNESS_BLOCK_RE.sub(" ", str(text))
    text = " ".join(text.split())
    text = _ATTACHMENT_RE.sub("", text).strip()
    if not text or _HARNESS_MARKER_RE.match(text) or text.startswith("<") or text.startswith("Caveat:"):
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def local_title(c: Candidate, source: Any | None = None) -> str | None:
    """A human title for the local session list: Claude Code's own ``ai-title`` /
    ``summary`` record when present, else the first real user prompt.

    Titles live only in the local index on this machine. They are never written
    into a map document, which stays free of prompt text by contract.
    """
    with captured():
        if c.agent == "claude-code" and c.path.endswith(".jsonl"):
            first_prompt = None
            with open(c.path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 4000:
                        break
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    kind = rec.get("type")
                    if kind in ("ai-title", "summary"):
                        t = _clean_title(rec.get("title") or rec.get("summary") or "")
                        if t:
                            return t
                    if first_prompt is None and kind == "user" and not rec.get("isMeta"):
                        msg = rec.get("message") or {}
                        content = msg.get("content")
                        if isinstance(content, list):
                            content = " ".join(
                                b["text"] for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                                and isinstance(b.get("text"), str)
                            )
                        if isinstance(content, str):
                            first_prompt = _clean_title(content)
            return first_prompt
        if source is not None:
            for s in source.get_spans_by_session(c.session_id):
                if s.name.startswith("Turn "):
                    t = _clean_title(s.attributes.get("input.value") or "")
                    if t:
                        return t
    return None


def local_task_titles(c: Candidate, source: Any | None = None) -> list[str | None] | None:
    """One short label per user turn, for the task list, in turn order.

    A task window starts at a user turn, so the turn's own prompt is the only
    thing on the machine that says what the task was. Without this every task
    renders as "Task 3", which is why the task list cannot be used to pick what
    to review.

    Same contract as :func:`local_title`, and the same ``--no-titles`` switch:
    these live only in the local index on this machine and are merged into a map
    when it is *served*, never when it is built or exported. A map document on
    disk stays free of prompt text, so ``agentplay export`` is unaffected.

    The list is positional: a task is labelled by its ``firstTurn``, which counts
    the ``user`` events the builder emitted. So this walks records with the
    builder's own predicate (:func:`builder.user_turn_text`) and appends one
    entry per *emitted* turn — never for an interruption or a harness-only
    record, each of which would otherwise shift every later label by one. An
    entry may still be ``None`` when the prompt cleans down to nothing; that
    leaves the task unlabelled without moving its neighbours.
    """
    with captured():
        if c.agent == "claude-code" and c.path.endswith(".jsonl"):
            with open(c.path, encoding="utf-8", errors="replace") as fh:
                turns = builder.user_turns(islice(fh, 4001))
            return [_clean_title(text, _TASK_TITLE_MAX) for _, _, text in turns] or None
        if source is not None:
            # `build_map_from_spans` emits a user event only for a turn that carries
            # a prompt, so a turn without one takes no slot here either.
            prompts = [
                s.attributes.get("input.value")
                for s in source.get_spans_by_session(c.session_id)
                if s.name.startswith("Turn ")
            ]
            prompts = [p for p in prompts if p]
            if prompts:
                return [_clean_title(str(p), _TASK_TITLE_MAX) for p in prompts]
    return None


def now_utc() -> datetime:
    return datetime.now(UTC)
