"""Token and cost usage: today, 7 and 30 days, by model, agent and repo, and the current Claude 5-hour window.

Every model request is counted once, from two sources:

* Claude Code: its raw transcripts, main and subagent files, deduplicated on message id and request id the
  way ccusage counts. Maps can lag a live transcript or skip one that grew past the build limit, so the
  totals don't depend on them. Each file is read incrementally from where the last snapshot stopped.
* Every other agent: the ``llm`` events maps already hold (``in``, ``cw``, ``cr``, ``out``, ``cost``,
  ``model``). A map request that repeats one already counted (same model and token counts within five
  seconds) is dropped: Claude Code copies earlier messages into a resumed session's transcript, and a
  parent's map repeats its subagents' requests.

Costs are estimates at list prices; a request whose model has no price is counted as unpriced, never as $0.
Nothing beyond counts, model names and repo folder names leaves this module.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .store import Store, _atomic_write

CACHE_VERSION = 3
RAW_CACHE_VERSION = 1
BLOCK_HOURS = 5
WINDOW_DAYS = 30
DUP_SECONDS = 5

# One request: (epoch seconds, model, in, cw, cr, out, cost or None, agent, repo or None, session id)
Request = tuple[Any, ...]


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


def _price(model: str, i: int, cw: int, cr: int, o: int) -> float | None:
    from .builder import estimate_cost_usd

    return estimate_cost_usd(model, i, cw, cr, o)


def _int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


# --------------------------------------------------------------------------- Claude Code, raw transcripts
def _claude_lines(data: bytes) -> list[list[Any]]:
    """``[key, sec, model, in, cw, cr, out, repo]`` for each assistant line that carries usage."""
    out = []
    for line in data.split(b"\n"):
        if b'"usage"' not in line or b'"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message")
        if d.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
            continue
        model = str(m.get("model") or "unknown")
        if model == "<synthetic>":
            continue  # Claude Code's own placeholder replies, not model requests
        at = _parse(d.get("timestamp"))
        if at is None:
            continue
        u = m["usage"]
        key = f"{m.get('id') or ''}|{d.get('requestId') or ''}"
        if key == "|":
            key = f"{d.get('uuid') or ''}"  # no ids at all: the line itself is the unit
        cwd = d.get("cwd")
        repo = Path(cwd).name if isinstance(cwd, str) and cwd else None
        out.append([key, round(at.timestamp()), model, _int(u.get("input_tokens")),
                    _int(u.get("cache_creation_input_tokens")), _int(u.get("cache_read_input_tokens")),
                    _int(u.get("output_tokens")), repo])
    return out


def _session_of(path: Path) -> str:
    parts = path.parts
    if "subagents" in parts:
        return f"local:claude-code:{parts[parts.index('subagents') - 1]}"
    return f"local:claude-code:{path.stem}"


def claude_requests(homes: list[Path], store: Store, now: datetime, days: int = WINDOW_DAYS) -> list[Request]:
    """Every Claude Code request in the last ``days``, each once, from the raw transcripts."""
    since = (now - timedelta(days=days + 1)).timestamp()
    cache_path = store.base / "cache" / "companion-claude-usage.json"
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        cached = raw.get("files", {}) if raw.get("version") == RAW_CACHE_VERSION else {}
    except (OSError, ValueError, AttributeError):
        cached = {}
    files: dict[str, Any] = {}
    for home in homes:
        root = Path(home) / "projects"
        if not root.is_dir():
            continue
        for f in root.rglob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime < since:
                continue
            p = str(f)
            hit = cached.get(p)
            start = 0
            reqs: list[list[Any]] = []
            if hit and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime:
                files[p] = hit
                continue
            if hit and st.st_size >= hit.get("offset", 0):
                start, reqs = hit.get("offset", 0), list(hit.get("reqs", []))  # appended since: read the tail
            try:
                with open(f, "rb") as fh:
                    fh.seek(start)
                    data = fh.read()
            except OSError:
                continue
            end = data.rfind(b"\n") + 1  # a line still being written waits for the next look
            reqs += _claude_lines(data[:end])
            files[p] = {"size": st.st_size, "mtime": st.st_mtime, "offset": start + end,
                        "reqs": [r for r in reqs if r[1] >= since]}
    if files != cached:
        try:
            body = json.dumps({"version": RAW_CACHE_VERSION, "files": files}, separators=(",", ":"))
            _atomic_write(cache_path, body.encode("utf-8"))
        except OSError:
            pass  # a cache that can't be written only costs speed
    # A response is written as several lines that share its ids, and output_tokens grows as it streams:
    # the line with the most output is the finished request, and the first line is when it started.
    best: dict[str, tuple[list[Any], str]] = {}
    for p in sorted(files):
        session = _session_of(Path(p))
        for r in files[p]["reqs"]:
            have = best.get(r[0])
            if have is None:
                best[r[0]] = (list(r), session)
                continue
            keep = have[0]
            keep[1] = min(keep[1], r[1])
            if r[6] > keep[6]:
                keep[2:7] = r[2:7]
    out: list[Request] = []
    for (_key, sec, model, i, cw, cr, o, repo), session in best.values():
        out.append((sec, model, i, cw, cr, o, _price(model, i, cw, cr, o), "claude-code", repo, session))
    out.sort(key=lambda r: r[0])
    return out


# --------------------------------------------------------------------------- every agent, from maps
def session_requests(doc: dict[str, Any]) -> list[list[Any]]:
    """``[epoch seconds, model, in, cw, cr, out, cost or None]`` per model request in one map, every lane."""
    t0 = _parse((doc.get("session") or {}).get("startedAt"))
    if t0 is None:
        return []
    out = []
    for e in doc.get("events") or []:
        if e.get("kind") != "llm":
            continue
        at = t0 + timedelta(milliseconds=int(e.get("t") or 0))
        cost = e.get("cost")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        out.append([round(at.timestamp()), str(e.get("model") or "unknown"),
                    _int(e.get("in")), _int(e.get("cw")), _int(e.get("cr")), _int(e.get("out")), cost])
    return out


def _load_cache(store: Store) -> dict[str, Any]:
    try:
        raw = json.loads((store.base / "cache" / "companion-usage.json").read_text(encoding="utf-8"))
        return raw.get("sessions", {}) if raw.get("version") == CACHE_VERSION else {}
    except (OSError, ValueError, AttributeError):
        return {}


def map_requests(store: Store, now: datetime, days: int = WINDOW_DAYS) -> list[Request]:
    """Requests from every map active in the last ``days``, root sessions first."""
    since = now - timedelta(days=days + 1)
    cache = _load_cache(store)
    fresh: dict[str, Any] = {}
    changed = False
    for row in store.rows():
        if not row.get("requests"):
            continue
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
                   "role": row.get("role"), "requests": session_requests(doc)}
            changed = True
        fresh[sid] = hit
    if changed or set(fresh) != set(cache):
        try:
            body = json.dumps({"version": CACHE_VERSION, "sessions": fresh}, separators=(",", ":"))
            _atomic_write(store.base / "cache" / "companion-usage.json", body.encode("utf-8"))
        except OSError:
            pass
    out: list[Request] = []
    for sid, s in sorted(fresh.items(), key=lambda kv: (kv[1].get("role") in ("subagent", "helper"), kv[0])):
        agent = s.get("agent") or "unknown"
        for sec, model, i, cw, cr, o, cost in s.get("requests") or []:
            out.append((sec, model, i, cw, cr, o, cost, agent, s.get("repo"), sid))
    return out


def merge(first: list[Request], rest: list[Request]) -> list[Request]:
    """``first`` in full, then each of ``rest`` that isn't a repeat of a request already kept."""
    seen: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    out: list[Request] = []
    for r in [*first, *rest]:
        key = r[1:6]
        if any(abs(r[0] - t) <= DUP_SECONDS for t in seen[key]):
            continue
        seen[key].append(r[0])
        out.append(r)
    return out


