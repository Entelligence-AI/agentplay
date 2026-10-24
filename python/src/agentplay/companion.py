"""Read-only snapshot for the macOS companion: ``agentplay companion snapshot --json``.

The companion (``apps/macos/AgentPlayCompanion``) runs this as a child process and
reads one JSON document from stdout. Nothing listens: there is no port, socket or
endpoint, so a web page cannot reach it.

What a snapshot holds, and what it never holds:

* Runs: the newest root sessions with their last few recorded events (kind,
  shell verb, tool, repo-relative path as the map records it), error classes,
  retries and the recorded activity evidence.
* Every number that could be misread is a :func:`point` (a ``DataPoint``):
  value, kind (recorded / derived / estimated / provider_reported /
  unavailable), source, observedAt, window, confidence and a plain explanation.
* Never: session titles or task labels (prompt text), transcript paths, repo
  remotes, PR URLs, command arguments, tool inputs or outputs. Maps carry none of
  the text to begin with (see ``docs/format.md``); the index fields that do are
  simply not read.

The only file read outside the store is the tail of a Codex transcript, for the
numeric ``token_count`` record Codex writes itself (context window and plan
windows). Only numbers and the plan label are taken from it.

Writes nothing, unless ``--refresh`` asks for an incremental build first, which is
the same build ``agentplay build`` does.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__, discover
from .store import Store

SCHEMA = "agentplay.companion/v1"
SCHEMA_VERSION = 1

# How many runs the popover can show (two cards plus "View all").
MAX_RUNS = 12
# Events per run in the micro timeline.
TAIL_EVENTS = 5
# A transcript written to this recently is "activity recorded recently".
RECENT_S = 10 * 60
# Learn card window and thresholds.
LEARN_DAYS = 14
LEARN_MAX_SESSIONS = 40
LEARN_MIN_FAILS = 4
LEARN_MIN_RUNS = 2
# Repeated failure: this many failures of one command family, with no success of
# it in between, inside this window.
REPEAT_N = 3
REPEAT_WINDOW_MS = 30 * 60 * 1000
# Codex: bytes read from the end of a transcript to find its last token_count.
CODEX_TAIL_BYTES = 512 * 1024

PROVIDER_USAGE_URLS = {
    "claude-code": "https://claude.ai/settings/usage",
    "codex": "https://chatgpt.com/codex/settings/usage",
    "cursor": "https://cursor.com/dashboard",
}

_AGENT_LABELS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "copilot": "Copilot CLI",
    "copilot-vscode": "Copilot (VS Code)",
    "opencode": "OpenCode",
    "kiro": "Kiro",
}
# Agents whose sessions live in one shared database: a fresh mtime says the
# agent wrote something, not which session.
_STATUS_WORDS = {"busy": "working", "waiting": "waiting for you", "idle": "idle, waiting for your next prompt"}
_SHARED_STORE_AGENTS = {"cursor", "opencode", "kiro", "copilot-vscode"}
# Process names that write each agent's transcripts.
_PROCESS_NAMES = {"claude-code": ("claude",), "codex": ("codex",)}


# --------------------------------------------------------------------------- helpers
def iso(ts: float | datetime | None) -> str | None:
    if ts is None:
        return None
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, tz=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def point(
    value: Any,
    kind: str,
    source: str,
    *,
    observed_at: str | None = None,
    window: str = "none",
    confidence: str = "high",
    explanation: str = "",
    freshness: str = "unknown",
) -> dict[str, Any]:
    """A ``DataPoint``. ``kind == unavailable`` always carries ``value: None``."""
    if kind == "unavailable":
        value, source, confidence = None, "none", "low"
    return {
        "value": value,
        "kind": kind,
        "source": source,
        "observedAt": observed_at,
        "window": window,
        "confidence": confidence,
        "explanation": explanation,
        "freshness": freshness,
    }


def _home_label(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home):] if s.startswith(home) else s


def command_family(e: dict[str, Any]) -> str | None:
    """The redacted family of a shell event: the builder's verb (``pnpm test``), never arguments.

    Matches PR #19's ``facts.command_label`` for plain verbs, so a Learn card can
    later read ``row.facts.cmd`` instead of scanning maps.
    """
    if e.get("kind") not in ("exec", "verify"):
        return None
    verb = e.get("verb") or e.get("tool")
    return str(verb)[:60] if verb else None


def event_label(e: dict[str, Any], files: list[dict[str, Any]]) -> tuple[str, str | None]:
    """``(label, target)`` for one event, from redacted map fields only."""
    kind = e.get("kind")
    target = None
    for t in e.get("targets") or []:
        if isinstance(t.get("file"), int) and 0 <= t["file"] < len(files):
            target = files[t["file"]].get("path")
        elif t.get("label"):
            target = t["label"]
        if target:
            break
    if kind in ("exec", "verify"):
        return command_family(e) or "command", None
    if kind in ("read", "search", "edit"):
        return str(e.get("tool") or kind), target
    if kind == "mcp":
        return f"{e.get('server') or 'mcp'} · {e.get('tool') or 'tool'}", None
    if kind == "subagent":
        return f"subagent {e.get('subagentType') or ''}".strip(), None
    if kind == "skill":
        return f"skill {e.get('skill') or ''}".strip(), None
    if kind == "web":
        return str(e.get("host") or "web"), None
    if kind == "llm":
        return str(e.get("model") or "model request"), None
    if kind == "user":
        return "prompt", None
    if kind == "error":
        return str(e.get("errClass") or "error"), None
    if kind == "compaction":
        return f"compaction ({e.get('trigger') or 'auto'})", None
    if kind == "delay":
        return f"delay ({e.get('cause') or 'stall'})", None
    return str(e.get("tool") or kind or "event"), None


def session_event(e: dict[str, Any], files: list[dict[str, Any]], t0: datetime | None, *, pending: bool = False) -> dict[str, Any]:
    label, target = event_label(e, files)
    at = iso(t0 + timedelta(milliseconds=int(e.get("t") or 0))) if t0 else None
    return {
        "seq": e.get("seq"),
        "at": at,
        "kind": e.get("kind"),
        "cat": e.get("cat"),
        "agent": e.get("agent") or "main",
        "label": label,
        "target": target,
        # A call with no result at the end of a live run is still running, not failed.
        "err": bool(e.get("err")) and not pending,
        "pending": pending,
        "errClass": None if pending else e.get("errClass"),
        "retryOf": e.get("retryOf"),
        "attempt": e.get("attempt"),
    }


# --------------------------------------------------------------------------- activity evidence
def _lsof(args: list[str]) -> str:
    exe = shutil.which("lsof")
    if not exe or sys.platform == "win32":
        return ""
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout


def _claude_project_dir(cwd: str) -> str:
    """Claude Code's project folder name for a working directory."""
    return "".join(c if c.isalnum() else "-" for c in cwd)


