"""How each MCP server, skill and subagent actually performs, measured from the session maps AgentPlay built.

Every map step records its tool, whether it failed, how many bytes it returned into the agent's context and
how long it took. That is measured once, at scan time, for every session; this module only adds it up:

- per item, over the last ``days``: calls, failed calls, output put into context (bytes / 4 as tokens), time
  spent, the sessions it ran in, and the most recent of yours that used it, with the step it was first used;
- your sessions apart from one-prompt runs: a session with a single message from you is a script, a bot or a
  ``claude -p`` call (an Agent SDK pipeline, a grader), and those dwarf what a person does by hand;
- among your sessions, those with the item against those without, on failed calls per 100, retries and
  compactions per session and cost per session. A verdict only with ``MIN_SESSIONS`` on each side;
- one plain next step per item, from rules a person can check.

Names only, never text: the maps hold no prompts, and nothing here reads the index's session titles.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import Store

MIN_SESSIONS = 5          # per side, to compare at all
FAILING = 0.2             # a fifth of its own calls failing, over at least FAILING_CALLS, is worth fixing
FAILING_CALLS = 3          # failed calls, at least, before "failing"
HEAVY_TOKENS = 40_000     # this much returned into one session's context, on average, is heavy
BYTES_PER_TOKEN = 4
RECENT = 3                # your latest sessions that used it, linked by id


def _parse(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def item_of(e: dict[str, Any]) -> tuple[str, str] | None:
    """The setup an event belongs to: an MCP server, a skill or a subagent type."""
    if e.get("kind") == "mcp" and e.get("server"):
        return ("mcp", str(e["server"]))
    if e.get("kind") == "skill" and e.get("skill"):
        return ("skill", str(e["skill"]))
    if e.get("kind") == "subagent" and e.get("subagentType"):
        return ("agent", str(e["subagentType"]))
    return None


def _session_stats(row: dict[str, Any]) -> dict[str, float]:
    calls = max(1, int(row.get("calls") or 0))
    return {"errorRate": 100 * int(row.get("errors") or 0) / calls, "retries": float(row.get("retries") or 0),
            "compactions": float(row.get("compactions") or 0), "cost": float(row.get("costUsd") or 0)}


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def compare(with_: list[dict[str, Any]], without: list[dict[str, Any]]) -> dict[str, Any]:
    """Your sessions with the item against your sessions without it."""
    out: dict[str, Any] = {"with": len(with_), "without": len(without)}
    for k in ("errorRate", "retries", "compactions", "cost"):
        out[k + "With"] = _mean([s[k] for s in with_])
        out[k + "Without"] = _mean([s[k] for s in without])
    if len(with_) < MIN_SESSIONS or len(without) < MIN_SESSIONS:
        out["verdict"] = "too few"
        return out
    ew, eo = out["errorRateWith"] or 0, out["errorRateWithout"] or 0
    cw, co = out["compactionsWith"] or 0, out["compactionsWithout"] or 0
    if ew <= eo - 2:
        out["verdict"] = "fewer failed calls"
    elif ew >= eo + 2:
        out["verdict"] = "more failed calls"
    elif cw <= co - 0.3:
        out["verdict"] = "fewer compactions"
    elif cw >= co + 0.3:
        out["verdict"] = "more compactions"
    else:
        out["verdict"] = "no clear difference"
    return out


def advice(s: dict[str, Any]) -> dict[str, str]:
    """One next step, from the item's own numbers first (they are its own), then the comparison."""
    calls, failed = s["calls"], s["failed"]
    per = s["tokens"] / max(1, s["sessions"])
    if failed >= FAILING_CALLS and failed / max(1, calls) >= FAILING:
        top = max(s["errClasses"].items(), key=lambda kv: kv[1])[0] if s["errClasses"] else None
        why = {"auth": "its login", "permission": "its permissions", "timeout": "timeouts",
               "not_found": "missing tools or files"}.get(top or "", "its setup")
        return {"kind": "failing", "text": f"{failed:,} of {calls:,} calls failed. Check {why}."}
    if per >= HEAVY_TOKENS:
        return {"kind": "heavy", "text": f"Each session that uses it takes in about {round(per / 1000)}k tokens "
                                         "of its results. Ask for less, or turn it off when you don't need it."}
    v = (s.get("compare") or {}).get("verdict", "too few")
    if v.startswith("fewer"):
        return {"kind": "helps", "text": f"Your sessions with it had {v}. Keep using it."}
    if v.startswith("more"):
        return {"kind": "watch", "text": f"Your sessions with it had {v}. Worth a look at how it's used."}
    if not calls:
        return {"kind": "fine", "text": "Nothing to fix."}
    return {"kind": "fine", "text": f"All {calls:,} calls worked." if not failed else f"{calls - failed:,} of {calls:,} calls worked."}