# --------------------------------------------------------------------------- periods
def _empty() -> dict[str, Any]:
    return {"tokens": {"input": 0, "cacheWrite": 0, "cacheRead": 0, "output": 0, "total": 0},
            "costUsd": 0.0, "requests": 0, "unpricedRequests": 0, "sessions": 0}


def _add(acc: dict[str, Any], r: Request) -> None:
    _, _, i, cw, cr, o, cost, *_ = r
    t = acc["tokens"]
    t["input"] += i
    t["cacheWrite"] += cw
    t["cacheRead"] += cr
    t["output"] += o
    t["total"] += i + cw + cr + o
    acc["requests"] += 1
    if cost is None:
        acc["unpricedRequests"] += 1
    else:
        acc["costUsd"] += cost


def _period(reqs: list[Request], start: datetime, end: datetime) -> dict[str, Any]:
    a, b = start.timestamp(), end.timestamp()
    total = _empty()
    sessions: set[str] = set()
    by: dict[str, dict[str, dict[str, Any]]] = {"model": {}, "agent": {}, "repo": {}}
    for r in reqs:
        if not (a <= r[0] < b):
            continue
        _add(total, r)
        sessions.add(r[9])
        for dim, key in (("model", r[1]), ("agent", r[7]), ("repo", r[8] or "outside a repo")):
            acc = by[dim].setdefault(key, _empty())
            _add(acc, r)
            acc.setdefault("_s", set()).add(r[9])
    total["sessions"] = len(sessions)
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


