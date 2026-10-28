"""Suggestions from Claude, at most once a day, through the person's own Claude Code. Off unless they turn it on.

What is sent: counts (run health and its parts, steering rates, failed-command names), the titles of the fixes
AgentPlay found, and the person's installed skills, subagents and MCP servers by name with their use counts and
the one-line description each skill or subagent declares. Never a prompt, a message, a file path or code.

How: ``claude -p`` on Haiku, with every tool off, no settings sources (so none of the person's hooks run), no
MCP servers, no session saved, from a folder of AgentPlay's own. One call is a few thousand tokens.

What comes back is checked before it is kept: at most three suggestions, short plain strings, and a skill name
only if it is one the person actually has installed.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .store import Store, _atomic_write

MODEL = "haiku"
EVERY = timedelta(hours=20)  # "once a day", with slack so a daily refresh never skips a day
TIMEOUT_S = 120
MAX_SUGGESTIONS = 3
KINDS = ("use-skill", "remove", "habit")


def _cache(store: Store) -> Path:
    return store.base / "cache" / "companion-coach.json"


def cached(store: Store) -> dict[str, Any] | None:
    """The last suggestions, for the snapshot. Never runs anything."""
    try:
        d = json.loads(_cache(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("suggestions"), list) else None


def _description(md: Path) -> str | None:
    """The one-line ``description:`` a SKILL.md or agent file declares in its front matter."""
    try:
        head = md.read_text(encoding="utf-8", errors="replace")[:3000]
    except OSError:
        return None
    if not head.startswith("---") or head.count("---") < 2:
        return None
    m = re.search(r"^description:\s*(.+)$", head.split("---", 2)[1], re.M)
    if not m:
        return None
    text = m.group(1).strip().strip("\"'>|").strip()
    return text[:240] or None


def descriptions(claude_home: Path) -> dict[tuple[str, str], str]:
    """``(kind, name) -> description`` for the person's own skills and subagents."""
    out: dict[tuple[str, str], str] = {}
    for md in sorted((claude_home / "skills").glob("*/SKILL.md")):
        if d := _description(md):
            out[("skill", md.parent.name)] = d
    for md in sorted((claude_home / "agents").glob("*.md")):
        if d := _description(md):
            out[("agent", md.stem)] = d
    return out


def facts(snap: dict[str, Any], claude_home: Path) -> dict[str, Any]:
    """Everything the prompt says about this person: counts and names, nothing they wrote."""
    usage = snap.get("usage") or {}
    ins = snap.get("insights") or {}
    sig = ((usage.get("signals") or {}).get("current")) or {}
    setup = usage.get("setup") or {}
    desc = descriptions(claude_home)
    items = []
    for it in setup.get("items", []):
        if it.get("kind") == "hook":
            continue
        row = {"kind": it.get("kind"), "name": it.get("name"), "uses30d": it.get("uses", 0), "status": it.get("verdict")}
        if it.get("runBy"):
            row["runAutomaticallyByHook"] = it["runBy"]
        if it.get("worksWith"):
            row["drivesSubagents"] = it["worksWith"]
        if d := desc.get((it.get("kind"), it.get("name"))):
            row["description"] = d
        if m := it.get("measured"):
            row.update({"failedCalls": m.get("failed"), "tokensIntoContext": m.get("tokens"),
                        "sessions": m.get("sessions"), "yourSessions": m.get("yours"),
                        "comparedToYourOtherSessions": (m.get("compare") or {}).get("verdict"),
                        "flag": (m.get("advice") or {}).get("kind")})
        items.append(row)
    learn = snap.get("learn") or {}
    return {
        "runHealth": ins.get("score"),
        "runHealthParts": {c.get("name"): c.get("points") for c in ins.get("components", []) if isinstance(c, dict)},
        "runHealthPartsOutOf": 20,
        "steering": {k: sig.get(k) for k in ("messages", "correctionRate", "frustrationRate", "interruptRate", "refusalRate")},
        # Already in front of the person: Claude must not repeat any of these.
        "alreadyShown": [f.get("title") for f in ins.get("fixes", [])][:6] + [t.get("title") for t in setup.get("tips", [])],
        "fixesApplied": [{"title": a.get("title"), "verdict": a.get("verdict")} for a in (ins.get("applied") or [])][:5],
        "repeatedlyFailingCommand": learn.get("command"),
        "sessions": setup.get("sessions"),  # yours (2+ messages) and automated one-prompt runs, with cost
        "setup": items,
    }


