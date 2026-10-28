"""What the person has set up for Claude Code on this Mac, and how much each piece is used.

Skills, subagents, slash commands and MCP servers are listed from where Claude Code keeps them (the user's
``~/.claude`` and the plugins it has installed); hooks from ``settings.json``. Uses come from the transcripts
(``usage._setup_line``): a Skill call, an Agent call with that subagent type, a slash command, an MCP tool. Only
names that match something installed are kept, so nothing a person typed is ever stored.

Each item gets a plain verdict a menu can show: in use, rarely used, or unused. Unused skills and MCP servers are
worth knowing about: their descriptions are loaded into every session, used or not.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9_.:@-]{1,80}$")
RARE = 3  # fewer uses than this in 30 days is "rarely used"


def _frontmatter_name(md: Path) -> str | None:
    """The ``name:`` in a SKILL.md or agent file's front matter, if it has one."""
    try:
        head = md.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    m = re.search(r"^name:\s*([^\n]+)$", head.split("---", 2)[1] if head.count("---") >= 2 else "", re.M)
    name = m.group(1).strip().strip("\"'") if m else None
    return name if name and _NAME.match(name) else None


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def installed(claude_home: Path, claude_json: Path | None = None) -> list[dict[str, Any]]:
    """Every skill, subagent, slash command, MCP server and hook Claude Code would load for this user."""
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()

    def add(kind: str, name: str | None, source: str, since: float | None = None, detail: str | None = None) -> None:
        if not name or not _NAME.match(name) or (kind, name, detail) in seen:
            return
        seen.add((kind, name, detail))
        items.append({"kind": kind, "name": name, "source": source,
                      "installedAt": datetime.fromtimestamp(since, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if since else None,
                      "detail": detail})

    def scan(root: Path, source: str) -> None:
        for md in sorted((root / "skills").glob("*/SKILL.md")):
            add("skill", _frontmatter_name(md) or md.parent.name, source, _mtime(md))
        for md in sorted((root / "agents").glob("*.md")):
            add("agent", _frontmatter_name(md) or md.stem, source, _mtime(md))
        for md in sorted((root / "commands").glob("*.md")):
            add("command", md.stem, source, _mtime(md))

    scan(claude_home, "you")
    # Plugins: each installed plugin's own skills, agents and commands.
    try:
        plugins = json.loads((claude_home / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")).get("plugins", {})
    except (OSError, ValueError, AttributeError):
        plugins = {}
    for pid, installs in plugins.items():
        for inst in installs if isinstance(installs, list) else []:
            path = Path(str(inst.get("installPath") or ""))
            if path.is_dir():
                scan(path, "plugin " + pid.split("@")[0])
    # MCP servers: ~/.claude.json and settings.json.
    for f in (claude_json, claude_home / "settings.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8")) if f else {}
        except (OSError, ValueError):
            d = {}
        for name in (d.get("mcpServers") or {}):
            add("mcp", str(name), "you", _mtime(f) if f else None)
    # Hooks: the event and the script name, never its arguments.
    try:
        hooks = json.loads((claude_home / "settings.json").read_text(encoding="utf-8")).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        hooks = {}
    for event, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                from .companion import _hook_executable  # the program's file name only: no arguments, env or paths
                script = _hook_executable(h.get("command")) or ""
                matcher = g.get("matcher")
                add("hook", f"{event}{':' + matcher if matcher else ''}", "you", None, script or None)
    return items


def _keys(it: dict[str, Any]) -> list[tuple[str, str]]:
    """The use names an installed item answers to. Typing /name runs a skill too; a plugin's skill is also
    called "plugin:skill"."""
    keys = [(it["kind"], it["name"])]
    if it["kind"] == "skill":
        keys.append(("command", it["name"]))
        if it["source"].startswith("plugin "):
            plugin = it["source"].removeprefix("plugin ")
            keys += [("skill", f"{plugin}:{it['name']}"), ("command", f"{plugin}:{it['name']}")]
    return keys


BUILT_IN_AGENTS = {"general-purpose", "Explore", "Plan", "statusline-setup", "output-style-setup"}


def _label(kind: str, name: str) -> tuple[str, str]:
    """Where an item seen in sessions but not installed here comes from, and the name to show."""
    if kind == "mcp" and name.startswith("claude_ai_"):
        return name.removeprefix("claude_ai_").replace("_", " "), "claude.ai connector"
    if kind == "agent" and name in BUILT_IN_AGENTS:
        return name, "built in"
    return name, "seen in sessions"


def report(claude_home: Path, uses: list[list[Any]], now: datetime, claude_json: Path | None = None,
           days: int = 30, measured: dict[str, Any] | None = None) -> dict[str, Any]:
    """The inventory with 30-day use counts and a verdict per item, most used first, unused last.

    ``measured`` (impact.measure over the session maps): each item it covers gets ``measured``, its calls,
    failures, context taken in, linked sessions and next step; items seen in sessions but not installed here
    (claude.ai connectors, built-in subagents) are added. Then calls from the maps are its use count."""
    since = (now - timedelta(days=days)).timestamp()
    count: Counter[tuple[str, str]] = Counter()
    last: dict[tuple[str, str], int] = {}
    for sec, kind, name in uses:
        if sec < since:
            continue
        count[(kind, name)] += 1
        last[(kind, name)] = max(last.get((kind, name), 0), sec)
    items = installed(claude_home, claude_json)
    by = {(m["kind"], m["name"]): m for m in (measured or {}).get("items", [])}
    matched: set[tuple[str, str]] = set()
    for it in items:
        hits = [by[k] for k in _keys(it) if k in by and k not in matched]
        if hits:
            matched.update((h["kind"], h["name"]) for h in hits)
            it["measured"] = {k: v for k, v in hits[0].items() if k not in ("kind", "name")}
    for key, m in by.items():
        if key not in matched:
            label, source = _label(*key)
            items.append({"kind": key[0], "name": label, "source": source, "installedAt": None, "detail": None,
                          "measured": {k: v for k, v in m.items() if k not in ("kind", "name")}})
    for it in items:
        keys = _keys(it)
        n = sum(count.get(k, 0) for k in keys)
        at = max((last.get(k, 0) for k in keys), default=0)
        it["uses"] = it["measured"]["calls"] if it.get("measured") else n
        n = it["uses"]
        it["lastUsed"] = datetime.fromtimestamp(at, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if at else None
        if it["kind"] == "hook":
            it["verdict"] = "runs"
        elif n >= RARE:
            it["verdict"] = "used"
        elif n > 0:
            it["verdict"] = "rare"
        else:
            it["verdict"] = "unused"
    items.sort(key=lambda i: (i["kind"] == "hook", -i["uses"], i["kind"], i["name"]))
    kinds = Counter(i["kind"] for i in items)
    return {"days": days, "counts": dict(kinds), "sessions": (measured or {}).get("sessions"),
            "unused": sum(1 for i in items if i["verdict"] == "unused" and i["kind"] in ("skill", "mcp", "agent")),
            "items": items}
