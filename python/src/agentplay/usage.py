"""Token and cost usage from the local maps: today, 7 and 30 days, by model, agent and repo, and the
current Claude 5-hour window.

Read from the ``llm`` events maps already hold (``in``, ``cw``, ``cr``, ``out``, ``cost``, ``model``), so
nothing new is parsed and nothing beyond counts leaves this module. Costs are estimates at list prices
(``_core/pricing.py``); a request whose model has no price is counted as unpriced, never as $0.

Per-session hourly buckets are cached in ``<store>/cache/companion-usage.json`` keyed by content hash,
so a snapshot loads only maps that changed.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import Store, _atomic_write

CACHE_VERSION = 1
BLOCK_HOURS = 5
WINDOW_DAYS = 30

# [in, cw, cr, out, cost, requests, unpriced]
_IN, _CW, _CR, _OUT, _COST, _REQ, _UNPRICED = range(7)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_buckets(doc: dict[str, Any]) -> dict[str, dict[str, list[float]]]:
    """``{hour (UTC ISO): {model: [in, cw, cr, out, cost, requests, unpriced]}}`` for one map, every lane."""
    t0 = _parse((doc.get("session") or {}).get("startedAt"))
    if t0 is None:
        return {}
    out: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0] * 7))
    for e in doc.get("events") or []:
        if e.get("kind") != "llm":
            continue
        at = t0 + timedelta(milliseconds=int(e.get("t") or 0))
        hour = _iso(at.replace(minute=0, second=0, microsecond=0))
        b = out[hour][str(e.get("model") or "unknown")]
        for i, k in ((_IN, "in"), (_CW, "cw"), (_CR, "cr"), (_OUT, "out")):
            v = e.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                b[i] += v
        b[_REQ] += 1
        cost = e.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            b[_COST] += cost
        else:
            b[_UNPRICED] += 1
    return {h: {m: v for m, v in ms.items()} for h, ms in out.items()}


def _load_cache(store: Store) -> dict[str, Any]:
    try:
        raw = json.loads((store.base / "cache" / "companion-usage.json").read_text(encoding="utf-8"))
        return raw.get("sessions", {}) if raw.get("version") == CACHE_VERSION else {}
    except (OSError, ValueError, AttributeError):
        return {}


def collect(store: Store, now: datetime, days: int = WINDOW_DAYS) -> list[dict[str, Any]]:
    """Cached per-session buckets for root sessions active in the last ``days``."""
    since = now - timedelta(days=days + 1)
    cache = _load_cache(store)
    fresh: dict[str, Any] = {}
    changed = False
    for row in store.rows():
        if row.get("role") in ("subagent", "helper") or not row.get("requests"):
            continue  # subagent work is already inside its parent's map
        end = _parse(row.get("endedAt")) or _parse(row.get("startedAt"))
        if end is None or end < since:
            continue
        sid, h = row["sessionId"], row.get("contentHash")
        hit = cache.get(sid)
        if not hit or hit.get("hash") != h:
            doc = store.get(sid)
            if doc is None:
                continue
            hit = {"hash": h, "agent": row.get("agentTool"), "repo": (row.get("repos") or [None])[0],
                   "buckets": session_buckets(doc)}
            changed = True
        fresh[sid] = hit
    if changed or set(fresh) != set(cache):
        try:
            body = json.dumps({"version": CACHE_VERSION, "sessions": fresh}, separators=(",", ":"))
            _atomic_write(store.base / "cache" / "companion-usage.json", body.encode("utf-8"))
        except OSError:
            pass  # a cache that can't be written only costs speed
    return [{"sessionId": sid, **v} for sid, v in fresh.items()]


def _empty() -> dict[str, Any]:
    return {"tokens": {"input": 0, "cacheWrite": 0, "cacheRead": 0, "output": 0, "total": 0},
            "costUsd": 0.0, "requests": 0, "unpricedRequests": 0, "sessions": 0}


def _add(acc: dict[str, Any], v: list[float]) -> None:
    t = acc["tokens"]
    t["input"] += int(v[_IN])
    t["cacheWrite"] += int(v[_CW])
    t["cacheRead"] += int(v[_CR])
    t["output"] += int(v[_OUT])
    t["total"] += int(v[_IN] + v[_CW] + v[_CR] + v[_OUT])
    acc["costUsd"] += v[_COST]
    acc["requests"] += int(v[_REQ])
    acc["unpricedRequests"] += int(v[_UNPRICED])


def _period(sessions: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    total = _empty()
    by: dict[str, dict[str, dict[str, Any]]] = {"model": {}, "agent": {}, "repo": {}}
    seen: dict[str, set[str]] = {"total": set(), "model": set(), "agent": set(), "repo": set()}
    for s in sessions:
        for hour, models in s["buckets"].items():
            at = _parse(hour)
            if at is None or not (start <= at < end):
                continue
            for model, v in models.items():
                _add(total, v)
                seen["total"].add(s["sessionId"])
                for dim, key in (("model", model), ("agent", s.get("agent") or "unknown"),
                                 ("repo", s.get("repo") or "outside a repo")):
                    acc = by[dim].setdefault(key, _empty())
                    _add(acc, v)
                    acc.setdefault("_s", set()).add(s["sessionId"])
    total["sessions"] = len(seen["total"])
    total["costUsd"] = round(total["costUsd"], 4)
    out = {**total}
    for dim, items in by.items():
        rows = []
        for key, acc in items.items():
            acc["sessions"] = len(acc.pop("_s", set()))
            acc["costUsd"] = round(acc["costUsd"], 4)
            rows.append({"key": key, **acc})
        rows.sort(key=lambda r: (r["costUsd"], r["tokens"]["total"]), reverse=True)
        out[f"by{dim.capitalize()}"] = rows[:8]
    return out


def claude_block(sessions: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The current 5-hour window as ccusage reckons it: starts at the hour of the first request after the
    previous window ended, lasts five hours. An estimate from this Mac only."""
    hours = sorted({h for s in sessions if s.get("agent") == "claude-code" for h in s["buckets"]})
    start = None
    for h in hours:
        at = _parse(h)
        if at is None:
            continue
        if start is None or at >= start + timedelta(hours=BLOCK_HOURS):
            start = at
    if start is None or now >= start + timedelta(hours=BLOCK_HOURS):
        return None
    end = start + timedelta(hours=BLOCK_HOURS)
    claude = [s for s in sessions if s.get("agent") == "claude-code"]
    p = _period(claude, start, end)
    minutes = max(1.0, (now - start).total_seconds() / 60)
    return {
        "start": _iso(start), "end": _iso(end),
        "tokens": p["tokens"], "costUsd": p["costUsd"], "requests": p["requests"],
        "unpricedRequests": p["unpricedRequests"], "sessions": p["sessions"], "byModel": p["byModel"],
        "burnTokensPerMinute": round(p["tokens"]["total"] / minutes),
        "burnCostPerHour": round(p["costUsd"] / minutes * 60, 4),
        "projectedCostUsd": round(p["costUsd"] / minutes * BLOCK_HOURS * 60, 4),
    }


