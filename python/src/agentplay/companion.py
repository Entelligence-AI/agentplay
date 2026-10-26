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
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__, discover
from .store import INDEX_VERSION, Store

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
    existing = [p for _, p in paths_by_sid.values() if os.path.exists(p)]
    # With no file named, lsof would list every open file on the machine.
    held = _lsof(["-Fn", "--", *existing]) if existing else ""
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


_PS_START = "%a %b %d %H:%M:%S %Y"  # how ps prints lstart: "Sat Oct 24 17:20:26 2026"


def _process_starts(pids: list[int]) -> dict[int, set[str]] | None:
    """pid -> the process's start time as ``ps`` prints it, in UTC and in local time, for the processes that
    exist right now. None when ``ps`` can't be asked, so the caller falls back to the plain alive check."""
    if not pids:
        return {}
    try:
        res = subprocess.run(["/bin/ps", "-p", ",".join(str(p) for p in pids), "-o", "pid=,lstart="],
                             capture_output=True, text=True, timeout=5, check=False, env={**os.environ, "TZ": "UTC"})
    except (OSError, subprocess.SubprocessError):
        return None
    out: dict[int, set[str]] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            pid, utc = int(parts[0]), " ".join(parts[1:6])
            local = datetime.strptime(utc, _PS_START).replace(tzinfo=UTC).astimezone().strftime(_PS_START)
        except ValueError:
            continue
        out[pid] = {" ".join(utc.split()), " ".join(local.split())}
    return out


def _same_process(rec: dict[str, Any], running: dict[int, set[str]] | None) -> bool:
    """Whether the process under the record's pid is still the one that wrote it. Claude Code stores the
    process start time in ``ps`` form (in UTC); a record without one is trusted on the alive check alone."""
    if running is None:
        return True
    starts = running.get(rec["pid"])
    if starts is None:
        return False
    recorded = rec.get("procStart")
    if isinstance(recorded, str) and recorded.split():
        return " ".join(recorded.split()) in starts
    return True