def process_evidence(paths_by_sid: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Which sessions a live agent process is writing, and how we know.

    ``paths_by_sid``: ``{sessionId: (agentTool, transcriptPath)}`` for recent runs.
    Returns ``{sessionId: evidence}``. Two kinds of evidence, both from ``lsof``:
    a process holds the transcript open, or a ``claude`` process runs in the
    project directory the transcript sits under. Absent ``lsof``, no run is ever
    called active.
    """
    if not paths_by_sid:
        return {}
    out: dict[str, str] = {}
    held = _lsof(["-Fn", "--", *[p for _, p in paths_by_sid.values() if os.path.exists(p)]])
    open_paths = {line[1:] for line in held.splitlines() if line.startswith("n")}
    for sid, (_, path) in paths_by_sid.items():
        if path in open_paths:
            out[sid] = "transcript held open by a running process"
    claude = {sid: p for sid, (a, p) in paths_by_sid.items() if a == "claude-code" and sid not in out}
    if claude:
        cwds = _lsof(["-a", "-d", "cwd", "-c", "claude", "-Fn"])
        dirs = {_claude_project_dir(line[1:]) for line in cwds.splitlines() if line.startswith("n")}
        for sid, p in claude.items():
            if Path(p).parent.name in dirs:
                out[sid] = "a claude process is running in this project"
    return out


def live_claude_sessions(homes: discover.Homes, now: datetime) -> list[dict[str, Any]]:
    """Claude Code's own live-process records, ``<home>/sessions/<pid>.json``, for processes still alive.

    Claude Code writes one per running process with its ``sessionId``, ``status`` (``busy`` working,
    ``waiting`` needs the user, ``idle`` finished its turn) and ``cwd``. Only the folder name of ``cwd`` is
    kept (a repo-like label); the path, session name and socket are never read into the snapshot.
    The file is internal to Claude Code, not a documented interface, so anything unexpected is skipped.
    """
    out = []
    for home in homes.claude_code:
        d = Path(home) / "sessions"
        try:
            files = sorted(d.glob("*.json"))
        except OSError:
            continue
        for f in files[:200]:
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            pid, sid, status = rec.get("pid"), rec.get("sessionId"), rec.get("status")
            if not isinstance(pid, int) or pid <= 1 or not isinstance(sid, str) or status not in ("busy", "waiting", "idle"):
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue  # stale record: that process is gone
            except PermissionError:
                pass  # alive, owned by someone else
            except OSError:
                continue
            def ms(v: Any) -> str | None:
                return iso(v / 1000) if isinstance(v, (int, float)) and v > 0 else None
            cwd = rec.get("cwd")
            out.append({
                "agentTool": "claude-code",
                "sessionId": f"local:claude-code:{sid}",
                "status": status,
                "pid": pid,  # lets the app bring the session's terminal forward
                "project": Path(cwd).name if isinstance(cwd, str) and cwd else None,
                "startedAt": ms(rec.get("startedAt")),
                "statusSince": ms(rec.get("statusUpdatedAt")) or ms(rec.get("updatedAt")),
            })
    out.sort(key=lambda r: ({"waiting": 0, "busy": 1, "idle": 2}[r["status"]], r["startedAt"] or ""), reverse=False)
    return out


# --------------------------------------------------------------------------- context and limits
def _codex_token_counts(path: str) -> list[tuple[str | None, dict[str, Any]]]:
    """``(timestamp, payload)`` of every ``token_count`` record in the last part of a Codex transcript."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - CODEX_TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return []
    found = []
    for raw in tail.splitlines():
        if b'"token_count"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        payload = rec.get("payload") if isinstance(rec, dict) else None
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            found.append((rec.get("timestamp"), payload))
    return found


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def codex_context(path: str) -> dict[str, Any] | None:
    """Context occupancy Codex itself reported for its latest request, or None."""
    for ts, payload in reversed(_codex_token_counts(path)):
        info = payload.get("info") or {}
        last = info.get("last_token_usage") or {}
        used, limit = _num(last.get("input_tokens")), _num(info.get("model_context_window"))
        if used is None:
            continue
        if not limit:
            return point(
                {"tokens": int(used), "limit": None, "percent": None}, "provider_reported", "agentplay_map",
                observed_at=ts, window="context", confidence="medium",
                explanation="Codex recorded the latest request's input tokens but no context window.")
        return point(
            {"tokens": int(used), "limit": int(limit), "percent": round(100 * used / limit, 1)},
            "provider_reported", "agentplay_map", observed_at=ts, window="context", confidence="high",
            explanation="Reported by Codex in its session file: input tokens of the latest request over the "
                        "model context window. Codex's own indicator may differ slightly.")
    return None


def map_context(doc: dict[str, Any], t0: datetime | None) -> dict[str, Any]:
    """Recorded prompt size of the latest main-lane request. Tokens only: no limit is recorded."""
    last_llm, last_compaction = None, None
    for e in doc.get("events") or []:
        if (e.get("agent") or "main") != "main":
            continue
        if e.get("kind") == "llm":
            last_llm = e
        elif e.get("kind") == "compaction":
            last_compaction = e
    def at(e: dict[str, Any]) -> str | None:
        return iso(t0 + timedelta(milliseconds=int(e.get("t") or 0))) if t0 else None
    if last_compaction and (last_llm is None or (last_compaction.get("seq") or 0) > (last_llm.get("seq") or 0)):
        post = _num(last_compaction.get("postTokens"))
        if post is not None:
            return point({"tokens": int(post), "limit": None, "percent": None}, "recorded", "agentplay_map",
                         observed_at=at(last_compaction), window="context", confidence="medium",
                         explanation="Recorded right after a compaction. No context limit is recorded, "
                                     "so there is no percentage.")
    if last_llm is None:
        return point(None, "unavailable", "none", window="context",
                     explanation="This session has no recorded model requests with token counts.")
    tokens = sum(int(_num(last_llm.get(k)) or 0) for k in ("in", "cw", "cr"))
    if tokens <= 0:
        return point(None, "unavailable", "none", window="context",
                     explanation="This agent does not record token counts locally.")
    return point({"tokens": tokens, "limit": None, "percent": None}, "recorded", "agentplay_map",
                 observed_at=at(last_llm), window="context", confidence="medium",
                 explanation="Prompt size of the latest recorded request (input plus cache). No context "
                             "limit is recorded, so there is no percentage. Claude Code's /context shows its own.")


_WINDOW_NAMES = {300: "five_hour", 10080: "weekly"}


def codex_limits(paths: list[tuple[str, str]], now: datetime) -> list[dict[str, Any]]:
    """Plan windows Codex wrote into its newest transcripts, as ``ProviderLimitWindow`` rows.

    ``paths``: ``(sessionId, transcriptPath)`` newest first. Plan windows are
    account-wide and shared across devices, so a report is a snapshot of the
    account at that moment, not live. A window whose reset time has passed is
    stale: usage after the reset is unknown.
    """
    reports = []
    for sid, path in paths[:4]:
        for ts, payload in reversed(_codex_token_counts(path)):
            rl = payload.get("rate_limits")
            if isinstance(rl, dict):
                reports.append((ts, sid, rl))
                break
    if not reports:
        return []
    reports.sort(key=lambda r: r[0] or "", reverse=True)
    ts, _sid, rl = reports[0]
    plans = {r[2].get("plan_type") for r in reports[:2] if r[2].get("plan_type")}
    conflict = len(plans) > 1
    out = []
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not isinstance(w, dict):
            continue
        used, minutes, resets = _num(w.get("used_percent")), _num(w.get("window_minutes")), _num(w.get("resets_at"))
        if used is None or minutes is None:
            continue
        name = _WINDOW_NAMES.get(int(minutes), f"window_{int(minutes)}m")
        reset_dt = datetime.fromtimestamp(resets, tz=UTC) if resets else None
        stale = reset_dt is not None and reset_dt <= now
        explanation = ("Reported by Codex in its session file. Plan windows are shared across devices, "
                       "so this is the account at that moment.")
        if stale:
            explanation = ("This window has reset since Codex last reported it, so current usage is unknown "
                           "until Codex runs again.")
        if conflict:
            explanation += f" Recent Codex sessions reported different plans ({', '.join(sorted(plans))}); " \
                           "they may be different accounts."
        out.append({
            "provider": "codex",
            "window": name,
            "windowMinutes": int(minutes),
            "plan": rl.get("plan_type"),
            "limitId": rl.get("limit_id"),
            "resetsAt": iso(reset_dt),
            "usageUrl": PROVIDER_USAGE_URLS["codex"],
            "conflictingPlans": sorted(plans) if conflict else None,
            "used": point({"usedPercent": used, "leftPercent": round(100 - used, 1)}, "provider_reported",
                          "agentplay_map", observed_at=ts, window=name,
                          confidence="low" if conflict or stale else "high",
                          explanation=explanation, freshness="stale" if stale else "fresh"),
        })
    if not out:
        out.append({
            "provider": "codex", "window": "none", "windowMinutes": None, "plan": rl.get("plan_type"),
            "limitId": rl.get("limit_id"), "resetsAt": None, "usageUrl": PROVIDER_USAGE_URLS["codex"],
            "used": point(None, "unavailable", "none", explanation=(
                f"Codex's latest report carried no plan windows (plan: {rl.get('plan_type') or 'unknown'}). "
                "Check usage in Codex.")),
        })
    return out


def claude_statusline(base: Path, now: datetime) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """What the optional statusline bridge saved: plan windows and per-session context, as Claude Code reported them.

    The bridge (installed only when the user turns it on) writes Claude Code's documented statusline JSON to
    ``<store>/live/claude/<session_id>.json``. Only numbers are read: ``rate_limits.five_hour`` and
    ``seven_day`` (``used_percentage``, ``resets_at``) and ``context_window`` (``used_percentage``,
    ``context_window_size``). Returns ``(limit rows, {sessionId: context point})``.
    """
    d = base / "live" / "claude"
    try:
        files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]
    except OSError:
        return [], {}
    limits: list[dict[str, Any]] = []
    contexts: dict[str, dict[str, Any]] = {}
    for f in files:
        try:
            st = f.stat()
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or now.timestamp() - st.st_mtime > 2 * 86400:
            continue
        observed = iso(st.st_mtime)
        sid = rec.get("session_id")
        cw = rec.get("context_window") if isinstance(rec.get("context_window"), dict) else {}
        pct, size = _num(cw.get("used_percentage")), _num(cw.get("context_window_size"))
        if isinstance(sid, str) and sid and pct is not None and size:
            contexts[f"local:claude-code:{sid}"] = point(
                {"tokens": int(round(size * pct / 100)), "limit": int(size), "percent": round(pct, 1)},
                "provider_reported", "approved_provider_cli", observed_at=observed, window="context",
                explanation="Reported by Claude Code's statusline: how full this session's context window is.",
                freshness="fresh" if now.timestamp() - st.st_mtime < 600 else "stale")
        rl = rec.get("rate_limits")
        if limits or not isinstance(rl, dict):
            continue
        for key, name, minutes in (("five_hour", "five_hour", 300), ("seven_day", "weekly", 10080)):
            w = rl.get(key)
            if not isinstance(w, dict):
                continue
            used, resets = _num(w.get("used_percentage")), _num(w.get("resets_at"))
            if used is None:
                continue
            reset_dt = datetime.fromtimestamp(resets, tz=UTC) if resets else None
            stale = reset_dt is not None and reset_dt <= now
            limits.append({
                "provider": "claude-code", "window": name, "windowMinutes": minutes, "plan": None, "limitId": key,
                "resetsAt": iso(reset_dt), "usageUrl": PROVIDER_USAGE_URLS["claude-code"],
                "used": point({"usedPercent": round(used, 1), "leftPercent": round(100 - used, 1)}, "provider_reported",
                              "approved_provider_cli", observed_at=observed, window=name,
                              confidence="low" if stale else "high", freshness="stale" if stale else "fresh",
                              explanation=("This window has reset since Claude Code last reported it."
                                           if stale else "Reported by Claude Code's statusline. Plan windows are "
                                           "account-wide, so this includes your other devices.")),
            })
    return limits, contexts