PROMPT = """You coach one developer on how they use Claude Code. Below are measured facts about their last 30 \
days: counts and names only. Suggest up to three changes that would make their sessions go better, best first. \
An empty list is a good answer when nothing clears the bar below.

Each suggestion must:
- point at something the numbers show, and quote the number in "why";
- not repeat or restate anything in "alreadyShown" (those are on screen already);
- never tell them to use a skill or subagent whose status is "used", or that a hook already runs
  ("runAutomaticallyByHook"), or that "drivesSubagents" that were used: they already use it;
- name only a skill or subagent from "setup".

Kinds: "use-skill" (a skill they have but rarely use that fits a problem in the numbers), "remove" (setup they
never use), "habit" (a change in how they work, backed by a number). Plain words a non-programmer understands, no
jargon, no em dashes. Each title under 60 characters, each why under 160.

Reply with JSON only, no prose:
{"suggestions": [{"kind": "use-skill|remove|habit", "title": "...", "why": "...", "name": "skill or subagent name, or null"}]}

Facts:
"""


def _clean(text: Any, limit: int) -> str | None:
    if not isinstance(text, str):
        return None
    t = re.sub(r"\s+", " ", text.replace("—", ", ").replace("–", "-")).strip()
    return t[:limit] if t else None


def _words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) > 3}


def parse(reply: str, installed: set[str], in_use: set[str] | None = None,
          shown: list[str] | None = None) -> list[dict[str, Any]]:
    """The suggestions in Claude's reply, checked: known kinds, short strings, installed names only. Dropped: a
    "use more" for something already in use, a fix (the fix list has those), and anything that mostly restates
    a line already on screen."""
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for s in d.get("suggestions", []) if isinstance(d, dict) else []:
        if not isinstance(s, dict):
            continue
        title, why = _clean(s.get("title"), 80), _clean(s.get("why"), 200)
        kind = s.get("kind") if s.get("kind") in KINDS else "habit"
        if not title or not why:
            continue
        name = s.get("name") if isinstance(s.get("name"), str) and s.get("name") in installed else None
        if s.get("kind") == "fix" or (kind == "use-skill" and (name is None or name in (in_use or set()))):
            continue
        w = _words(title)
        if any(w and len(w & _words(x)) / len(w) >= 0.5 for x in shown or []):
            continue
        out.append({"kind": kind, "title": title, "why": why, "name": name})
    return out[:MAX_SUGGESTIONS]


def run(store: Store, claude: str, snap: dict[str, Any], claude_home: Path, now: datetime | None = None,
        force: bool = False) -> dict[str, Any]:
    """Ask once, unless the last answer is under a day old. Returns what the snapshot will show."""
    now = now or datetime.now(UTC)
    have = cached(store)
    if have and not force:
        try:
            at = datetime.fromisoformat(str(have.get("generatedAt")).replace("Z", "+00:00"))
            if now - at < EVERY:
                return {**have, "ran": False}
        except ValueError:
            pass
    f = facts(snap, claude_home)
    installed = {str(i["name"]) for i in f["setup"] if i.get("name")}
    in_use = {str(i["name"]) for i in f["setup"] if i.get("status") == "used" or i.get("runAutomaticallyByHook")}
    cwd = store.base / "coach"  # AgentPlay's own folder: no project's CLAUDE.md, one empty entry in ~/.claude
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [claude, "-p", "--model", MODEL, "--tools", "", "--setting-sources", "", "--strict-mcp-config",
           "--no-session-persistence", "--output-format", "json"]
    # The app starts the core with a minimal environment; Claude Code finds its Keychain login by user name.
    env = dict(os.environ)
    for k in ("USER", "LOGNAME"):
        env.setdefault(k, getpass.getuser())
    p = subprocess.run(cmd, input=PROMPT + json.dumps(f, indent=1), capture_output=True, text=True,
                       timeout=TIMEOUT_S, cwd=cwd, env=env, check=False)
    try:
        res = json.loads(p.stdout)
    except ValueError as e:
        raise RuntimeError(f"claude returned no JSON (exit {p.returncode})") from e
    if res.get("is_error"):
        raise RuntimeError(_clean(res.get("result"), 200) or "claude reported an error")
    u = res.get("usage") or {}
    out = {"generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "model": MODEL,
           "tokens": sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens")),
           "suggestions": parse(str(res.get("result") or ""), installed, in_use, f["alreadyShown"])}
    _atomic_write(_cache(store), json.dumps(out).encode("utf-8"))
    return {**out, "ran": True}
