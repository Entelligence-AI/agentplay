"""Search across every stored map: which sessions touched a file, command, MCP server and so on.

Each map yields postings (``term -> [[seq, touch], ...]``) built only from
fields the map already holds: repo-relative paths, command verbs, MCP server
and tool names, skill and subagent types, models, repos, web hosts and error
classes. Command arguments, prompt and output text are never stored, so they
can never be searched.

Postings live in ``~/.agentplay/search.json`` beside the index, rebuilt for a
session whenever its map's content hash changes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from .facts import command_label

SEARCH_VERSION = 3
FACETS = ("file", "cmd", "mcp", "skill", "subagent", "model", "repo", "host", "err")
# Postings kept per term, per touch kind, per session: enough to step through, small enough to load.
MAX_SEQS = 40
_FACET_RE = re.compile(rf"^({'|'.join(FACETS)}):(.+)$", re.I)

TOUCHES = ("hit", "read", "edit", "fail")


def postings(doc: dict[str, Any]) -> dict[str, list[list[Any]]]:
    """``term -> [[seq, touch], ...]`` for one map. Session-level terms carry seq None.

    Each touch kind keeps its first ``MAX_SEQS`` postings; when it overflows, its
    first posting carries the true total as a third item.
    """
    out: dict[str, list[list[Any]]] = {}
    firsts: dict[tuple[str, str], list[Any]] = {}
    totals: dict[tuple[str, str], int] = {}
    files = {f.get("id"): f for f in doc.get("files") or []}
    repos = {r.get("id"): r.get("name") for r in doc.get("repos") or []}

    def add(term: str, seq: int | None, touch: str) -> None:
        term = term.lower()
        key = (term, touch)
        totals[key] = totals.get(key, 0) + 1
        if totals[key] <= MAX_SEQS:
            posting = [seq, touch]
            firsts.setdefault(key, posting)
            out.setdefault(term, []).append(posting)

    for e in doc.get("events") or []:
        seq = e.get("seq")
        failed = bool(e.get("err"))
        kind = e.get("kind")
        for t in e.get("targets") or []:
            f = files.get(t.get("file"))
            if not f or not f.get("path"):
                continue
            touch = "fail" if failed else str(t.get("touch") or "hit")
            path = str(f["path"])
            add(f"file:{repos.get(f.get('repo'), '')}:{path}", seq, touch)
            base = path.rsplit("/", 1)[-1]
            if base != path:
                add(f"file:{base}", seq, touch)
        if kind in ("exec", "verify"):
            add(f"cmd:{command_label(e)}", seq, "fail" if failed else "read")
        elif kind == "mcp" and e.get("server"):
            add(f"mcp:{e['server']}", seq, "fail" if failed else "read")
            if e.get("tool"):
                add(f"mcp:{e['server']}/{e['tool']}", seq, "fail" if failed else "read")
        if e.get("skill"):
            add(f"skill:{e['skill']}", seq, "fail" if failed else "read")
        if kind == "subagent" and e.get("subagentType"):
            add(f"subagent:{e['subagentType']}", seq, "fail" if failed else "read")
        if e.get("host"):
            add(f"host:{e['host']}", seq, "fail" if failed else "read")
        if failed and e.get("errClass"):
            add(f"err:{e['errClass']}", seq, "fail")
    for m in (doc.get("session") or {}).get("models") or []:
        add(f"model:{m}", None, "hit")
    for name in repos.values():
        if name:
            add(f"repo:{name}", None, "hit")
    for key, n in totals.items():
        if n > MAX_SEQS:
            firsts[key].append(n)
    return out


# ------------------------------------------------------------------ the index file


def load(path: Any) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != SEARCH_VERSION:
        return {}
    sessions = raw.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def dump(sessions: dict[str, Any]) -> bytes:
    return json.dumps({"version": SEARCH_VERSION, "sessions": sessions}, separators=(",", ":")).encode("utf-8")


def refresh(store: Any) -> int:
    """Bring ``search.json`` up to date with the store. Returns how many sessions were (re)indexed.

    Reads a map only when its content hash changed since it was indexed, so a
    run with nothing new reads none.
    """
    from .store import _atomic_write

    path = store.base / "search.json"
    old = load(path)
    out: dict[str, Any] = {}
    n = 0
    for sid, row in store.index.items():
        h = str(row.get("contentHash") or "")
        prev = old.get(sid)
        # An empty hash says nothing about content, so it never counts as current.
        if h and isinstance(prev, dict) and prev.get("hash") == h:
            out[sid] = prev
            continue
        doc = store.get(sid)
        if doc is None:
            continue
        try:
            out[sid] = {"hash": h, "terms": postings(doc)}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        n += 1
    if n or len(out) != len(old):
        _atomic_write(path, dump(out))
    return n


# ------------------------------------------------------------------ querying


def parse(q: str) -> tuple[str | None, str]:
    """``file:auth.ts`` -> (``file``, ``auth.ts``); a bare query matches every facet."""
    q = q.strip()
    m = _FACET_RE.match(q)
    if m:
        return m.group(1).lower(), m.group(2).strip().lower()
    return None, q.lower()


def _matches(term: str, facet: str | None, value: str) -> bool:
    f, _, v = term.partition(":")
    if facet and f != facet:
        return False
    if f == "file":
        # The full key is `file:<repo>:<path>`; match on the path, any repo.
        v = v.split(":", 1)[-1] if v.count(":") else v
    return value in v


_VERBS = {"hit": "found", "read": "read", "edit": "edited", "fail": "failed"}


def _label(term: str) -> str:
    f, _, v = term.partition(":")
    if f == "file":
        v = v.split(":", 1)[-1].rsplit("/", 1)[-1]
    return v


def query(terms_by_session: dict[str, dict[str, list[list[Any]]]], q: str) -> dict[str, dict[str, Any]]:
    """Sessions matching ``q``: ``sid -> {count, seqs, chips}``.

    ``chips`` summarise the matches a person reads first ("edited auth.ts 3×"),
    each with the first matching ``seq`` to open the session at.
    """
    facet, value = parse(q)
    if not value:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sid, terms in terms_by_session.items():
        hits = [(t, p) for t, ps in terms.items() if _matches(t, facet, value) for p in ps]
        if not hits:
            continue
        groups: dict[tuple[str, str], list[int | None]] = {}
        # Per group: totals by full-path term (distinct files add up), and the
        # basename alias's total, used only when no full-path term matched.
        full: dict[tuple[str, str], dict[str, int]] = {}
        alias: dict[tuple[str, str], int] = {}
        for t, (seq, touch, *total) in hits:
            # The basename and the full-path term of one file are the same match.
            key = (_label(t), touch)
            if seq not in groups.setdefault(key, []):
                groups[key].append(seq)
            if total:
                if t.startswith("file:") and t.count(":") >= 2:
                    by_term = full.setdefault(key, {})
                    by_term[t] = max(by_term.get(t, 0), total[0])
                else:
                    alias[key] = max(alias.get(key, 0), total[0])
        totals = {k: sum(full[k].values()) if full.get(k) else alias.get(k, 0) for k in groups}
        # Stored postings are capped per touch kind, so cap per kind here too or a
        # busy file's reads would crowd its edits out of the stepper.
        by_touch: dict[str, set[int]] = {}
        for (_, touch), s in groups.items():
            by_touch.setdefault(touch, set()).update(x for x in s if x is not None)
        seqs = sorted({x for v in by_touch.values() for x in sorted(v)[:MAX_SEQS]})
        counts = {k: max(len(s), totals.get(k, 0)) for k, s in groups.items()}
        chips = sorted(
            (
                {
                    "label": f"{_VERBS.get(touch, touch)} {label}" + (f" {counts[label, touch]}×" if s[0] is not None else ""),
                    "touch": touch,
                    "seq": min((x for x in s if x is not None), default=None),
                    "n": counts[label, touch],
                }
                for (label, touch), s in groups.items()
            ),
            key=lambda c: (-TOUCHES.index(c["touch"]) if c["touch"] in TOUCHES else 0, -c["n"]),
        )
        truncated = bool(totals)
        count = sum(counts.values()) if truncated else len(seqs) or len(hits)
        out[sid] = {"count": count, "seqs": seqs, "chips": chips[:4]}
    return out


def argument_fallback(terms_by_session: dict[str, dict[str, list[list[Any]]]], q: str) -> tuple[str, dict[str, Any]] | None:
    """A query that names command arguments (``pnpm test auth``) can't match: arguments
    are never stored. Retry with the longest leading words that exactly name a recorded
    command (``pnpm test``) or its family (``git`` for ``git push``), never a substring."""
    facet, value = parse(q)
    if facet not in (None, "cmd"):
        return None
    words = value.split()
    for n in range(len(words) - 1, 0, -1):
        prefix = " ".join(words[:n])
        own = f"cmd:{prefix}"
        exact = {
            sid: {t: ps for t, ps in terms.items() if t == own or t.startswith(own + " ")}
            for sid, terms in terms_by_session.items()
        }
        hits = query(exact, own)
        if hits:
            return prefix, hits
    return None


def iter_terms(rows: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, list[list[Any]]]]:
    return {sid: entry.get("terms") or {} for sid, entry in rows if isinstance(entry, dict)}