def report(store: Store, now: datetime | None = None) -> dict[str, Any]:
    t_start = time.monotonic()
    now = now or datetime.now(UTC)
    sessions = collect(store, now)
    local_midnight = datetime.fromtimestamp(now.timestamp()).replace(hour=0, minute=0, second=0, microsecond=0)
    today = local_midnight.astimezone(UTC)
    daily = []
    for d in range(13, -1, -1):
        s = today - timedelta(days=d)
        p = _period(sessions, s, s + timedelta(days=1))
        daily.append({"date": datetime.fromtimestamp(s.timestamp()).strftime("%Y-%m-%d"),
                      "tokens": p["tokens"]["total"], "costUsd": p["costUsd"], "requests": p["requests"]})
    return {
        "generatedAt": _iso(now),
        "kind": "estimated",
        "explanation": ("Counted from the model requests in your local AgentPlay maps. Costs are estimates at list "
                        "prices; requests on unpriced models are counted separately, not as $0. Usage on other "
                        "devices, the web or the API isn't here."),
        "today": _period(sessions, today, now + timedelta(minutes=1)),
        "last7Days": _period(sessions, now - timedelta(days=7), now + timedelta(minutes=1)),
        "last30Days": _period(sessions, now - timedelta(days=30), now + timedelta(minutes=1)),
        "daily": daily,
        "claudeBlock": claude_block(sessions, now),
        "timingMs": round(1000 * (time.monotonic() - t_start)),
    }