def measure(store: Store, now: datetime | None = None, days: int = 30) -> dict[str, Any]:
    """Per item stats over the last ``days`` of root sessions, plus how your sessions split from automated ones."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    rows = [r for r in store.index.values()
            if r.get("role", "root") == "root" and (_parse(r.get("startedAt")) or since) > since]
    yours = [r for r in rows if int(r.get("userTurns") or 0) >= 2]
    auto = [r for r in rows if int(r.get("userTurns") or 0) == 1]  # 0: no messages read at all, neither
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    used_by: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in rows:
        sid = str(r.get("sessionId") or "")
        doc = store.get(sid) if sid else None
        if not doc:
            continue
        mine = int(r.get("userTurns") or 0) >= 2
        for e in doc.get("events", []):
            key = item_of(e)
            if key is None:
                continue
            s = stats.setdefault(key, {"calls": 0, "failed": 0, "bytes": 0, "ms": 0, "sessions": 0, "yours": 0,
                                       "errClasses": defaultdict(int), "recent": {}, "last": ""})
            s["calls"] += 1
            if e.get("err"):
                s["failed"] += 1
                s["errClasses"][str(e.get("errClass") or "other")] += 1
            s["bytes"] += int(e.get("outBytes") or 0)
            s["ms"] += int(e.get("dur") or 0)
            if sid not in used_by[key]:
                used_by[key].add(sid)
                s["sessions"] += 1
                s["last"] = max(s["last"], str(r.get("startedAt") or ""))
                if mine:
                    s["yours"] += 1
                    s["recent"][sid] = {"sessionId": sid, "startedAt": r.get("startedAt"),
                                        "repo": (r.get("repos") or [None])[0], "seq": e.get("seq"),
                                        "calls": 0, "failed": 0}
            if mine and sid in s["recent"]:
                s["recent"][sid]["calls"] += 1
                s["recent"][sid]["failed"] += 1 if e.get("err") else 0
    per = {str(r.get("sessionId")): _session_stats(r) for r in yours}
    out = []
    for (kind, name), s in stats.items():
        s["tokens"] = s.pop("bytes") // BYTES_PER_TOKEN
        mine = used_by[(kind, name)] & per.keys()
        s["compare"] = compare([per[k] for k in mine], [v for k, v in per.items() if k not in mine])
        s["errClasses"] = dict(s["errClasses"])
        s["recent"] = sorted(s["recent"].values(), key=lambda x: str(x["startedAt"] or ""), reverse=True)[:RECENT]
        s["advice"] = advice(s)
        out.append({"kind": kind, "name": name, **s})
    out.sort(key=lambda i: -i["calls"])
    cost = lambda rs: round(sum(float(r.get("costUsd") or 0) for r in rs), 2)  # noqa: E731
    return {"days": days, "items": out,
            "sessions": {"yours": len(yours), "automated": len(auto), "yoursCostUsd": cost(yours),
                         "automatedCostUsd": cost(auto)}}