def live_claude_sessions(homes: discover.Homes, now: datetime) -> list[dict[str, Any]]:
    """Claude Code's own live-process records, ``<home>/sessions/<pid>.json``, for processes still alive.

    Claude Code writes one per running process with its ``sessionId``, ``status`` (``busy`` working,
    ``waiting`` needs the user, ``idle`` finished its turn) and ``cwd``. Only the folder name of ``cwd`` is
    kept (a repo-like label); the path, session name and socket are never read into the snapshot.
    The file is internal to Claude Code, not a documented interface, so anything unexpected is skipped.
    """
    if os.name != "posix":
        return []  # the companion is macOS-only; os.kill(pid, 0) elsewhere (Windows) terminates the process
    candidates: list[dict[str, Any]] = []
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
            candidates.append(rec)
    # A pid outlives its process: after a reboot the same numbers go to Docker, a system helper, or a newer
    # agent. The record carries the process's start time, so compare it with the one running now.
    running = _process_starts([r["pid"] for r in candidates])
    out = []
    for rec in candidates:
        if not _same_process(rec, running):
            continue
        pid, sid, status = rec["pid"], rec["sessionId"], rec["status"]

        def ms(v: Any) -> str | None:
            return iso(v / 1000) if isinstance(v, (int, float)) and v > 0 else None

        cwd = rec.get("cwd")
        out.append({
            "agentTool": "claude-code",
            "sessionId": f"local:claude-code:{sid}",
            "status": status,
            "pid": pid,  # lets the app bring the session's terminal forward
            # "cli" in a terminal; "sdk-ts" and the like from an editor's agent panel, which has no statusline.
            "entrypoint": rec.get("entrypoint") if isinstance(rec.get("entrypoint"), str) else None,
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
        # The record is Codex's own and undocumented: a field of the wrong shape skips to an earlier count.
        info = payload.get("info")
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        if not isinstance(last, dict):
            continue
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


# Only account-wide windows: a model-scoped refusal ("seven_day_opus") says nothing about the general weekly window.
_RL_WINDOWS = {"five_hour": ("five_hour", 300), "seven_day": ("weekly", 10080)}


def claude_limit_hits(homes: discover.Homes, now: datetime) -> list[dict[str, Any]]:
    """Plan windows Claude Code was refused on, still before their reset, from its own transcripts.

    When a request is refused for a plan limit, Claude Code records ``rate_limit_info`` (``rateLimitType``,
    ``resetsAt``) with the error, whichever way it was started, the Agent SDK and editor panels included.
    That says a window is full and when it reopens; below the limit nothing is recorded. Only those fields
    and the line's time are read, from the last 256 KB of transcripts changed in the last eight days.
    """
    latest: dict[str, tuple[datetime, datetime]] = {}  # window -> (refused at, resets at)
    since = now.timestamp() - 8 * 86400
    for home in homes.claude_code:
        root = Path(home) / "projects"
        try:
            files = [f for f in root.rglob("*.jsonl") if f.stat().st_mtime >= since]
        except OSError:
            continue
        for f in files:
            try:
                with open(f, "rb") as fh:
                    fh.seek(max(0, f.stat().st_size - 256 * 1024))
                    tail = fh.read()
            except OSError:
                continue
            if b"rate_limit_info" not in tail:
                continue
            for line in tail.split(b"\n"):
                if b"rate_limit_info" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                info = (d.get("apiErrorParams") or {}).get("rate_limit_info") if isinstance(d, dict) else None
                at = _parse_time(d.get("timestamp")) if isinstance(d, dict) else None
                if not isinstance(info, dict) or at is None or info.get("status") != "rejected":
                    continue
                kind, resets = info.get("rateLimitType"), _num(info.get("resetsAt"))
                if kind not in _RL_WINDOWS or not resets:
                    continue
                reset_dt = datetime.fromtimestamp(resets, tz=UTC)
                name = _RL_WINDOWS[kind][0]
                if reset_dt > now and (name not in latest or at > latest[name][0]):
                    latest[name] = (at, reset_dt)
    rows = []
    for name, (at, reset_dt) in latest.items():
        minutes = 300 if name == "five_hour" else 10080
        rows.append({
            "provider": "claude-code", "window": name, "windowMinutes": minutes, "plan": None, "limitId": name,
            "resetsAt": iso(reset_dt), "usageUrl": PROVIDER_USAGE_URLS["claude-code"],
            "used": point({"usedPercent": 100.0, "leftPercent": 0.0}, "provider_reported", "agentplay_map",
                          observed_at=iso(at), window=name, freshness="fresh",
                          explanation="Claude Code was refused for this limit and recorded when it resets. "
                                      "Below the limit, it records nothing about how much is used."),
        })
    return sorted(rows, key=lambda r: r["windowMinutes"])


def _parse_time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


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
    found: list[dict[str, Any]] = []
    open_alerts: dict[tuple[str, str, str], dict[str, Any]] = {}  # the alert of each streak still running
    for e in doc.get("events") or []:
        fam = command_family(e)
        if not fam:
            continue
        if pending_seq is not None and e.get("seq") == pending_seq:
            continue  # still running: neither a failure nor a success
        lane = e.get("agent") or "main"
        if not e.get("err"):
            # A success ends every streak of this family in this lane, and resolves what it ended.
            for key in [k for k in streaks if k[0] == lane and k[1] == fam]:
                if key in open_alerts:
                    open_alerts.pop(key)["resolvedSeq"] = e.get("seq")
                del streaks[key]
            continue
        key = (lane, fam, str(e.get("errClass") or "other"))
        s = streaks[key]
        s.append(e)
        while len(s) > 1 and (int(s[-1].get("t") or 0) - int(s[0].get("t") or 0)) > REPEAT_WINDOW_MS:
            s.pop(0)
        if len(s) >= REPEAT_N and key not in open_alerts:
            first = s[0]
            open_alerts[key] = {
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
            found.append(open_alerts[key])
        elif key in open_alerts:
            open_alerts[key]["count"] = max(open_alerts[key]["count"], len(s))
    return found


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
                                          "repo": (r.get("repos") or [None])[0],
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
         codex_ctx: dict[str, Any] | None, live_status: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``live_status``: what Claude Code's own live record says (``busy``, ``waiting``, ``idle``), if it has one."""
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
    # A process Claude Code reports idle has finished its turn: nothing in it is still running, so a call
    # with no result is treated like one on a lagging map, neither running nor failed.
    pending_is_live = active and behind is None and live_status != "idle"
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


INSIGHTS_DAYS = 30
INSIGHTS_TTL_S = 900
# Bumped when the cached shape changes, so an older companion's cache is recomputed rather than read short.
INSIGHTS_CACHE_V = 2


def _health_rows(store: Store, lo: str, hi: str) -> list[dict[str, Any]]:
    """Root sessions dated ``lo``..``hi`` with their facts (stored, else computed from the map)."""
    from . import facts

    out = []
    for r in store.rows():
        day = r.get("sessionDate") or ""
        if r.get("role") in ("subagent", "helper") or not day or day < lo or day > hi:
            continue
        f = r.get("facts") if r.get("factsVersion") == facts.FACTS_VERSION else None
        if f is None:
            doc = store.get(r["sessionId"])
            f = facts.stamp(doc)["facts"] if doc else None
        if f is not None:
            out.append({**r, "facts": f})
    return out


def insights(store: Store, now: datetime, homes: discover.Homes | None = None) -> dict[str, Any] | None:
    """Run health over the last 30 days, against the 30 before, and the fixes that would raise it.

    The same report as the viewer's Run health page (``health.py``): five components and fixes that name
    the command, tool, hook or server behind them, with their evidence sessions. Cached for 15 minutes in
    ``<store>/cache/companion-insights.json``, since facts for an unstamped store take a second to compute.
    """
    from . import health

    cache = store.base / "cache" / "companion-insights.json"
    try:
        hit = json.loads(cache.read_text(encoding="utf-8"))
        made = _parse_iso(hit.get("generatedAt"))
        if made and 0 <= (now - made).total_seconds() < INSIGHTS_TTL_S and hit.get("v") == INSIGHTS_CACHE_V:
            return hit.get("insights")
    except (OSError, ValueError, AttributeError):
        pass
    day = now.astimezone().date()
    lo = (day - timedelta(days=INSIGHTS_DAYS - 1)).isoformat()
    plo = (day - timedelta(days=2 * INSIGHTS_DAYS - 1)).isoformat()
    phi = (day - timedelta(days=INSIGHTS_DAYS)).isoformat()
    rep = health.report(_health_rows(store, lo, day.isoformat()), _health_rows(store, plo, phi))
    out = {
        "days": INSIGHTS_DAYS,
        "sessions": rep["sessions"],
        "minSessions": rep["minSessions"],
        "score": rep["score"],
        "prevScore": rep["prevScore"],
        "components": [{"key": c["key"], "name": c["name"], "points": c["points"], "prevPoints": c.get("prevPoints")}
                       for c in rep["components"]],
        "applied": [{"id": e["id"], "fixId": e.get("fixId"), "repo": e.get("repo"), "file": e.get("file"),
                     "at": e.get("at"), "source": e.get("source"), "title": e.get("title")}
                    for e in _applied(store)][:8],
        "autotune": _autotune_summary(store),
        "fixes": [_fix_entry(f, homes) for f in rep["fixes"][:6]],
    }
    try:
        _atomic_write_text(cache, json.dumps({"generatedAt": iso(now), "v": INSIGHTS_CACHE_V, "insights": out},
                                             separators=(",", ":")))
    except OSError:
        pass  # a cache that can't be written only costs speed
    return out


_WRONG_EVENT = re.compile(r"expected '(\w+)' but got '(\w+)'")
_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


# Launchers a hook runs its program through; the program is the next word ("bash ~/x.sh" -> "x.sh").
_HOOK_WRAPPERS = {"env", "exec", "command", "sudo", "nohup", "time", "bash", "sh", "zsh", "fish", "python", "python3",
                  "node", "bun", "deno", "ruby", "perl", "npx", "uvx", "uv", "run", "pnpm", "npm", "yarn"}


def _hook_executable(command: Any) -> str | None:
    """The file name of the program a hook command runs ("TOKEN=x /bin/hook.sh --a" -> "hook.sh").

    Only that: environment assignments, arguments and the folder can hold secrets or private paths.
    The last command of a chain is the hook ("cd /x && ./h.sh" -> "h.sh"), launchers are skipped, and a
    script file name wins over other words ("/Users/me/my proj/h.sh" unquoted -> "h.sh", not "my").
    """
    if not isinstance(command, str):
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:  # unbalanced quotes: a whitespace split could cut a quoted folder and show part of it
        return None
    seg: list[str] = []
    skip_next = piped = False
    for t in tokens:
        if t == "|":
            piped = True  # "hook | tee log": the hook is the pipeline's first command
        elif t and set(t) <= set("&|;()"):
            seg, piped, skip_next = [], False, False  # a new command starts
        elif skip_next or piped:
            skip_next = False
        elif "<" in t or ">" in t:
            skip_next = not t.endswith("&")  # a redirection's target is a file ("2>&1" has none)
        else:
            seg.append(t)
    names = [os.path.basename(t.rstrip("/")) for t in seg if not _ENV_ASSIGN.match(t) and not t.startswith("-")]
    names = [n for n in names if n]
    script = next((n for n in names if "." in n.strip(".") and n.split(".")[0] not in _HOOK_WRAPPERS), None)
    exe = script or next((n for n in names if n not in _HOOK_WRAPPERS), None) or (names[0] if names else None)
    return exe[:160] if exe else None


def hook_cause(homes: discover.Homes, hook_name: str) -> dict[str, Any]:
    """For a failing hook ("SessionStart:startup"): the programs (file names only) configured for its event, and why the newest
    failure happened, as a fixed phrase. Claude Code records the error with the hook's output; that output is
    read to classify it and dropped, like every error text in AgentPlay."""
    event = hook_name.split(":")[0]
    commands: list[str] = []
    files: list[str] = []
    for home in homes.claude_code:
        for name in ("settings.json", "settings.local.json"):
            p = Path(home) / name
            try:
                groups = (json.loads(p.read_text(encoding="utf-8")).get("hooks") or {}).get(event) or []
            except (OSError, ValueError, AttributeError):
                continue
            for g in groups if isinstance(groups, list) else []:
                for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                    exe = _hook_executable(h.get("command")) if isinstance(h, dict) else None
                    if exe:
                        if exe not in commands:
                            commands.append(exe)
                        files.append(_home_label(p))
    cause = None
    newest = 0.0
    wrong: tuple[str, str] | None = None  # (the event it ran for, the event it answered as)
    failing: str | None = None            # the exact command, kept out of the snapshot; the fix needs it
    for home in homes.claude_code:
        try:
            recent = sorted((Path(home) / "projects").glob("*/*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:30]
        except OSError:
            continue
        for f in recent:
            try:
                with open(f, "rb") as fh:
                    head = fh.read(256 * 1024)
            except OSError:
                continue
            for line in head.split(b"\n"):
                if hook_name.encode() not in line or b"hook_" not in line:
                    continue
                try:
                    a = (json.loads(line).get("attachment") or {})
                except ValueError:
                    continue
                if a.get("hookName") != hook_name or f.stat().st_mtime <= newest:
                    continue
                newest = f.stat().st_mtime
                err = str(a.get("stderr") or "")
                failing = a.get("command") if isinstance(a.get("command"), str) else None
                m = _WRONG_EVENT.search(err)
                if m:
                    wrong = (m.group(1), m.group(2))
                    cause = (f"It was written for {m.group(2)}: when Claude Code runs it at {m.group(1)} it answers "
                             f"as the wrong event, so every run is rejected.")
                elif "not found" in err or "No such file" in err:
                    cause = "Its command can't be found."
                elif "timed out" in err.lower() or "timeout" in err.lower():
                    cause = "It runs past Claude Code's time limit."
                elif re.search(r"exit(ed)? (with )?(code|status) \d+", err):
                    cause = "It exits with an error."
                else:
                    cause = "It fails when it runs."
                break
    out: dict[str, Any] = {"commands": commands[:4], "settingsFiles": sorted(set(files)), "cause": cause, "remedy": None}
    if wrong and failing:
        action = _hook_event_action(homes, failing, ran_for=wrong[0], answers=wrong[1])
        if action:
            label = _home_label(Path(action["root"]) / action["file"])
            out["remedy"] = (
                f"Apply takes its entry out of {wrong[0]} in {label}. It keeps running at {wrong[1]}, where it is already registered."
                if action["to"] is None else
                f"Apply moves its entry from {wrong[0]} to {wrong[1]} in {label}.")
            out["_action"] = action
    return out


def _hook_event_action(homes: discover.Homes, command: str, *, ran_for: str, answers: str) -> dict[str, Any] | None:
    """The file change that stops a hook answering the wrong event: its entry leaves ``ran_for`` and, unless
    it is already there, joins ``answers``. None when no settings file of this Mac registers it at ``ran_for``.
    The full command stays inside this action; the snapshot only ever sees its name."""
    for home in homes.claude_code:
        for name in ("settings.json", "settings.local.json"):
            p = Path(home) / name
            try:
                hooks = json.loads(p.read_text(encoding="utf-8")).get("hooks") or {}
            except (OSError, ValueError, AttributeError):
                continue
            if _hook_runs(hooks, ran_for, command):
                return {"kind": "hook_event", "file": name, "root": str(p.parent), "command": command,
                        "from": ran_for, "to": None if _hook_runs(hooks, answers, command) else answers}
    return None


def _hook_runs(hooks: Any, event: str, command: str) -> bool:
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    return any(isinstance(h, dict) and h.get("command") == command
               for g in (groups if isinstance(groups, list) else []) if isinstance(g, dict)
               for h in (g.get("hooks") or []))


def _applied(store: Store) -> list[dict[str, Any]]:
    from . import fixes

    try:
        return list(reversed(fixes.active(store.base)))
    except Exception:  # noqa: BLE001 - a broken journal must not cost the insights
        return []


def _autotune_summary(store: Store) -> dict[str, Any] | None:
    from . import autotune

    try:
        st = autotune.status(store)
    except Exception:  # noqa: BLE001
        return None
    return {"enabled": any(r.get("enabled") for r in st["repos"]), "lastRun": st.get("lastRun"),
            "pending": [{"fixId": p["fixId"], "title": p["title"], "repo": p["repo"], "points": p.get("points")}
                        for p in st["pending"]][:5],
            "active": [{"id": a["id"], "fixId": a.get("fixId"), "repo": a.get("repo"), "file": a.get("file"),
                        "sessionsSince": a.get("sessionsSince"), "current": a.get("current"),
                        "baseline": a.get("baseline")} for a in st["active"]][:5]}


def _atomic_write_text(path: Path, text: str) -> None:
    from .store import _atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, text.encode("utf-8"))


def _insights(store: Store, now: datetime, homes: discover.Homes) -> dict[str, Any] | None:
    try:
        return insights(store, now, homes)
    except Exception:  # noqa: BLE001 - insights are extra; a bad map must not cost the whole snapshot
        return None


def _usage(store: Store, now: datetime, homes: discover.Homes) -> dict[str, Any] | None:
    from . import usage

    try:
        return usage.report(store, now, claude_homes=list(homes.claude_code))
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
            raw = json.loads(store.index_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("the index is not an object")
            # Valid JSON of the wrong shape would fail inside Store.rows(). An older version is not an
            # error: the store treats it as empty and the next build rewrites it.
            sessions = raw.get("sessions", {}) if raw.get("version") == INDEX_VERSION else {}
            if not isinstance(sessions, dict) or not all(isinstance(r, dict) for r in sessions.values()):
                raise ValueError("the index is not a map of session rows")
        except (OSError, ValueError):
            index_error = "The AgentPlay index can't be read. Rebuild it from Settings (Diagnostics)."
    rows = ([] if index_error else
            [r for r in store.rows() if r.get("role") not in ("subagent", "helper") and r.get("calls")])
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
        rec = status_by_sid.get(r["sessionId"])
        run, run_alerts = _run(doc, r, now, live, ctx, live_status=rec["status"] if rec else None)
        run["liveStatus"] = rec["status"] if rec else None
        runs.append(run)
        alerts.extend(run_alerts)
    limits = codex_limits(codex_rows, now) if codex_rows else []
    if not limits:
        limits.append(unavailable_limit("codex", "No indexed Codex session has reported plan usage. "
                                                 "Check usage in Codex."))
    claude_limits, claude_contexts = claude_statusline(store.base, now)
    if not claude_limits:
        claude_limits = claude_limit_hits(homes, now)
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
        "live": [{**r, "inStore": index_error is None and r["sessionId"] in store.index} for r in live_sessions],
        "learn": learn_card(store, by_activity, now),
        "usage": _usage(store, now, homes),
        "insights": _insights(store, now, homes),
        "timing": {"snapshotMs": round(1000 * (time.monotonic() - t_start))},
    }


def _fix_entry(f: dict[str, Any], homes: discover.Homes | None) -> dict[str, Any]:
    """A fix as the snapshot carries it. A failing hook brings its cause and, when the fix can be written,
    what Apply would do; the action itself (which holds the full command) never leaves this process."""
    out = {"id": f["id"], "rule": f["rule"], "repo": f.get("repo") or None, "subject": f.get("subject"),
           "component": f["component"], "title": f["title"], "pattern": f["pattern"], "effect": f["effect"],
           "points": f.get("points"), "sessions": len(f.get("sessions") or []),
           "evidence": list(f.get("sessions") or [])[:5], "applicable": bool(f.get("actions"))}
    if f["rule"] == "broken_hook" and homes is not None:
        hook = hook_cause(homes, str(f.get("subject") or ""))
        out["applicable"] = out["applicable"] or "_action" in hook
        out["hook"] = {k: v for k, v in hook.items() if not k.startswith("_")}
    return out


def _find_fix(store: Store, fix_id: str) -> dict[str, Any]:
    from . import fixes, health

    day = datetime.now(UTC).astimezone().date()
    rows = _health_rows(store, (day - timedelta(days=INSIGHTS_DAYS - 1)).isoformat(), day.isoformat())
    fx = next((f for f in health.fixes(rows) if f["id"] == fix_id), None)
    if fx is None:
        raise fixes.FixError("not_found", "That fix no longer applies to your recent sessions.")
    if fx["rule"] == "broken_hook" and not fx.get("actions"):
        # The hook's own fix lives in Claude Code's settings, not a repo: found here, written as the home "repo".
        action = hook_cause(discover.Homes.default(), str(fx.get("subject") or "")).get("_action")
        if action:
            fx["actions"] = [action]
            fx["repo"] = fixes.HOME_REPO
    return fx


def _drop_insights_cache(store: Store) -> None:
    """Forget cached insights after a change, so an applied fix stops showing as applicable."""
    try:
        (store.base / "cache" / "companion-insights.json").unlink(missing_ok=True)
    except OSError:
        pass  # a stale cache only lasts INSIGHTS_TTL_S


def fix_action(store: Store, action: str, ident: str, *, grant: bool = False) -> dict[str, Any]:
    """The menu's Apply, Preview and Undo, through the same guarded writer as the viewer (fixes.py)."""
    from . import fixes

    if action == "undo":
        e = fixes.undo(store.base, ident)
        _drop_insights_cache(store)
        return {"undone": e["id"], "file": e.get("file"), "repo": e.get("repo")}
    fx = _find_fix(store, ident)
    if not fx.get("actions"):
        raise fixes.FixError("no_action", "This one has no file change to make; it needs a look in AgentPlay.")
    act = fx["actions"][0]
    repo = str(fx.get("repo") or "")
    granted = repo in fixes.grants(store.base)
    root = fixes.grants(store.base).get(repo) or act.get("root") or fixes.known_roots(store.base).get(repo)
    if action == "preview":
        out = {"fixId": fx["id"], "title": fx["title"], "repo": repo, "granted": granted, "root": root,
               "kind": act.get("kind")}
        if granted or act.get("root"):
            out.update(fixes.preview(store.base, repo, fx, act, root=root))
        else:
            out["file"] = act.get("file")
        return out
    if not granted:
        if not grant:
            raise fixes.FixError("not_granted", f"AgentPlay may not write to {repo} yet.")
        fixes.grant(store.base, repo, root)
    e = fixes.apply(store.base, fx, act, source="button")
    _drop_insights_cache(store)
    return {"entryId": e["id"], "file": e.get("file"), "repo": repo}


def autotune_action(store: Store, action: str, fix_id: str) -> dict[str, Any]:
    from . import autotune

    if action == "approve":
        e = autotune.approve(store, fix_id)
        _drop_insights_cache(store)
        return {"entryId": e["id"], "file": e.get("file"), "repo": e.get("repo")}
    autotune.dismiss(store, fix_id)
    _drop_insights_cache(store)
    return {"dismissed": fix_id}


def main(args: Any) -> int:
    cmd = getattr(args, "companion_cmd", "snapshot")
    if cmd in ("fix", "autotune"):
        from . import fixes

        try:
            st = Store()
            out = (fix_action(st, args.action, args.id, grant=getattr(args, "grant", False)) if cmd == "fix"
                   else autotune_action(st, args.action, args.id))
            print(json.dumps({"ok": True, **out}))
            return 0
        except fixes.FixError as e:
            print(json.dumps({"ok": False, "code": getattr(e, "code", "error"), "message": str(e)}))
            return 1
    if getattr(args, "refresh", False):
        from . import pipeline

        days = getattr(args, "refresh_days", 2)
        days = 2 if days is None else int(days)
        # 0 is a full scan: usage totals cover 30 days and Show All Sessions covers everything.
        since = None if days <= 0 else datetime.now(UTC) - timedelta(days=days)
        raw = getattr(args, "agents", None)
        agents = {a.strip() for a in raw.split(",") if a.strip() in discover.AGENTS} if raw else None
        pipeline.build_all(agents=agents or None, since=since)
    snap = snapshot(max_runs=getattr(args, "max_runs", None) or MAX_RUNS)
    print(json.dumps(snap, separators=(",", ":") if not getattr(args, "pretty", False) else None,
                     indent=2 if getattr(args, "pretty", False) else None))
    return 0
