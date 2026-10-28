"""Token and cost usage: today, 7 and 30 days, by model, agent and repo, and the current Claude 5-hour window.

Every model request is counted once, from two sources:

* Claude Code: its raw transcripts, main and subagent files, deduplicated on message id and request id the
  way ccusage counts. Maps can lag a live transcript or skip one that grew past the build limit, so the
  totals don't depend on them. Each file is read incrementally from where the last snapshot stopped, one
  line at a time, so a transcript of any size costs the same memory.
* Every other agent: the ``llm`` events maps already hold (``in``, ``cw``, ``cr``, ``out``, ``cost``,
  ``model``). A map request that repeats one already counted (same model and token counts within five
  seconds) is dropped: Claude Code copies earlier messages into a resumed session's transcript, and a
  parent's map repeats its subagents' requests.

Costs are estimates at list prices; a request whose model has no price is counted as unpriced, never as $0.
Nothing beyond counts, model names and repo folder names leaves this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .store import Store, _atomic_write

CACHE_VERSION = 3
RAW_CACHE_VERSION = 9  # 9: failed tool calls, for whether a skill or fix goes with fewer of them
# was 8  # 8: setup uses (skills, subagents, slash commands, MCP servers by name)
# was 7  # 7: steering signals carry their kind (a correction that says it still isn't working; swearing)
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
def _claude_line(line: bytes) -> list[Any] | None:
    """``[key, sec, model, in, cw, cr, out, repo]`` for an assistant line that carries usage, else None."""
    if b'"usage"' not in line or b'"assistant"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    m = d.get("message")
    if d.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
        return None
    model = str(m.get("model") or "unknown")
    if model == "<synthetic>":
        return None  # Claude Code's own placeholder replies, not model requests
    at = _parse(d.get("timestamp"))
    if at is None:
        return None
    u = m["usage"]
    key = f"{m.get('id') or ''}|{d.get('requestId') or ''}"
    if key == "|":
        key = f"{d.get('uuid') or ''}"  # no ids at all: the line itself is the unit
    cwd = d.get("cwd")
    repo = Path(cwd).name if isinstance(cwd, str) and cwd else None
    return [key, round(at.timestamp()), model, _int(u.get("input_tokens")),
            _int(u.get("cache_creation_input_tokens")), _int(u.get("cache_read_input_tokens")),
            _int(u.get("output_tokens")), repo]


# A reply that opens by turning the agent around: the signal behind "Corrections".
_CORRECTION = re.compile(
    r"^\W*(no\b|nope\b|wrong\b|wait\b|stop\b|don'?t\b|do not\b|undo\b|revert\b|actually\b|instead\b"
    r"|that'?s not\b|that is not\b|not what\b|why did you\b|you didn'?t\b|you broke\b|you missed\b)",
    re.IGNORECASE)
# The same, said later in the message: "why is it still not showing", "this doesn't work".
_CORRECTION_ANYWHERE = re.compile(
    r"\b(still (not|broken|wrong|the same|failing)|(doesn'?t|does not|didn'?t|did not|isn'?t|is not|not) "
    r"(work|working|showing|show|fixed|right)|why (is|isn'?t|it'?s|does|doesn'?t|did|didn'?t|are|aren'?t)\b)",
    re.IGNORECASE)
# Frustration, Blume's third rate: swearing, or a pile of ! or ?. Counted, never kept.
_FRUSTRATION = re.compile(r"\b(fuck\w*|wtf|shit\w*|damn\w*|ffs|goddamn|crap|stupid|useless|retard\w*)\b|[!?]{3,}",
                          re.IGNORECASE)


def _user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if texts and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return "\n".join(t for t in texts if isinstance(t, str))
    return None


_USER_LINE = re.compile(rb'"type"\s*:\s*"user"')


def _signal_line(line: bytes, seen_user: bool) -> tuple[list[list[Any]], bool]:
    """``[sec, kind, key]`` steering signals in one line (``key`` dedupes the copies a resumed session makes):
    ``msg`` (a message from the user), ``cor`` (one that corrects), ``int`` (an interruption), ``ref`` (a
    refused tool call), ``call`` (a tool call). Read to classify, then dropped: no text is kept. ``seen_user``
    carries over between lines and reads, so a session's first message is never counted as a correction."""
    out: list[list[Any]] = []
    if not _USER_LINE.search(line) and b'"tool_use"' not in line:
        return out, seen_user
    try:
        d = json.loads(line)
    except ValueError:
        return out, seen_user
    at = _parse(d.get("timestamp")) if isinstance(d, dict) else None
    if at is None or d.get("isSidechain"):
        return out, seen_user
    sec = round(at.timestamp())
    uid = str(d.get("uuid") or "")
    m = d.get("message") if isinstance(d.get("message"), dict) else {}
    content = m.get("content")
    if d.get("type") == "assistant":
        if isinstance(content, list):
            out += [[sec, "call", str(b.get("id") or uid)] for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"]
        return out, seen_user
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
        return out, seen_user
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                body = b.get("content")
                body = body if isinstance(body, str) else json.dumps(body)[:400]
                if "doesn't want to proceed" in body or "user rejected" in body.lower():
                    out.append([sec, "ref", str(b.get("tool_use_id") or uid)])
    text = _user_text(content)
    if text is None:
        return out, seen_user
    t = text.lstrip()
    if t.startswith("[Request interrupted"):
        out.append([sec, "int", uid])
        return out, seen_user
    if not t or t.startswith("<") or "[SYSTEM NOTIFICATION" in t:
        return out, seen_user  # harness turns, command output, notifications: not the user talking
    # A program driving the Agent SDK (a pipeline, a test harness) sends prompts too. Editor panels mark
    # what the user typed with origin.kind "human"; terminal sessions are typed unless marked otherwise.
    origin = d.get("origin") if isinstance(d.get("origin"), dict) else {}
    sdk = str(d.get("entrypoint") or "").startswith("sdk")
    if origin.get("kind", "human" if not sdk else None) != "human":
        return out, seen_user
    out.append([sec, "msg", uid])
    if seen_user:
        if _CORRECTION.match(t):
            out.append([sec, "cor", uid])
        elif _CORRECTION_ANYWHERE.search(t[:160]):
            # Said later in the message ("still not working", "why is it..."): a result that didn't hold,
            # rather than a change of direction. Kept apart because the two call for different fixes.
            out.append([sec, "cor", uid])
            out.append([sec, "cornw", uid])
    if (fr := _FRUSTRATION.search(t)) is not None:
        out.append([sec, "fru", uid])
        if fr.group(1):
            out.append([sec, "fruw", uid])  # a word, not a pile of ! or ?
    return out, True


def signals(entries: list[list[Any]], now: datetime, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Corrections and interruptions per message from you, refusals per 100 tool calls: the last
    ``days`` against the ``days`` before, and per day for the trend."""
    unique: dict[tuple[str, str], int] = {}
    for e in entries:
        sec, kind = e[0], e[1]
        key = (kind, e[2] if len(e) > 2 and e[2] else f"{sec}")
        unique.setdefault(key, sec)  # a resumed session's copy of a line counts once

    def count(lo: float, hi: float) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for (kind, _), sec in unique.items():
            if lo <= sec < hi:
                c[kind] += 1
        return c

    def rates(c: dict[str, int]) -> dict[str, Any]:
        msgs, calls = c.get("msg", 0), c.get("call", 0)
        return {"messages": msgs, "calls": calls, "corrections": c.get("cor", 0), "interrupts": c.get("int", 0),
                "refusals": c.get("ref", 0), "frustrated": c.get("fru", 0),
                # Of the corrections, those that say the result didn't hold; of the frustration, the swearing.
                "notWorking": c.get("cornw", 0), "swearing": c.get("fruw", 0),
                "correctionRate": round(100 * c.get("cor", 0) / msgs, 1) if msgs else None,
                "frustrationRate": round(100 * c.get("fru", 0) / msgs, 1) if msgs else None,
                "interruptRate": round(100 * c.get("int", 0) / msgs, 1) if msgs else None,
                "refusalRate": round(100 * c.get("ref", 0) / calls, 2) if calls else None}

    end = now.timestamp() + 60
    start = end - days * 86400
    local_midnight = datetime.fromtimestamp(now.timestamp()).replace(hour=0, minute=0, second=0, microsecond=0)
    daily = []
    for d in range(days - 1, -1, -1):
        s = (local_midnight - timedelta(days=d)).timestamp()
        r = rates(count(s, s + 86400))
        daily.append({"date": datetime.fromtimestamp(s).strftime("%Y-%m-%d"), "messages": r["messages"],
                      "correctionRate": r["correctionRate"], "interruptRate": r["interruptRate"],
                      "frustrationRate": r["frustrationRate"],
                      "refusalRate": r["refusalRate"]})
    return {"days": days, "current": rates(count(start, end)), "previous": rates(count(start - days * 86400, start)),
            "daily": daily}


def _session_of(path: Path) -> str:
    parts = path.parts
    if "subagents" in parts:
        return f"local:claude-code:{parts[parts.index('subagents') - 1]}"
    return f"local:claude-code:{path.stem}"


def _file_key(path: Path) -> str:
    """The cache key for a transcript: a digest of its path, so the cache on disk holds no path."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]


_SETUP_NAME = re.compile(r"^[A-Za-z0-9_.:@-]{1,80}$")
_COMMAND = re.compile(rb"<command-name>/?([A-Za-z0-9_.:@-]{1,80})</command-name>")


def _setup_line(line: bytes) -> list[list[Any]]:
    """``[sec, kind, name]`` for each use of a piece of setup in one line: a Skill call (``skill``), an Agent or
    Task call (``agent``, its subagent type), an MCP tool (``mcp``, the server), a slash command (``command``).
    Names only; setup_inventory keeps just the ones that match something installed."""
    if b'"tool_use"' not in line and b"<command-name>" not in line:
        return []
    try:
        d = json.loads(line)
    except ValueError:
        return []
    at = _parse(d.get("timestamp")) if isinstance(d, dict) else None
    if at is None:
        return []
    sec = round(at.timestamp())
    out: list[list[Any]] = []
    m = d.get("message") if isinstance(d.get("message"), dict) else {}
    content = m.get("content")
    if d.get("type") == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, inp = str(b.get("name") or ""), b.get("input") if isinstance(b.get("input"), dict) else {}
            if name == "Skill":
                out.append([sec, "skill", str(inp.get("skill") or inp.get("command") or "")])
            elif name in ("Agent", "Task"):
                out.append([sec, "agent", str(inp.get("subagent_type") or "general-purpose")])
            elif name.startswith("mcp__"):
                out.append([sec, "mcp", name.split("__")[1] if name.count("__") >= 2 else ""])
    elif d.get("type") == "user":
        hit = _COMMAND.search(line)
        if hit:
            out.append([sec, "command", hit.group(1).decode()])
    return [u for u in out if _SETUP_NAME.match(u[2])]


def claude_requests(homes: list[Path], store: Store, now: datetime, days: int = WINDOW_DAYS,
                    signals_out: list[list[Any]] | None = None,
                    uses_out: list[list[Any]] | None = None) -> list[Request]:
    """Every Claude Code request in the last ``days``, each once, from the raw transcripts.

    ``signals_out``: if given, extended with the steering signals of the last ``2 × days`` (``_signal_lines``),
    from the user's own sessions only (subagent transcripts are prompts the agent wrote)."""
    since = (now - timedelta(days=days + 1)).timestamp()
    sig_since = (now - timedelta(days=2 * days + 1)).timestamp()
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
            if st.st_mtime < sig_since:
                continue
            key = _file_key(f)
            hit = cached.get(key)
            start = 0
            reqs: list[list[Any]] = []
            sigs: list[list[Any]] = []
            uses: list[list[Any]] = []
            seen_user = False
            if hit and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime:
                files[key] = hit
                continue
            if hit and st.st_size >= hit.get("offset", 0):
                start, reqs = hit.get("offset", 0), list(hit.get("reqs", []))  # appended since: read the tail
                sigs, seen_user = list(hit.get("sig", [])), bool(hit.get("seenUser"))
                uses = list(hit.get("use", []))
            root = "subagents" not in f.parts  # a subagent's prompts are the agent's, not the user's
            end = start
            try:
                with open(f, "rb") as fh:
                    fh.seek(start)
                    for line in fh:  # one line at a time: the file is never held whole
                        if not line.endswith(b"\n"):
                            break  # a line still being written waits for the next look
                        end += len(line)
                        r = _claude_line(line)
                        if r is not None:
                            reqs.append(r)
                        uses += _setup_line(line)
                        if root:
                            new_sigs, seen_user = _signal_line(line, seen_user)
                            sigs += new_sigs
            except OSError:
                continue
            files[key] = {"session": _session_of(f), "size": st.st_size, "mtime": st.st_mtime, "offset": end,
                          "reqs": [r for r in reqs if r[1] >= since],
                          "sig": [s for s in sigs if s[0] >= sig_since], "seenUser": seen_user,
                          "use": [u for u in uses if u[0] >= since]}
    if files != cached:
        try:
            body = json.dumps({"version": RAW_CACHE_VERSION, "files": files}, separators=(",", ":"))
            _atomic_write(cache_path, body.encode("utf-8"))
        except OSError:
            pass  # a cache that can't be written only costs speed
    # A response is written as several lines that share its ids, and output_tokens grows as it streams:
    # the line with the most output is the finished request, and the first line is when it started.
    best: dict[str, tuple[list[Any], str]] = {}
    # In session order, so a request copied into a resumed session is credited the same way every time.
    for hit in sorted(files.values(), key=lambda h: str(h.get("session") or "")):
        session = str(hit.get("session") or "")
        for r in hit.get("reqs", []):
            have = best.get(r[0])
            if have is None:
                best[r[0]] = (list(r), session)
                continue
            keep = have[0]
            keep[1] = min(keep[1], r[1])
            if r[6] > keep[6]:
                keep[2:7] = r[2:7]
    if signals_out is not None:
        for hit in files.values():
            signals_out += hit.get("sig", [])
    if uses_out is not None:
        for hit in files.values():
            uses_out += hit.get("use", [])
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
    """``first`` in full, then each of ``rest`` that isn't a repeat of a request already kept.

    ``first`` is already each request once, by id, so two of its requests that happen to match in model and
    token counts within five seconds are both real and both kept. Only maps are deduplicated this way."""
    seen: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for r in first:
        seen[r[1:6]].append(r[0])
    out: list[Request] = list(first)
    for r in rest:
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


def _setup(claude_homes: list[Path] | None, uses: list[list[Any]], now: datetime, store: Store) -> dict[str, Any] | None:
    """The person's Claude Code setup, how much of it is used, and how each piece performs (impact)."""
    if not claude_homes:
        return None
    from . import impact, setup_inventory

    home = Path(claude_homes[0])
    try:
        from .coach import descriptions

        rep = setup_inventory.report(home, uses, now, claude_json=home.parent / ".claude.json",
                                     measured=impact.measure(store, now))
        rep["tips"] = impact.tips(rep, descriptions(home))
        return rep
    except Exception:  # noqa: BLE001 - the setup list must never cost the usage report
        return None


def report(store: Store, now: datetime | None = None, claude_homes: list[Path] | None = None) -> dict[str, Any]:
    """``claude_homes``: Claude Code homes to read raw transcripts from. None reads none (maps only)."""
    t_start = time.monotonic()
    now = now or datetime.now(UTC)
    sig: list[list[Any]] = []
    uses: list[list[Any]] = []
    raw = claude_requests(claude_homes, store, now, signals_out=sig, uses_out=uses) if claude_homes else []
    reqs = merge(raw, map_requests(store, now))
    local_midnight = datetime.fromtimestamp(now.timestamp()).replace(hour=0, minute=0, second=0, microsecond=0)
    today = local_midnight.astimezone(UTC)
    daily = []
    for d in range(13, -1, -1):
        s = today - timedelta(days=d)
        p = _period(reqs, s, s + timedelta(days=1))
        daily.append({"date": datetime.fromtimestamp(s.timestamp()).strftime("%Y-%m-%d"),
                      "tokens": p["tokens"]["total"], "costUsd": p["costUsd"], "requests": p["requests"]})
    # Claude Code alone, four weeks: what its plan forecast learns your weekdays from. Cost, not tokens: it
    # weighs models and cache reads roughly the way plan limits do.
    claude = [r for r in reqs if r[7] == "claude-code"]
    claude_daily = []
    for d in range(27, -1, -1):
        s = today - timedelta(days=d)
        claude_daily.append({"date": datetime.fromtimestamp(s.timestamp()).strftime("%Y-%m-%d"),
                             "costUsd": _period(claude, s, s + timedelta(days=1))["costUsd"]})
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
        "claudeDaily": claude_daily,
        "claudeBlock": claude_block(reqs, now),
        "signals": signals(sig, now) if claude_homes else None,
        "setup": _setup(claude_homes, uses, now, store),
        "timingMs": round(1000 * (time.monotonic() - t_start)),
    }


