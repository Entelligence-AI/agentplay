"""Session facts: the small per-session summary the Insights view aggregates.

Computed from a finished map document, so the same function serves a fresh
build, a backfill over maps already on disk (``ensure_facts``) and an imported
map. Every value comes from fields the map already holds: verbs, repo-relative
paths, counts, tokens and costs. No prompt text, command arguments or file
contents, so facts are as safe to keep as the map they came from.

Facts live on the session's index row under ``facts``. Bump ``FACTS_VERSION``
whenever what this module computes changes: rows with an older version are
recomputed from their stored map the next time stats are read, without
re-reading any transcript.
"""

from __future__ import annotations

from typing import Any

from .builder import _NON_CALL_KINDS, _error_source, estimate_cost_usd

FACTS_VERSION = 6
# Files kept per session in `files`, busiest first; the rest fold into `filesRest`.
# A huge session would otherwise add hundreds of KB to its index row and to /api/stats.
MAX_FACT_FILES = 300

# Outcomes whose whole cost counts as abandoned spend. `interrupted` is left
# out on purpose: stopping an agent is often the right call.
_ABANDONED = frozenset({"unresolved_errors", "incomplete"})
# How a subagent lane can end without giving its parent anything back.
_DEAD_LANE = frozenset({"no_result", "subagent_failed", "subagent_error", "subagent_killed"})
# Event kinds that run a shell command, keyed by verb in `cmd`.
_COMMAND_KINDS = frozenset({"exec", "verify"})


def _cache_miss_usd(model: str | None, fresh: int) -> float:
    """What ``fresh`` input tokens cost over reading them from a warm cache."""
    if not model or fresh <= 0:
        return 0.0
    full = estimate_cost_usd(model, fresh, 0, 0, 0) or 0.0
    cached = estimate_cost_usd(model, 0, 0, fresh, 0) or 0.0
    return max(0.0, full - cached)


def _waste(doc: dict[str, Any], evs: list[dict[str, Any]], outcome: str) -> dict[str, float]:
    """Spend that bought nothing that shipped, in USD, split into buckets.

    Cost exists only on ``llm`` events, so a tool-level failure is charged the
    model request that read its result. Each request is charged at most once,
    in bucket order: retry, abandoned, cache miss, re-read, dead subagent.
    Every bucket is an estimate and the viewer labels it as one.
    """
    llm = [e for e in evs if e["kind"] == "llm"]
    charged: set[int] = set()
    out = {"retry": 0.0, "abandoned": 0.0, "cacheMiss": 0.0, "reread": 0.0, "deadSubagent": 0.0}

    def next_llm(e: dict[str, Any]) -> dict[str, Any] | None:
        for x in llm:
            if x["seq"] > e["seq"] and x["agent"] == e["agent"] and x["seq"] not in charged:
                return x
        return None

    # Cache-miss estimates are only part of a request's cost, so a later bucket can still take
    # the whole request; when it does, the estimate moves out of cacheMiss rather than counting twice.
    misses: dict[int, float] = {}

    def take(x: dict[str, Any], bucket: str) -> None:
        charged.add(x["seq"])
        out["cacheMiss"] -= misses.pop(x["seq"], 0.0)
        out[bucket] += float(x.get("cost") or 0.0)

    def charge(bucket: str, e: dict[str, Any]) -> None:
        x = next_llm(e)
        if x is not None:
            take(x, bucket)

    for e in evs:
        if e["kind"] in _NON_CALL_KINDS:
            continue
        if e.get("err") or e.get("retryOf") is not None:
            charge("retry", e)

    if outcome in _ABANDONED:
        for x in llm:
            if x["seq"] not in charged:
                charged.add(x["seq"])
                out["abandoned"] += float(x.get("cost") or 0.0)

    seen_agent: set[str] = set()
    for x in llm:
        first = x["agent"] not in seen_agent
        seen_agent.add(x["agent"])
        if first or x["seq"] in charged:
            continue
        miss = _cache_miss_usd(x.get("model"), int(x.get("in") or 0))
        if miss > 0:
            misses[x["seq"]] = miss
            out["cacheMiss"] += miss

    # A re-read: a file read again with no edit to it since the last read.
    last_read: dict[int, int] = {}
    for e in evs:
        reread = False
        for t in e.get("targets") or []:
            fid = t.get("file")
            if not isinstance(fid, int):
                continue
            if t.get("touch") == "edit":
                last_read.pop(fid, None)
            elif t.get("touch") == "read":
                reread = reread or fid in last_read
                last_read[fid] = e["seq"]
        # One read event (a shell read can name several files) buys one request at most.
        if reread:
            charge("reread", e)

    launches = {e["seq"]: e for e in evs if e["kind"] == "subagent"}
    for a in doc.get("agents") or []:
        launch = launches.get(a.get("launchSeq"))
        dead = a.get("outcome") in _DEAD_LANE or (launch is not None and launch.get("errClass") in _DEAD_LANE)
        if a.get("kind") == "subagent" and dead:
            for x in [x for x in llm if x["agent"] == a.get("id") and x["seq"] not in charged]:
                take(x, "deadSubagent")

    return {k: round(max(v, 0.0), 6) for k, v in out.items()}