def unavailable_limit(provider: str, why: str) -> dict[str, Any]:
    return {"provider": provider, "window": "none", "windowMinutes": None, "plan": None, "limitId": None,
            "resetsAt": None, "usageUrl": PROVIDER_USAGE_URLS.get(provider),
            "used": point(None, "unavailable", "none", explanation=why)}


# --------------------------------------------------------------------------- alerts and patterns
def _sig(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]


def repeated_failures(doc: dict[str, Any], t0: datetime | None, pending_seq: int | None = None) -> list[dict[str, Any]]:
    """Runs of ``REPEAT_N`` failures of one command family and error class, per lane.

    Consecutive means no success of that family in between: a test that fails,
    is fixed and passes, then fails once more is not a repeat. Edits in between
    do not reset it (that is the usual fix-and-retry loop). ``no_result`` on the
    pending call of a live run is not a failure.
    """
    files = doc.get("files") or []
    streaks: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in doc.get("events") or []:
        fam = command_family(e)
        if not fam:
            continue
        lane = e.get("agent") or "main"
        if not e.get("err") or e.get("seq") == pending_seq:
            # A success ends every streak of this family in this lane, and resolves what it ended.
            for key in [k for k in streaks if k[0] == lane and k[1] == fam]:
                if key in found and found[key].get("resolvedSeq") is None:
                    found[key]["resolvedSeq"] = e.get("seq")
                del streaks[key]
            continue
        key = (lane, fam, str(e.get("errClass") or "other"))
        s = streaks[key]
        s.append(e)
        while len(s) > 1 and (int(s[-1].get("t") or 0) - int(s[0].get("t") or 0)) > REPEAT_WINDOW_MS:
            s.pop(0)
        if len(s) >= REPEAT_N and key not in found:
            first = s[0]
            found[key] = {
                "rule": "repeated_failure",
                "signature": _sig(doc["session"]["id"], lane, fam, key[2], first.get("seq")),
                "patternKey": _sig("repeated_failure", fam, key[2]),
                "sessionId": doc["session"]["id"],
                "agentTool": doc["session"].get("agentTool"),
                "command": fam,
                "errClass": key[2],
                "agent": lane,
                "count": len(s),
                "firstSeq": first.get("seq"),
                "firstAt": session_event(first, files, t0)["at"],
                "triggerSeq": e.get("seq"),
                "triggerAt": session_event(e, files, t0)["at"],
                "resolvedSeq": None,
                "evidence": point(len(s), "recorded", "agentplay_map", observed_at=session_event(e, files, t0)["at"],
                                  window="session", explanation=(
                                      f"{fam} failed {len(s)} times in a row ({key[2]}) in one run, "
                                      "with no success of it in between.")),
            }
        elif key in found and found[key].get("resolvedSeq") is None:
            found[key]["count"] = max(found[key]["count"], len(s))
    return list(found.values())