def claude_block(reqs: list[Request], now: datetime) -> dict[str, Any] | None:
    """The current 5-hour window as ccusage reckons it: starts at the hour of the first request after the
    previous window ended, lasts five hours. An estimate from this Mac only."""
    claude = [r for r in reqs if r[7] == "claude-code"]
    start = None
    for sec in sorted(r[0] for r in claude):
        at = datetime.fromtimestamp(sec, UTC)
        if start is None or at >= start + timedelta(hours=BLOCK_HOURS):
            start = at.replace(minute=0, second=0, microsecond=0)
    if start is None or now >= start + timedelta(hours=BLOCK_HOURS):
        return None
    end = start + timedelta(hours=BLOCK_HOURS)
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


def report(store: Store, now: datetime | None = None, claude_homes: list[Path] | None = None) -> dict[str, Any]:
    """``claude_homes``: Claude Code homes to read raw transcripts from. None reads none (maps only)."""
    t_start = time.monotonic()
    now = now or datetime.now(UTC)
    raw = claude_requests(claude_homes, store, now) if claude_homes else []
    reqs = merge(raw, map_requests(store, now))
    local_midnight = datetime.fromtimestamp(now.timestamp()).replace(hour=0, minute=0, second=0, microsecond=0)
    today = local_midnight.astimezone(UTC)
    daily = []
    for d in range(13, -1, -1):
        s = today - timedelta(days=d)
        p = _period(reqs, s, s + timedelta(days=1))
        daily.append({"date": datetime.fromtimestamp(s.timestamp()).strftime("%Y-%m-%d"),
                      "tokens": p["tokens"]["total"], "costUsd": p["costUsd"], "requests": p["requests"]})
    source = "Claude Code's own transcripts and your local AgentPlay maps" if raw else "your local AgentPlay maps"
    return {
        "generatedAt": _iso(now),
        "kind": "estimated",
        "explanation": (f"Counted from {source}, subagents included and each request once. Costs are "
                        "estimates at list prices; requests on unpriced models are counted separately, not as "
                        "$0. Usage on other devices, the web or the API isn't here."),
        "today": _period(reqs, today, now + timedelta(minutes=1)),
        "last7Days": _period(reqs, now - timedelta(days=7), now + timedelta(minutes=1)),
        "last30Days": _period(reqs, now - timedelta(days=30), now + timedelta(minutes=1)),
        "daily": daily,
        "claudeBlock": claude_block(reqs, now),
        "timingMs": round(1000 * (time.monotonic() - t_start)),
    }