def _rereads_after_compaction(evs: list[dict[str, Any]]) -> int:
    """Files read again after a compaction that had been read before it, with no edit between."""
    read_before: set[int] = set()
    compacted = False
    n = 0
    for e in evs:
        if e["kind"] == "compaction" and e["agent"] == "main":
            compacted = True
            continue
        for t in e.get("targets") or []:
            fid = t.get("file")
            if not isinstance(fid, int):
                continue
            if t.get("touch") == "edit":
                read_before.discard(fid)
            elif t.get("touch") == "read":
                if compacted and fid in read_before:
                    n += 1
                    read_before.discard(fid)
                elif not compacted:
                    read_before.add(fid)
    return n


def file_counts(doc: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Every file the session touched, ``repo:path -> {hits, reads, edits}``, uncapped.

    ``compute`` stores at most ``MAX_FACT_FILES`` of these; ``/api/stats`` reads the
    full set for a capped session so the cross-session heatmap misses no path.
    """
    repo_names = {r["id"]: r["name"] for r in doc.get("repos") or [] if "id" in r and "name" in r}
    files: dict[str, dict[str, int]] = {}
    for f in doc.get("files") or []:
        hits, reads, edits = int(f.get("hits") or 0), int(f.get("reads") or 0), int(f.get("edits") or 0)
        if not (hits or reads or edits) or not f.get("path"):
            continue
        key = f"{repo_names.get(f.get('repo'), '')}:{f['path']}"
        # Two repos can share a display name; add their counters rather than drop one.
        prev = files.get(key) or {"hits": 0, "reads": 0, "edits": 0}
        files[key] = {"hits": prev["hits"] + hits, "reads": prev["reads"] + reads, "edits": prev["edits"] + edits}
    return files


def compute(doc: dict[str, Any]) -> dict[str, Any]:
    """The facts for one map document.

    Args:
        doc: an ``agentplay/v1`` map, as the builder emits it or the store holds it.

    Returns:
        A JSON-ready dict; see the module docstring for what it may contain.
    """
    s = doc.get("session") or {}
    t = doc.get("totals") or {}
    evs = sorted(doc.get("events") or [], key=lambda e: e.get("seq", 0))
    outcome = str(s.get("outcome") or "completed")
    llm = [e for e in evs if e["kind"] == "llm"]

    cmd: dict[str, dict[str, Any]] = {}
    for e in evs:
        if e["kind"] not in _COMMAND_KINDS:
            continue
        verb = command_label(e)
        c = cmd.setdefault(verb, {"calls": 0, "fails": 0, "retries": 0, "byClass": {}, "verify": False, "outBytes": 0})
        c["calls"] += 1
        c["outBytes"] += int(e.get("outBytes") or 0)
        if e["kind"] == "verify":
            c["verify"] = True
        if e.get("retryOf") is not None:
            c["retries"] += 1
        if e.get("err"):
            c["fails"] += 1
            cls = str(e.get("errClass") or "other")
            c["byClass"][cls] = c["byClass"].get(cls, 0) + 1

    files = file_counts(doc)
    files_rest: dict[str, int] = {}
    if len(files) > MAX_FACT_FILES:
        ranked = sorted(files.items(), key=lambda kv: (-kv[1]["hits"], -(kv[1]["reads"] + kv[1]["edits"]), kv[0]))
        rest = [c for _, c in ranked[MAX_FACT_FILES:]]
        files = dict(ranked[:MAX_FACT_FILES])
        files_rest = {"count": len(rest), **{k: sum(c[k] for c in rest) for k in ("hits", "reads", "edits")}}

    edits = [e for e in evs if e["kind"] == "edit"]
    verifies = [e for e in evs if e["kind"] == "verify" and not e.get("err")]
    last_edit = max((e["seq"] for e in edits), default=None)
    users = [e for e in evs if e["kind"] == "user" and e.get("agent") == "main"]

    by_source: dict[str, dict[str, int]] = {}
    for e in evs:
        if e.get("err"):
            classes = by_source.setdefault(_error_source(e), {})
            cls = str(e.get("errClass") or "other")
            classes[cls] = classes.get(cls, 0) + 1

    mcp: dict[str, dict[str, int]] = {}
    hooks: dict[str, int] = {}
    subagents: dict[str, dict[str, int]] = {}
    for e in evs:
        if e["kind"] == "mcp":
            m = mcp.setdefault(str(e.get("server") or "?")[:60], {"calls": 0, "fails": 0})
            m["calls"] += 1
            m["fails"] += int(bool(e.get("err")))
        elif e.get("err") and _error_source(e) == "hook":
            name = str(e.get("hook") or e.get("hookEvent") or "hook")[:60]
            hooks[name] = hooks.get(name, 0) + 1
        elif e["kind"] == "subagent":
            sa = subagents.setdefault(str(e.get("subagentType") or "general")[:60], {"launches": 0, "noResult": 0})
            sa["launches"] += 1
            sa["noResult"] += int(bool(e.get("err")) and e.get("errClass") in _DEAD_LANE)

    return {
        "outcome": outcome,
        "edited": bool(edits),
        "prsCreated": int(t.get("prsCreated") or 0),
        "prsMerged": int(t.get("prsMerged") or 0),
        "costUsd": float(t.get("costUsd") or 0.0),
        # Cursor writes requests with no usable cost; a zero total means "unknown", not "free".
        "hasCost": float(t.get("costUsd") or 0.0) > 0,
        "tokens": {
            "in": sum(int(e.get("in") or 0) for e in llm),
            "cw": sum(int(e.get("cw") or 0) for e in llm),
            "cr": sum(int(e.get("cr") or 0) for e in llm),
            "out": sum(int(e.get("out") or 0) for e in llm),
        },
        "costByModel": _cost_by_model(llm),
        "calls": int(t.get("calls") or 0),
        "errors": int(t.get("errors") or 0),
        "retries": int(t.get("retries") or 0),
        "cmd": cmd,
        "errorsByClass": dict(t.get("errorsByClass") or {}),
        "errorsBySource": by_source,
        "files": files,
        **({"filesRest": files_rest} if files_rest else {}),
        "compactions": int(t.get("compactions") or 0),
        "preTokens": [int(e.get("preTokens") or 0) for e in evs if e["kind"] == "compaction"],
        "maxContext": int(t.get("maxContext") or 0),
        "rereadsAfterCompaction": _rereads_after_compaction(evs),
        "verifyAfterLastEdit": last_edit is not None and any(v["seq"] > last_edit for v in verifies),
        "waste": _waste(doc, evs, outcome),
        "stallMs": sum(int(e.get("ms") or 0) for e in evs if e["kind"] == "delay" and e.get("cause") == "stall"),
        "firstPromptChars": int(users[0].get("chars") or 0) if users else 0,
        "userTurns": len(users),
        "mcp": mcp,
        "hookFails": hooks,
        "subagents": subagents,
        **_explore(evs),
    }


def command_label(e: dict[str, Any]) -> str:
    """The command a shell event ran, as its verb plus a git or gh action (``git push``, ``gh pr_create``).

    Never arguments: the builder only stores the verb, and a VCS action is
    a closed vocabulary.
    """
    verb = str(e.get("verb") or e.get("tool") or "?")
    action = (e.get("vcs") or {}).get("action")
    if action and verb.split(" ")[0] in ("git", "gh") and str(action) not in verb:
        verb = f"{verb} {action}"
    return verb[:60]


def _explore(evs: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether the agent read before it edited (main agent only)."""
    read_first: set[int] = set()
    edited: dict[int, int] = {}
    blind = rework = 0
    steps_to_edit: int | None = None
    explore = act = 0
    last_edit_seq: dict[int, int] = {}
    main = [e for e in evs if e.get("agent") == "main" and e["kind"] not in _NON_CALL_KINDS]
    for i, e in enumerate(main):
        if e["kind"] in ("read", "search"):
            explore += 1
        elif e["kind"] in ("edit", "exec", "verify"):
            act += 1
        for t in e.get("targets") or []:
            fid = t.get("file")
            if not isinstance(fid, int):
                continue
            if t.get("touch") == "read":
                read_first.add(fid)
            elif t.get("touch") == "edit":
                if steps_to_edit is None:
                    steps_to_edit = i
                if fid not in edited and fid not in read_first and t.get("op") != "create":
                    blind += 1
                prev = last_edit_seq.get(fid)
                if e.get("err") or (prev is not None and i - prev <= 5):
                    rework += 1
                edited[fid] = edited.get(fid, 0) + 1
                last_edit_seq[fid] = i
    return {
        "explore": explore,
        "act": act,
        "editedFiles": len(edited),
        "blindEdits": blind,
        "reworkEdits": rework,
        "stepsToFirstEdit": steps_to_edit,
    }


def _cost_by_model(llm: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for e in llm:
        if "cost" not in e:
            continue
        m = str(e.get("model") or "unknown")
        out[m] = round(out.get(m, 0.0) + float(e.get("cost") or 0.0), 6)
    return out


def stamp(doc: dict[str, Any]) -> dict[str, Any]:
    """The index-row fields that carry a map's facts.

    ``facts`` is None for a map that cannot be summarised (an imported
    document missing fields the builder always writes); Insights skips it
    rather than failing the whole view.
    """
    try:
        value: dict[str, Any] | None = compute(doc)
    except (KeyError, TypeError, ValueError, AttributeError):
        value = None
    return {"facts": value, "factsVersion": FACTS_VERSION}
