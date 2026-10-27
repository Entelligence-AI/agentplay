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


MIN_SESSIONS = 5     # fewer sessions than this on either side and there's nothing to compare
MIN_MESSAGES = 3     # a session with fewer messages from you says nothing about steering
CLEAR_POINTS = 3.0   # a gap smaller than this, in percentage points, is "no clear difference"


def sessions(files: list[dict[str, Any]], since: float) -> dict[str, dict[str, Any]]:
    """Per session since ``since``: messages from you, corrections, tool calls, failed calls, and what setup it
    used. A session's subagent transcripts add their uses; its signals come from the main transcript only."""
    out: dict[str, dict[str, Any]] = {}
    for f in files:
        sid = str(f.get("session") or "")
        if not sid:
            continue
        s = out.setdefault(sid, {"msg": set(), "cor": set(), "call": set(), "err": set(), "used": set(), "at": 0})
        for e in f.get("sig", []):
            if e[0] >= since and e[1] in ("msg", "cor", "call", "err"):
                s[e[1]].add(e[2] if len(e) > 2 and e[2] else e[0])
                s["at"] = max(s["at"], e[0])
        for u in f.get("use", []):
            if u[0] >= since:
                s["used"].add((u[1], u[2]))
    return {k: {"msg": len(v["msg"]), "cor": len(v["cor"]), "call": len(v["call"]), "err": len(v["err"]),
                "used": v["used"], "at": v["at"]}
            for k, v in out.items() if len(v["msg"]) >= MIN_MESSAGES}


def _rates(group: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    msgs, calls = sum(s["msg"] for s in group), sum(s["call"] for s in group)
    cor = round(100 * sum(s["cor"] for s in group) / msgs, 1) if msgs else None
    err = round(100 * sum(s["err"] for s in group) / calls, 1) if calls else None
    return cor, err


def compare(with_: list[dict[str, Any]], without: list[dict[str, Any]]) -> dict[str, Any]:
    """Sessions with something against sessions without it: correction rate (per message from you) and failed
    tool calls (per call). A verdict only when both sides have enough sessions and the gap is clear. Sessions
    that use a skill differ in other ways too, so this says "goes with", never "caused"."""
    cw, ew = _rates(with_)
    co, eo = _rates(without)
    out: dict[str, Any] = {"with": len(with_), "without": len(without), "correctionWith": cw, "correctionWithout": co,
                           "errorWith": ew, "errorWithout": eo}
    if len(with_) < MIN_SESSIONS or len(without) < MIN_SESSIONS:
        out["verdict"] = "too few"
    elif cw is not None and co is not None and cw <= co - CLEAR_POINTS:
        out["verdict"] = "fewer corrections"
    elif cw is not None and co is not None and cw >= co + CLEAR_POINTS:
        out["verdict"] = "more corrections"
    elif ew is not None and eo is not None and ew <= eo - CLEAR_POINTS:
        out["verdict"] = "fewer failed calls"
    elif ew is not None and eo is not None and ew >= eo + CLEAR_POINTS:
        out["verdict"] = "more failed calls"
    else:
        out["verdict"] = "no clear difference"
    return out


def report(claude_home: Path, uses: list[list[Any]], now: datetime, claude_json: Path | None = None,
           days: int = 30, files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The inventory with 30-day use counts and a verdict per item, most used first, unused last. With
    ``files`` (usage.claude_requests' per-transcript records), each used item also gets ``impact``: how
    sessions that used it went against sessions that didn't."""
    since = (now - timedelta(days=days)).timestamp()
    count: Counter[tuple[str, str]] = Counter()
    last: dict[tuple[str, str], int] = {}
    for sec, kind, name in uses:
        if sec < since:
            continue
        count[(kind, name)] += 1
        last[(kind, name)] = max(last.get((kind, name), 0), sec)
    items = installed(claude_home, claude_json)
    per = list(sessions(files, since).values()) if files else []
    for it in items:
        keys = _keys(it)
        if per and it["kind"] != "hook" and any(count.get(k) for k in keys):
            ks = set(keys)
            it["impact"] = compare([p for p in per if p["used"] & ks], [p for p in per if not p["used"] & ks])
        n = sum(count.get(k, 0) for k in keys)
        at = max((last.get(k, 0) for k in keys), default=0)
        it["uses"] = n
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
    return {"days": days, "counts": dict(kinds), "sessions": len(per),
            "unused": sum(1 for i in items if i["verdict"] == "unused" and i["kind"] in ("skill", "mcp", "agent")),
            "items": items}
