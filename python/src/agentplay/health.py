"""Run health: a 0-100 score over a window of sessions, and the specific fixes that would raise it.

Five components worth 20 points each, every one computed from session facts
(``facts.py``) of root sessions:

========================  ====================================================
Shipping                  sessions that opened a PR / sessions with an edit
Verification              sessions verified after the last edit / with an edit
Efficiency                0.5 x (1 - min(1, 4 x (errors + retries) / calls))
                          + 0.5 x cache hit rate
Context hygiene           0.6 x (1 - share compacting)
                          + 0.4 x (1 - min(1, re-reads after compaction / reads))
Environment health        1 - min(1, 10 x configuration errors / calls)
========================  ====================================================

A fix is only proposed when it can name the command, tool or server behind
it, and its points are the score it would recover: the component recomputed
with the pattern's effect removed (or raised to a target rate), minus now.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

MIN_SESSIONS = 10
CONFIG_CLASSES = frozenset({"permission_denied", "blocked", "not_found"})
VERIFY_TARGET = 0.7

# Commands with a known quieter form. Only these can get a "noisy output"
# fix: without one, the fix would be advice, not an instruction.
QUIET_FORMS = {
    "pytest": "pytest -q",
    "npm test": "npm test -- --silent",
    "pnpm test": "pnpm test --silent",
    "yarn test": "yarn test --silent",
    "jest": "jest --silent",
    "vitest": "vitest run --reporter=dot",
    "pnpm vitest": "pnpm vitest run --reporter=dot",
    "cargo test": "cargo test -q",
    "go test": "go test ./...",  # quiet by default; only -v is verbose
}
NOISY_BYTES = 40_000  # about 10k tokens of output per run

# What a check command is for. A passing command only replaces a failing one
# that does the same job: `pnpm vitest` for `pnpm test`, never `git commit`
# for `git push` or a linter for a test runner.
_PURPOSES = (
    ("test", ("test", "vitest", "jest", "pytest", "mocha", "spec")),
    ("lint", ("lint", "ruff", "eslint", "clippy", "flake8", "biome")),
    ("typecheck", ("tsc", "mypy", "pyright", "typecheck", "type-check")),
    ("build", ("build", "compile")),
)


def _tokens(verb: str) -> list[str]:
    """The lowercase words of a command, split on anything but letters and digits."""
    low = verb.lower()
    # A compound job word (`type-check`) is one word: joined before splitting, so its
    # pieces (`check`) never become generic and strip a real target.
    for w in (w for _, ws in _PURPOSES for w in ws if not w.isalnum()):
        low = low.replace(w, re.sub(r"[^a-z0-9]+", "", w))
    return [w for w in re.split(r"[^a-z0-9]+", low) if w]


def _scope(verb: str) -> frozenset[str]:
    """What a check is narrowed to beyond its job: ``pnpm test:unit`` -> {"unit"}, ``pnpm vitest`` -> {}.

    Two checks only stand in for each other when their scopes match, so a
    failing unit suite is never "replaced" by a passing e2e one.
    """
    words = _tokens(verb)[1:]
    # Whole job words only (and their plurals): `unittest` or `e2etests` keep their target.
    job_words = {re.sub(r"[^a-z0-9]+", "", w) for _, ws in _PURPOSES for w in ws}
    generic = job_words | {"run", "exec", "x"}
    return frozenset(w for w in words if w not in generic and not (w.endswith("s") and w[:-1] in job_words))


def purpose(verb: str) -> str | None:
    # Whole words (and their plurals), so `latest` is not a test and `rebuild` is not a build.
    words = _tokens(verb)
    tokens = {*words, *(t.removesuffix("s") for t in words)}
    for name, names in _PURPOSES:
        if any(re.sub(r"[^a-z0-9]+", "", w) in tokens for w in names):
            return name
    return None


@dataclass
class Totals:
    """The sums every component is computed from."""

    sessions: int = 0
    edited: int = 0
    opened: int = 0
    verified: int = 0
    calls: int = 0
    errors: int = 0
    retries: int = 0
    tok_in: int = 0
    tok_cw: int = 0
    tok_cr: int = 0
    compacting: int = 0
    reads: int = 0
    rereads_ac: int = 0
    config_errors: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _config_errors(f: dict[str, Any]) -> int:
    n = 0
    for src, classes in (f.get("errorsBySource") or {}).items():
        for cls, k in classes.items():
            if src in ("hook", "mcp") or cls in CONFIG_CLASSES:
                n += int(k)
    return n


def totals(rows: Iterable[dict[str, Any]]) -> Totals:
    t = Totals()
    for r in rows:
        f = r.get("facts") or {}
        t.sessions += 1
        if f.get("edited"):
            t.edited += 1
            t.opened += int(f.get("prsCreated", 0) > 0)
            t.verified += int(bool(f.get("verifyAfterLastEdit")))
        t.calls += int(f.get("calls") or 0)
        t.errors += int(f.get("errors") or 0)
        t.retries += int(f.get("retries") or 0)
        if f.get("hasCost"):
            tok = f.get("tokens") or {}
            t.tok_in += int(tok.get("in") or 0)
            t.tok_cw += int(tok.get("cw") or 0)
            t.tok_cr += int(tok.get("cr") or 0)
        t.compacting += int((f.get("compactions") or 0) > 0)
        t.reads += sum(int(x.get("reads") or 0) for x in (f.get("files") or {}).values())
        t.reads += int((f.get("filesRest") or {}).get("reads") or 0)  # files past facts.MAX_FACT_FILES
        t.rereads_ac += int(f.get("rereadsAfterCompaction") or 0)
        t.config_errors += _config_errors(f)
    return t


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def components(t: Totals) -> dict[str, float | None]:
    """Each component as a 0-1 value, or None when it has nothing to measure."""
    err_part = 1 - min(1.0, 4 * (t.errors + t.retries) / t.calls) if t.calls else None
    tok = t.tok_in + t.tok_cw + t.tok_cr
    cache = t.tok_cr / tok if tok else None
    if err_part is None:
        eff = None
    elif cache is None:
        eff = err_part  # no cost data (Cursor only): missing data is neutral
    else:
        eff = 0.5 * err_part + 0.5 * cache
    return {
        "shipping": t.opened / t.edited if t.edited else None,
        "verification": t.verified / t.edited if t.edited else None,
        "efficiency": _clamp(eff) if eff is not None else None,
        "context": _clamp(
            0.6 * (1 - t.compacting / t.sessions) + 0.4 * (1 - min(1.0, t.rereads_ac / t.reads if t.reads else 0.0))
        ) if t.sessions else None,
        "environment": _clamp(1 - min(1.0, 10 * t.config_errors / t.calls)) if t.calls else None,
    }


def score(t: Totals) -> float | None:
    c = components(t)
    if t.sessions < MIN_SESSIONS:
        return None
    # A component with nothing to measure counts in full: it can't be fixed.
    return round(sum(20 * (v if v is not None else 1.0) for v in c.values()), 1)


def _points(base: Totals, changed: Totals, key: str) -> float:
    a, b = components(base)[key], components(changed)[key]
    if a is None or b is None:
        return 0.0
    return round(max(0.0, 20 * (b - a)), 1)


NAMES = {
    "shipping": "Shipping",
    "verification": "Verification",
    "efficiency": "Efficiency",
    "context": "Context hygiene",
    "environment": "Environment health",
}


# ------------------------------------------------------------------ fixes


def fix_id(rule: str, repo: str, subject: str) -> str:
    return hashlib.sha256(f"{rule}\0{repo}\0{subject}".encode()).hexdigest()[:12]


def _by_repo(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        repos = r.get("repos") or []
        if repos:
            out.setdefault(str(repos[0]), []).append(r)
    return out


def _cmds(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        for verb, c in ((r.get("facts") or {}).get("cmd") or {}).items():
            a = out.setdefault(verb, {"calls": 0, "fails": 0, "retries": 0, "outBytes": 0, "verify": False,
                                      "denied": 0, "deniedSessions": 0, "sessions": []})
            a["calls"] += int(c.get("calls") or 0)
            a["fails"] += int(c.get("fails") or 0)
            a["retries"] += int(c.get("retries") or 0)
            a["outBytes"] += int(c.get("outBytes") or 0)
            a["verify"] = a["verify"] or bool(c.get("verify"))
            denied = int((c.get("byClass") or {}).get("permission_denied", 0)) + int((c.get("byClass") or {}).get("blocked", 0))
            a["denied"] += denied
            a["deniedSessions"] += int(denied > 0)
            a["sessions"].append(r.get("sessionId"))
    return out


def _fix(rule: str, repo: str, subject: str, component: str, title: str, pattern: str, effect: str,
         points: float, sessions: list[str], actions: list[dict[str, Any]], metric: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": fix_id(rule, repo, subject),
        "rule": rule,
        "repo": repo,
        "subject": subject,
        "component": component,
        "title": title,
        "pattern": pattern,
        "effect": effect,
        "points": points,
        "sessions": [s for s in sessions if s][:50],
        "actions": actions,
        "metric": metric,
    }


def fixes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Specific fixes for the sessions in ``rows``, highest points first."""
    base = totals(rows)
    out: list[dict[str, Any]] = []
    for repo, rrows in _by_repo(rows).items():
        cmds = _cmds(rrows)
        rt = totals(rrows)

        # Verification: name the commands that already pass here.
        if rt.edited >= 5 and rt.verified / rt.edited < 1 - 0.3:
            passing = sorted(
                (v for v, c in cmds.items() if c["verify"] and c["calls"] >= 3 and 1 - c["fails"] / c["calls"] >= 0.6),
                key=lambda v: -cmds[v]["calls"],
            )[:2]
            if passing:
                gain = max(0.0, VERIFY_TARGET * rt.edited - rt.verified)
                changed = replace(base, verified=min(base.edited, base.verified + round(gain)))
                unverified = [r["sessionId"] for r in rrows
                              if (r.get("facts") or {}).get("edited") and not (r.get("facts") or {}).get("verifyAfterLastEdit")]
                rates = ", ".join(f"`{v}` passes {round(100 * (1 - cmds[v]['fails'] / cmds[v]['calls']))}% of runs" for v in passing)
                joined = " && ".join(passing)
                out.append(_fix(
                    "verify_before_stop", repo, joined, "verification",
                    "Run the passing checks before the agent stops",
                    f"{rt.edited - rt.verified} of {rt.edited} sessions with edits ended without a check after the last edit. Here {rates}.",
                    f"Sessions verified after the last edit rise from {round(100 * rt.verified / rt.edited)}% toward {round(100 * VERIFY_TARGET)}%.",
                    _points(base, changed, "verification"), unverified,
                    [
                        {"kind": "stop_hook", "label": "Add Stop hook", "file": ".claude/settings.json", "command": joined},
                        {"kind": "claude_md", "label": "Add to CLAUDE.md instead", "file": "CLAUDE.md",
                         "lines": [f"Before reporting done, run {' and '.join(f'`{v}`' for v in passing)}; they must pass."]},
                    ],
                    {"name": "verified_share"},
                ))

        # Efficiency: a failing check whose sibling, doing the same job, passes.
        for bad, c in cmds.items():
            job = purpose(bad)
            if job is None or " " not in bad or c["fails"] < 5 or c["fails"] / c["calls"] < 0.4:
                continue
            family = bad.split()[0]
            good = sorted(
                (v for v, g in cmds.items() if v != bad and v.split()[0] == family and purpose(v) == job
                 and _scope(v) == _scope(bad) and g["calls"] >= 3 and 1 - g["fails"] / g["calls"] >= 0.8),
                key=lambda v: -cmds[v]["calls"],
            )
            if not good:
                continue
            g = cmds[good[0]]
            changed = replace(base, errors=max(0, base.errors - c["fails"]), retries=max(0, base.retries - c["retries"]))
            out.append(_fix(
                "use_working_command", repo, bad, "efficiency",
                f"Use `{good[0]}` instead of `{bad}`",
                f"`{bad}` failed {c['fails']} of {c['calls']} runs here; `{good[0]}` passed {g['calls'] - g['fails']} of {g['calls']}.",
                f"About {c['fails']} fewer failed runs and {c['retries']} fewer retries over the same number of sessions.",
                _points(base, changed, "efficiency"), c["sessions"],
                [{"kind": "claude_md", "label": "Add to CLAUDE.md", "file": "CLAUDE.md",
                  "lines": [f"Run `{good[0]}`, not `{bad}`: `{bad}` fails in this repo."]}],
                {"name": "fail_rate", "verb": bad},
            ))

        # Context: output large enough to fill context, with a known quieter form.
        for verb, c in cmds.items():
            quiet = QUIET_FORMS.get(verb)
            if not quiet or c["calls"] < 10 or c["outBytes"] / c["calls"] < NOISY_BYTES:
                continue
            ktok = round(c["outBytes"] / c["calls"] / 4 / 1000)
            out.append(_fix(
                "quiet_output", repo, verb, "context",
                f"Run `{quiet}` so test output stops filling context",
                f"`{verb}` output averaged about {ktok}k tokens per run across {c['calls']} runs.",
                f"Most of those {ktok}k tokens per run stay out of context, so sessions compact less often.",
                0.0, c["sessions"],
                [{"kind": "claude_md", "label": "Add to CLAUDE.md", "file": "CLAUDE.md",
                  "lines": [f"Run tests as `{quiet}`; rerun only a failing test for detail."]}],
                {"name": "out_bytes", "verb": verb},
            ))

        # Environment: the same command refused in several sessions.
        for verb, c in cmds.items():
            if c["deniedSessions"] < 3 or "_" in verb:
                continue
            changed = replace(base, config_errors=max(0, base.config_errors - c["denied"]))
            out.append(_fix(
                "allow_command", repo, verb, "environment",
                f"Allow `{verb}` in {repo}",
                f"`{verb}` was refused {c['denied']} times in {c['deniedSessions']} sessions, each a stop for your approval.",
                f"No permission stops on `{verb}` in {repo}.",
                _points(base, changed, "environment"), c["sessions"],
                [{"kind": "settings_allow", "label": "Add to settings.local.json", "file": ".claude/settings.local.json",
                  "entry": f"Bash({verb}:*)"}],
                {"name": "denials", "verb": verb},
            ))

    # Store-wide: integrations that fail. No file can fix these, so the fix names them.
    mcp: dict[str, dict[str, Any]] = {}
    hooks: dict[str, dict[str, Any]] = {}
    subs: dict[str, dict[str, Any]] = {}
    for r in rows:
        f = r.get("facts") or {}
        for name, m in (f.get("mcp") or {}).items():
            a = mcp.setdefault(name, {"calls": 0, "fails": 0, "sessions": []})
            a["calls"] += m.get("calls", 0)
            a["fails"] += m.get("fails", 0)
            if m.get("fails"):
                a["sessions"].append(r.get("sessionId"))
        for name, n in (f.get("hookFails") or {}).items():
            a = hooks.setdefault(name, {"fails": 0, "sessions": []})
            a["fails"] += n
            a["sessions"].append(r.get("sessionId"))
        for name, s in (f.get("subagents") or {}).items():
            a = subs.setdefault(name, {"launches": 0, "noResult": 0, "sessions": []})
            a["launches"] += s.get("launches", 0)
            a["noResult"] += s.get("noResult", 0)
            if s.get("noResult"):
                a["sessions"].append(r.get("sessionId"))
    for name, m in mcp.items():
        if m["calls"] >= 10 and m["fails"] / m["calls"] > 0.2:
            changed = replace(base, config_errors=max(0, base.config_errors - m["fails"]))
            out.append(_fix(
                "broken_mcp", "", name, "environment",
                f"Fix or remove the `{name}` MCP server",
                f"`{name}` failed {m['fails']} of {m['calls']} calls ({round(100 * m['fails'] / m['calls'])}%).",
                "Agents stop spending turns on a server that errors.",
                _points(base, changed, "environment"), m["sessions"], [],
            ))
    for name, h in hooks.items():
        if h["fails"] >= 5:
            changed = replace(base, config_errors=max(0, base.config_errors - h["fails"]))
            out.append(_fix(
                "broken_hook", "", name, "environment",
                f"Fix the `{name}` hook",
                f"`{name}` failed {h['fails']} times in {len(h['sessions'])} sessions.",
                "Tool calls stop being blocked by a failing hook.",
                _points(base, changed, "environment"), h["sessions"], [],
            ))
    for name, s in subs.items():
        if s["launches"] >= 5 and s["noResult"] / s["launches"] > 0.3:
            out.append(_fix(
                "dead_subagent", "", name, "efficiency",
                f"Narrow or stop using the `{name}` subagent",
                f"`{name}` ended with no result in {s['noResult']} of {s['launches']} launches.",
                "Less spend on subagents that return nothing.",
                0.0, s["sessions"], [],
            ))
    out.sort(key=lambda x: (-x["points"], x["title"]))
    return out