def learn_card(store: Store, rows: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """One cross-session pattern: the command family that failed most across recent runs.

    Observed counts only. It says nothing about why, or whether a change would help.
    """
    since = now - timedelta(days=LEARN_DAYS)
    recent = [r for r in rows if (_parse_iso(r.get("startedAt")) or now) >= since][:LEARN_MAX_SESSIONS]
    if len(recent) < LEARN_MIN_RUNS:
        return None
    fails: Counter = Counter()
    runs: dict[str, set[str]] = defaultdict(set)
    passed_later: dict[str, set[str]] = defaultdict(set)
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in recent:
        doc = store.get(r["sessionId"])
        if not doc:
            continue
        t0 = _parse_iso(doc["session"].get("startedAt"))
        failed_here: set[str] = set()
        for e in doc.get("events") or []:
            fam = command_family(e)
            if not fam or e.get("errClass") == "no_result":
                continue  # a call with no result is neither a failure nor a pass
            if e.get("err"):
                fails[fam] += 1
                if fam not in failed_here:
                    evidence[fam].append({"sessionId": r["sessionId"], "agentTool": r.get("agentTool"),
                                          "seq": e.get("seq"), "at": session_event(e, [], t0)["at"]})
                failed_here.add(fam)
                runs[fam].add(r["sessionId"])
            elif fam in failed_here:
                passed_later[fam].add(r["sessionId"])
    # Recurring across runs beats many failures in one run: the second is one bad afternoon.
    best = sorted((f for f, n in fails.items() if n >= LEARN_MIN_FAILS and len(runs[f]) >= LEARN_MIN_RUNS),
                  key=lambda f: (len(runs[f]), fails[f], f), reverse=True)
    if not best:
        return None
    fam = best[0]
    n, m = fails[fam], len(runs[fam])
    later = len(passed_later[fam])
    return {
        "signature": _sig("learn", fam, n, m),
        "patternKey": _sig("learn", fam),
        "kind": "observed",
        "command": fam,
        "title": f"`{fam}` failed {n} times in {m} runs.",
        "denominator": f"Across {len(recent)} recent sessions, last {LEARN_DAYS} days",
        "detail": (f"It passed later in the same run in {later} of those {m} runs. A retry that ends in a pass "
                   "is often normal work, not waste."),
        "failures": point(n, "derived", "local_index", observed_at=iso(now), window="project",
                          explanation=f"Counted from recorded {fam} events whose call failed, in the newest "
                                      f"{len(recent)} sessions of the last {LEARN_DAYS} days (at most "
                                      f"{LEARN_MAX_SESSIONS}). Calls still running are not counted."),
        "runs": m,
        "sessions": len(recent),
        "evidence": sorted(evidence[fam], key=lambda x: x["at"] or "", reverse=True)[:5],
    }


# --------------------------------------------------------------------------- runs
def _run(doc: dict[str, Any], row: dict[str, Any], now: datetime, live: dict[str, str],
         codex_ctx: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    s = doc["session"]
    sid = s["id"]
    agent = s.get("agentTool") or row.get("agentTool")
    t0 = _parse_iso(s.get("startedAt"))
    files = doc.get("files") or []
    events = doc.get("events") or []
    path = row.get("sourcePath")
    try:
        mtime = os.stat(path).st_mtime if isinstance(path, str) else None
    except OSError:
        mtime = None
    age = (now.timestamp() - mtime) if mtime else None
    # Activity: process evidence beats mtime, and mtime alone is only "recent".
    if sid in live:
        activity = point("active", "derived", "local_index", observed_at=iso(now), confidence="high",
                         explanation=live[sid].capitalize() + ".")
    elif age is not None and age <= RECENT_S:
        shared = agent in _SHARED_STORE_AGENTS
        activity = point("recent", "derived", "local_index", observed_at=iso(mtime),
                         confidence="low" if shared else "medium",
                         explanation=("The agent's shared database changed recently; it may not be this session."
                                      if shared else "The transcript changed recently. No running process was seen, "
                                      "so this is recorded activity, not proof it is running."))
    else:
        activity = point("idle", "derived", "local_index", observed_at=iso(mtime) if mtime else None,
                         confidence="medium" if mtime else "low",
                         explanation="No recent changes to the transcript." if mtime
                         else "The transcript is not on this machine (imported map, or the file moved).")
    active = activity["value"] == "active"
    # How far the map lags its transcript: it was built at `updatedAt`, the transcript has moved since.
    built = _parse_iso(row.get("updatedAt"))
    behind = int(mtime - built.timestamp()) if (mtime and built) else None
    behind = behind if behind is not None and behind > 10 else None
    visible = [e for e in events if e.get("kind") not in ("llm", "delay")]
    pending_seq = None
    trailing_no_result = bool(visible) and visible[-1].get("errClass") == "no_result"
    # A call with no result is "running" only on a live run whose map is current. On a map that lags
    # its transcript, the result has most likely landed since: neither running nor failed.
    if trailing_no_result and (active or behind is not None):
        pending_seq = visible[-1].get("seq")
    pending_is_live = active and behind is None
    tail = [session_event(e, files, t0, pending=pending_is_live and e.get("seq") == pending_seq)
            for e in visible[-TAIL_EVENTS:]]
    if pending_seq is not None and not pending_is_live and tail and tail[-1]["seq"] == pending_seq:
        tail[-1]["err"], tail[-1]["errClass"] = False, None
    last_err = next((e for e in reversed(visible) if e.get("err") and e.get("seq") != pending_seq), None)
    cats = Counter(e.get("kind") for e in events)
    pending_tool = None
    if pending_seq is not None and pending_is_live and t0:
        started = t0 + timedelta(milliseconds=int(visible[-1].get("t") or 0))
        pending_tool = {
            "event": tail[-1],
            "elapsed": point(int((now - started).total_seconds()), "derived", "agentplay_map",
                             observed_at=iso(now), window="session",
                             explanation="Time since this call started, with no result recorded yet and a "
                                         "running agent process seen."),
        }
    alerts = repeated_failures(doc, t0, pending_seq)
    outcome = s.get("outcome")
    if outcome == "unresolved_errors" and activity["value"] == "idle":
        alerts.append({
            "rule": "unresolved_end", "signature": _sig(sid, "unresolved_end"), "patternKey": _sig("unresolved_end"),
            "sessionId": sid, "agentTool": agent, "command": None, "errClass": None, "agent": "main",
            "count": sum(1 for e in events[-12:] if e.get("err")),
            "firstSeq": last_err.get("seq") if last_err else None,
            "firstAt": session_event(last_err, files, t0)["at"] if last_err else None,
            "triggerSeq": last_err.get("seq") if last_err else None,
            "triggerAt": row.get("endedAt"), "resolvedSeq": None,
            "evidence": point(outcome, "recorded", "agentplay_map", observed_at=row.get("endedAt"), window="session",
                              explanation="The run ended with two or more errors in its last 12 events."),
        })
    context = codex_ctx if (agent == "codex" and codex_ctx) else map_context(doc, t0)
    repos = row.get("repos") or []
    last_at = tail[-1]["at"] if tail else s.get("startedAt")
    return {
        "sessionId": sid,
        "agentTool": agent,
        "agentLabel": _AGENT_LABELS.get(agent or "", agent or "Unknown agent"),
        "repo": repos[0] if repos else None,
        "repoCount": len(repos),
        "startedAt": s.get("startedAt"),
        "lastEventAt": last_at,
        "lastActivityAt": iso(mtime) if mtime else None,
        "mapBehindSeconds": behind,
        "activity": activity,
        "outcome": outcome,
        "models": s.get("models") or [],
        "pathMode": s.get("pathMode") or "relative",
        "truncated": bool(s.get("truncated")),
        "imported": bool(row.get("imported")),
        "counts": {"events": len(events), "calls": (doc.get("totals") or {}).get("calls", 0),
                   "errors": (doc.get("totals") or {}).get("errors", 0),
                   "retries": (doc.get("totals") or {}).get("retries", 0),
                   "compactions": (doc.get("totals") or {}).get("compactions", 0)},
        "categories": {k: cats.get(k, 0) for k in ("read", "search", "edit", "exec", "verify", "mcp", "subagent",
                                                   "web", "error", "compaction")},
        "lastEvent": tail[-1] if tail else None,
        "lastError": session_event(last_err, files, t0) if last_err else None,
        "recentEvents": tail,
        "pendingTool": pending_tool,
        "context": context,
    }, alerts


def _sources(homes: discover.Homes, rows: list[dict[str, Any]], index_error: str | None) -> list[dict[str, Any]]:
    present = homes.present()
    counts = Counter(r.get("agentTool") for r in rows)
    latest: dict[str, str] = {}
    for r in rows:
        a = r.get("agentTool")
        if a and a not in latest:
            latest[a] = r.get("startedAt") or ""
    out = []
    for agent in discover.AGENTS:
        paths = present.get(agent) or []
        if index_error:
            status, remedy = "error", index_error
        elif not paths:
            status, remedy = "not_found", None
        else:
            unreadable = [p for p in paths if not os.access(p, os.R_OK | os.X_OK)]
            status = "error" if unreadable and len(unreadable) == len(paths) else "found"
            remedy = (f"Can't read {_home_label(unreadable[0])}. Check its permissions, or grant AgentPlay "
                      "Companion access to that folder.") if status == "error" else None
        out.append({"agent": agent, "label": _AGENT_LABELS.get(agent, agent), "status": status,
                    "paths": [_home_label(p) for p in paths], "sessionCount": counts.get(agent, 0),
                    "latestAt": latest.get(agent), "remedy": remedy})
    return out


def _usage(store: Store, now: datetime) -> dict[str, Any] | None:
    from . import usage

    try:
        return usage.report(store, now)
    except Exception:  # noqa: BLE001 - usage is extra; a bad map must not cost the whole snapshot
        return None


def snapshot(store: Store | None = None, *, now: datetime | None = None, max_runs: int = MAX_RUNS,
             homes: discover.Homes | None = None) -> dict[str, Any]:
    """The companion snapshot. Read-only."""
    t_start = time.monotonic()
    store = store or Store()
    now = now or datetime.now(UTC)
    homes = homes or discover.Homes.default()
    index_error = None
    if store.index_path.exists():
        try:
            json.loads(store.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index_error = "The AgentPlay index can't be read. Rebuild it from Settings (Diagnostics)."
    rows = [r for r in store.rows() if r.get("role") not in ("subagent", "helper") and r.get("calls")]
    # Newest activity first: a long session started yesterday and still writing outranks one started an hour ago.
    def activity_key(r: dict[str, Any]) -> str:
        return max(r.get("endedAt") or "", r.get("startedAt") or "", iso(r.get("sourceMtime")) or "")
    by_activity = sorted(rows, key=activity_key, reverse=True)
    live_sessions = live_claude_sessions(homes, now)
    status_by_sid = {r["sessionId"]: r for r in live_sessions}
    # Sessions a live agent process reports come first: they are what "now" means.
    by_activity.sort(key=lambda r: r["sessionId"] not in status_by_sid)
    top = by_activity[:max_runs]
    recent_paths = {}
    for r in top:
        p, m = r.get("sourcePath"), r.get("sourceMtime")
        if isinstance(p, str) and isinstance(m, (int, float)):
            try:
                m = os.stat(p).st_mtime
            except OSError:
                continue
            if now.timestamp() - m <= RECENT_S:
                recent_paths[r["sessionId"]] = (r.get("agentTool") or "", p)
    live = process_evidence(recent_paths)
    for r in live_sessions:
        live[r["sessionId"]] = f"Claude Code reports this session {_STATUS_WORDS[r['status']]}"
    codex_rows = [(r["sessionId"], r["sourcePath"]) for r in by_activity
                  if r.get("agentTool") == "codex" and isinstance(r.get("sourcePath"), str)]
    runs, alerts, errors = [], [], []
    for r in top:
        doc = store.get(r["sessionId"])
        if doc is None:
            errors.append({"sessionId": r["sessionId"], "agentTool": r.get("agentTool"),
                           "message": "Can't read this session's map. Rebuild it from Settings (Diagnostics)."})
            continue
        ctx = None
        if r.get("agentTool") == "codex" and isinstance(r.get("sourcePath"), str):
            ctx = codex_context(r["sourcePath"])
        run, run_alerts = _run(doc, r, now, live, ctx)
        rec = status_by_sid.get(run["sessionId"])
        run["liveStatus"] = rec["status"] if rec else None
        runs.append(run)
        alerts.extend(run_alerts)
    limits = codex_limits(codex_rows, now) if codex_rows else []
    if not limits:
        limits.append(unavailable_limit("codex", "No indexed Codex session has reported plan usage. "
                                                 "Check usage in Codex."))
    claude_limits, claude_contexts = claude_statusline(store.base, now)
    if claude_limits:
        limits[:0] = claude_limits
    else:
        limits.insert(0, unavailable_limit(
            "claude-code", "Claude Code doesn't save plan usage locally. Turn on live Claude limits in Settings "
                           "(it reads Claude Code's statusline), or check /usage in Claude Code."))
    for run in runs:
        ctx = claude_contexts.get(run["sessionId"])
        if ctx is not None:
            run["context"] = ctx
    return {
        "schema": SCHEMA,
        "schemaVersion": SCHEMA_VERSION,
        "agentplayVersion": __version__,
        "generatedAt": iso(now),
        "store": {"available": bool(rows), "sessionCount": len(rows), "indexError": index_error},
        "sources": _sources(homes, rows, index_error),
        "runs": runs,
        "unreadable": errors,
        "alerts": alerts,
        "limits": limits,
        "live": [{**r, "inStore": r["sessionId"] in store.index} for r in live_sessions],
        "learn": learn_card(store, by_activity, now),
        "usage": _usage(store, now),
        "timing": {"snapshotMs": round(1000 * (time.monotonic() - t_start))},
    }


def main(args: Any) -> int:
    if getattr(args, "refresh", False):
        from . import pipeline

        since = datetime.now(UTC) - timedelta(days=max(1, int(getattr(args, "refresh_days", 2) or 2)))
        raw = getattr(args, "agents", None)
        agents = {a.strip() for a in raw.split(",") if a.strip() in discover.AGENTS} if raw else None
        pipeline.build_all(agents=agents or None, since=since)
    snap = snapshot(max_runs=getattr(args, "max_runs", None) or MAX_RUNS)
    print(json.dumps(snap, separators=(",", ":") if not getattr(args, "pretty", False) else None,
                     indent=2 if getattr(args, "pretty", False) else None))
    return 0
