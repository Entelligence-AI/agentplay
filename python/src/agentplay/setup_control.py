"""Turn a skill, MCP server, subagent or hook off and on again, from the companion.

Claude Code's own switches wherever it has one, so `/skills`, `/mcp` and every other tool agree with us:

- a skill: ``skillOverrides: {"name": "off"}`` in ``~/.claude/settings.json`` (hidden from Claude and the / menu);
- an MCP server or claude.ai connector: a ``{"serverName": ...}`` entry in ``deniedMcpServers`` there; a connector
  goes by its display name, ``claude.ai Slack``.

Two have no switch in Claude Code, so AgentPlay sets them aside and remembers where they came from:

- a subagent: its file moves from ``~/.claude/agents`` to ``~/.agentplay/set-aside/agents``;
- a hook: its entry leaves ``hooks`` in ``settings.json`` and is kept in ``~/.agentplay/set-aside/hooks.json``.

Every write takes AgentPlay's write lock, copies ``settings.json`` to ``~/.agentplay/backups`` first, and keeps
the file's own formatting. Turning something back on undoes exactly what turning it off did.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .fixes import FixError, _dump, _read_json, _settings, write_lock
from .store import _atomic_write

KINDS = ("skill", "mcp", "agent", "hook")


def _set_aside(base: Path) -> Path:
    return base / "set-aside"


def _hooks_file(base: Path) -> Path:
    return _set_aside(base) / "hooks.json"


def _settings_path(claude_home: Path) -> Path:
    return claude_home / "settings.json"


def _load(claude_home: Path) -> tuple[dict[str, Any], str | None]:
    p = _settings_path(claude_home)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        text = None
    return _settings(text, "~/.claude/settings.json"), text


def _save(base: Path, claude_home: Path, data: dict[str, Any], like: str | None) -> None:
    p = _settings_path(claude_home)
    if p.exists():
        backups = base / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, backups / f"settings.json.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}")
    _atomic_write(p, _dump(data, like).encode("utf-8"))


def server_name(name: str, source: str | None) -> str:
    """The name Claude Code matches in ``deniedMcpServers``: a connector by its display name."""
    return f"claude.ai {name}" if source == "claude.ai connector" else name


def _agent_file(folder: Path, name: str) -> Path | None:
    from .setup_inventory import _frontmatter_name

    for md in sorted(folder.glob("*.md")):
        if md.stem == name or _frontmatter_name(md) == name:
            return md
    return None


def _hook_entries(groups: Any, script: str) -> list[tuple[int, int]]:
    from .companion import _hook_executable

    out = []
    for gi, g in enumerate(groups if isinstance(groups, list) else []):
        for hi, h in enumerate((g.get("hooks") or []) if isinstance(g, dict) else []):
            if isinstance(h, dict) and _hook_executable(h.get("command")) == script:
                out.append((gi, hi))
    return out


def switch(base: Path, claude_home: Path, kind: str, name: str, on: bool, *, source: str | None = None,
           script: str | None = None) -> dict[str, Any]:
    """Turn one thing off (``on=False``) or back on. Returns what was done, in a sentence."""
    if kind not in KINDS:
        raise FixError("bad_kind", f"AgentPlay can't switch a {kind}.")
    with write_lock(base):
        if kind == "skill":
            if source and source != "you":
                raise FixError("plugin_skill", f"{name} comes with a plugin; Claude Code switches plugins as a whole, in /plugin.")
            data, like = _load(claude_home)
            over = data.get("skillOverrides")
            over = dict(over) if isinstance(over, dict) else {}
            if on:
                over.pop(name, None)
            else:
                over[name] = "off"
            if over:
                data["skillOverrides"] = over
            else:
                data.pop("skillOverrides", None)
            _save(base, claude_home, data, like)
            return {"done": f"{name} is {'back on' if on else 'off'}: skillOverrides in ~/.claude/settings.json."}
        if kind == "mcp":
            data, like = _load(claude_home)
            target = server_name(name, source)
            denied = [d for d in (data.get("deniedMcpServers") or []) if isinstance(d, dict)]
            denied = [d for d in denied if d.get("serverName") != target]
            if not on:
                denied.append({"serverName": target})
            if denied:
                data["deniedMcpServers"] = denied
            else:
                data.pop("deniedMcpServers", None)
            _save(base, claude_home, data, like)
            return {"done": f"{target} is {'allowed again' if on else 'blocked'}: deniedMcpServers in ~/.claude/settings.json. "
                            "Sessions already open keep it until they restart."}
        if kind == "agent":
            live, aside = claude_home / "agents", _set_aside(base) / "agents"
            src, dst_dir = (aside, live) if on else (live, aside)
            f = _agent_file(src, name)
            if f is None:
                raise FixError("not_found", f"No {name} subagent in {src}.")
            dst_dir.mkdir(parents=True, exist_ok=True)
            if (dst_dir / f.name).exists():
                raise FixError("exists", f"{dst_dir / f.name} already exists; move one by hand.")
            shutil.move(str(f), str(dst_dir / f.name))
            return {"done": f"{name} is {'back in ~/.claude/agents' if on else 'set aside in ~/.agentplay/set-aside/agents'}."}
        # hook
        if not script:
            raise FixError("bad_hook", "Which script? A hook is switched by its script name.")
        event = name.split(":")[0]
        data, like = _load(claude_home)
        kept = _read_json(_hooks_file(base), [])
        kept = kept if isinstance(kept, list) else []
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        if on:
            mine = [k for k in kept if k.get("event") == event and k.get("script") == script]
            if not mine:
                raise FixError("not_found", f"AgentPlay hasn't set aside {script} on {event}.")
            for k in mine:
                groups = hooks.setdefault(event, [])
                group = next((g for g in groups if isinstance(g, dict) and g.get("matcher") == k.get("matcher")), None)
                if group is None:
                    group = {"hooks": []} if k.get("matcher") is None else {"matcher": k["matcher"], "hooks": []}
                    groups.append(group)
                group.setdefault("hooks", []).append(k["hook"])
            kept = [k for k in kept if k not in mine]
        else:
            groups = hooks.get(event) or []
            found = _hook_entries(groups, script)
            if not found:
                raise FixError("not_found", f"No {script} hook on {event} in ~/.claude/settings.json.")
            for gi, hi in sorted(found, reverse=True):
                g = groups[gi]
                kept.append({"event": event, "matcher": g.get("matcher"), "script": script, "hook": g["hooks"][hi],
                             "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
                del g["hooks"][hi]
            hooks[event] = [g for g in groups if not isinstance(g, dict) or g.get("hooks")]
            if not hooks[event]:
                del hooks[event]
        if hooks:
            data["hooks"] = hooks
        else:
            data.pop("hooks", None)
        _save(base, claude_home, data, like)
        _set_aside(base).mkdir(parents=True, exist_ok=True)
        _atomic_write(_hooks_file(base), json.dumps(kept, indent=1).encode("utf-8"))
        return {"done": f"{script} {'runs again' if on else 'no longer runs'} on {event}."}


def off_now(base: Path, claude_home: Path) -> dict[str, Any]:
    """What is switched off right now, for the inventory: skills and servers from settings, set-aside
    subagents and hooks from AgentPlay's own folder."""
    try:
        data, _ = _load(claude_home)
    except FixError:
        data = {}
    over = data.get("skillOverrides") if isinstance(data.get("skillOverrides"), dict) else {}
    denied = [d.get("serverName") for d in (data.get("deniedMcpServers") or []) if isinstance(d, dict)]
    agents = sorted((_set_aside(base) / "agents").glob("*.md"))
    kept = _read_json(_hooks_file(base), [])
    return {"skills": sorted(k for k, v in over.items() if v == "off"), "servers": [s for s in denied if s],
            "agents": agents, "hooks": [k for k in kept if isinstance(k, dict)] if isinstance(kept, list) else []}