# ------------------------------------------------------------------ report


def _worst(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    def why(f: dict[str, Any]) -> tuple[float, str] | None:
        if key == "shipping" and f.get("edited") and not f.get("prsCreated"):
            return f.get("costUsd") or 0, "edited code, no PR"
        if key == "verification" and f.get("edited") and not f.get("verifyAfterLastEdit"):
            return f.get("costUsd") or 0, "no check after the last edit"
        if key == "efficiency" and (f.get("errors", 0) + f.get("retries", 0)):
            n = f.get("errors", 0) + f.get("retries", 0)
            return n, f"{n} failed calls and retries"
        if key == "context" and f.get("compactions"):
            return f["compactions"], f"{f['compactions']} compactions"
        if key == "environment":
            n = _config_errors(f)
            if n:
                return n, f"{n} configuration errors"
        return None

    scored = []
    for r in rows:
        w = why(r.get("facts") or {})
        if w:
            scored.append((w[0], r.get("sessionId"), w[1]))
    scored.sort(key=lambda x: -x[0])
    return [{"sessionId": sid, "why": text} for _, sid, text in scored[:3]]


def report(rows: list[dict[str, Any]], prev_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Everything the Run health page shows, for root sessions in a window."""
    t = totals(rows)
    comps = components(t)
    prev = components(totals(prev_rows)) if prev_rows else {}
    tok = t.tok_in + t.tok_cw + t.tok_cr
    inputs = {
        "shipping": [["Sessions with edits", t.edited], ["Opened a PR", t.opened]],
        "verification": [["Sessions with edits", t.edited], ["Verified after last edit", t.verified]],
        "efficiency": [["Errors + retries", t.errors + t.retries], ["Calls", t.calls],
                       ["Cache hit rate", f"{round(100 * t.tok_cr / tok)}%" if tok else "n/a"]],
        "context": [["Sessions compacting", t.compacting], ["Re-reads after compaction", t.rereads_ac], ["Reads", t.reads]],
        "environment": [["Configuration errors", t.config_errors], ["Calls", t.calls]],
    }
    prev_score = score(totals(prev_rows)) if prev_rows else None
    return {
        "sessions": t.sessions,
        "minSessions": MIN_SESSIONS,
        "score": score(t),
        "prevScore": prev_score,
        "components": [
            {
                "key": k,
                "name": NAMES[k],
                "value": comps[k],
                "points": round(20 * comps[k], 1) if comps[k] is not None else None,
                "prevPoints": round(20 * prev[k], 1) if prev.get(k) is not None else None,
                "inputs": inputs[k],
                "worst": _worst(rows, k),
            }
            for k in NAMES
        ],
        "fixes": fixes(rows),
    }
