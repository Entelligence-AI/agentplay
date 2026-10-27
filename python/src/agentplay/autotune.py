"""Autotune: apply Run health fixes daily, ask before anything that grants access, retire what does not help.

Off until turned on for a repo. Each run (at most once a day unless forced):

1. Computes fixes over the last 30 days of root sessions (``health.fixes``).
2. For each repo that is both granted (``fixes.grant``) and enabled here:
   * an instruction fix is written inside the managed block of
     ``CLAUDE.local.md`` (personal, git-ignored), or of the shared
     ``CLAUDE.md`` when the repo's ``claudeMd`` setting is ``auto``;
   * a permission or hook fix is queued for the person to approve.
3. Measures every fix it applied on that fix's own metric over the sessions
   since. After 15 sessions with less than a 10% improvement the fix is
   undone, logged as retired and not applied by Autotune again. A change the
   person approved is measured but never retired.

State lives in ``~/.agentplay/autotune.json``; writes go through ``fixes.apply``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import facts, fixes, health

CONFIG = "autotune.json"
WINDOW_DAYS = 30
RETIRE_AFTER = 15
MIN_GAIN = 0.10
HOOK_COMMAND = "agentplay autotune run --daily --quiet"
LOG_MAX = 200


def config(base: Path) -> dict[str, Any]:
    path = base / CONFIG
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError):
        raw = None
    # Saving defaults over a file we could not read would erase repos, approvals and retired fixes.
    if not isinstance(raw, dict):
        raise fixes.FixError("bad_json", f"{path} could not be read as a JSON object; fix or remove it, then try again.")
    raw.setdefault("repos", {})
    raw.setdefault("pending", [])
    raw.setdefault("dismissed", [])
    raw.setdefault("retired", [])
    raw.setdefault("log", [])
    raw.setdefault("when", "manual")
    return raw


def save(base: Path, cfg: dict[str, Any]) -> None:
    cfg["log"] = cfg.get("log", [])[-LOG_MAX:]
    fixes._write_json(base / CONFIG, cfg)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(cfg: dict[str, Any], kind: str, **kw: Any) -> None:
    cfg["log"].append({"at": _now(), "kind": kind, **kw})


def set_repo(base: Path, repo: str, *, enabled: bool | None = None, claude_md: str | None = None) -> dict[str, Any]:
    with fixes.write_lock(base):
        return _set_repo(base, repo, enabled, claude_md)


def _set_repo(base: Path, repo: str, enabled: bool | None, claude_md: str | None) -> dict[str, Any]:
    cfg = config(base)
    r = cfg["repos"].setdefault(repo, {"enabled": False, "claudeMd": "ask"})
    if enabled is not None:
        r["enabled"] = bool(enabled)
    if claude_md in ("ask", "auto"):
        r["claudeMd"] = claude_md
    save(base, cfg)
    return cfg


# ------------------------------------------------------------------ sessions


def root_rows(store: Any, *, since: str | None = None) -> list[dict[str, Any]]:
    """Root sessions with their facts (computed from the map where the row predates them)."""
    out = []
    for r in store.rows():
        if r.get("role") in ("subagent", "helper") or not r.get("calls"):
            continue
        if since and (r.get("sessionDate") or "") < since:
            continue
        f = r.get("facts") if r.get("factsVersion") == facts.FACTS_VERSION else None
        if f is None:
            doc = store.get(r["sessionId"])
            f = facts.stamp(doc)["facts"] if doc is not None else None
        if f is not None:
            out.append({**r, "facts": f})
    return out


def metric(rows: list[dict[str, Any]], m: dict[str, Any] | None) -> float | None:
    """A fix's own measure over ``rows``. Lower is better except ``verified_share``."""
    if not m:
        return None
    name, verb = m.get("name"), m.get("verb")
    if name == "hook_fails":
        # Failures of this hook per session: 1.0 means it failed every session.
        return sum((r["facts"].get("hookFails") or {}).get(verb, 0) for r in rows) / len(rows) if rows else None
    if name == "mcp_fail_rate":
        ms = [(r["facts"].get("mcp") or {}).get(verb) for r in rows]
        ms = [x for x in ms if x]
        calls = sum(x.get("calls", 0) for x in ms)
        return sum(x.get("fails", 0) for x in ms) / calls if calls else None
    if name == "verified_share":
        edited = [r for r in rows if r["facts"].get("edited")]
        return sum(bool(r["facts"].get("verifyAfterLastEdit")) for r in edited) / len(edited) if edited else None
    cs = [r["facts"].get("cmd", {}).get(verb) for r in rows]
    cs = [c for c in cs if c]
    calls = sum(c.get("calls", 0) for c in cs)
    if name == "fail_rate":
        return sum(c.get("fails", 0) for c in cs) / calls if calls else None
    if name == "out_bytes":
        return sum(c.get("outBytes", 0) for c in cs) / calls if calls else None
    if name == "denials":
        # The same classes Run health counts as denied (health._cmds).
        denied = sum((c.get("byClass") or {}).get(k, 0) for c in cs for k in ("permission_denied", "blocked"))
        return denied / len(rows) if rows else None
    return None


def _improved(name: str, before: float, after: float) -> bool:
    if name == "verified_share":
        # `after >= before * 1.1` alone holds for an unchanged 0%; require a real rise.
        return after > before and (after >= before * (1 + MIN_GAIN) or after - before >= MIN_GAIN)
    return after < before and after <= before * (1 - MIN_GAIN)


def touches(repo: str | None, row: dict[str, Any]) -> bool:
    """Whether a change in ``repo`` can show in this session: a user-level change (no repo, or one in the
    Claude home such as ``~/.claude``) touches every session."""
    return not repo or repo.startswith("~") or repo in (row.get("repos") or [])


def effects(store: Any) -> list[dict[str, Any]]:
    """Each active Autotune fix with its metric before and since."""
    out = []
    for e in fixes.active(store.base):
        # A user-level change (a hook in ~/.claude, an MCP server) touches every repo.
        since = [r for r in root_rows(store, since=e["at"][:10]) if touches(e.get("repo"), r)
                 and (r.get("startedAt") or "") >= e["at"]]
        out.append({**e, "sessionsSince": len(since), "current": metric(since, e.get("metric"))})
    return out


# ------------------------------------------------------------------ run


def run(store: Any, *, force: bool = False, today: str | None = None) -> dict[str, Any]:
    # Held across the daily check, the writes and the final save, so two overlapping
    # runs (two sessions starting at once) cannot both apply the same fix.
    with fixes.write_lock(store.base):
        return _run(store, force, today)


def _run(store: Any, force: bool, today: str | None) -> dict[str, Any]:
    base = store.base
    cfg = config(base)
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    if not force and (cfg.get("lastRun") or "")[:10] == today:
        return {"skipped": "already ran today", "applied": [], "queued": [], "retired": []}
    granted = fixes.grants(base)
    enabled = {n for n, r in cfg["repos"].items() if r.get("enabled") and n in granted}
    since = (datetime.now(UTC) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    rows = root_rows(store, since=since)
    done = {e["fixId"] for e in fixes.active(base)}
    queued = {p["fixId"] for p in cfg["pending"]}
    summary: dict[str, list[Any]] = {"applied": [], "queued": [], "retired": []}

    for fx in health.fixes(rows):
        if fx["repo"] not in enabled or fx["id"] in done or fx["id"] in queued or fx["id"] in cfg["dismissed"]:
            continue
        # A fix retired for no effect would otherwise be re-applied the next day, forever.
        if fx["id"] in cfg["retired"]:
            continue
        md = next((a for a in fx["actions"] if a["kind"] == "claude_md"), None)
        repo_rows = [r for r in rows if fx["repo"] in (r.get("repos") or [])]
        if md:
            # Personal by default; the shared file only when the person said so for this repo.
            rel = "CLAUDE.md" if cfg["repos"][fx["repo"]].get("claudeMd") == "auto" else "CLAUDE.local.md"
            try:
                entry = fixes.apply(base, fx, md, rel=rel, source="autotune",
                                    baseline=metric(repo_rows, fx.get("metric")))
            except fixes.FixError as exc:
                if exc.code == "already_present":
                    continue
                _log(cfg, "error", title=fx["title"], repo=fx["repo"], detail=exc.message)
                continue
            _log(cfg, "add", title=fx["title"], repo=fx["repo"], file=rel, entry=entry["id"])
            summary["applied"].append(fx["title"])
            continue
        action = fx["actions"][0] if fx["actions"] else None
        if action is None:
            continue
        cfg["pending"].append({"fixId": fx["id"], "fix": fx, "action": action, "at": _now(),
                               "baseline": metric(repo_rows, fx.get("metric"))})
        _log(cfg, "queue", title=fx["title"], repo=fx["repo"], file=action.get("file"))
        summary["queued"].append(fx["title"])

    for e in effects(store):
        if e.get("source") != "autotune" or e.get("baseline") is None or e.get("current") is None:
            continue
        # A revoked repo, or one with Autotune switched off, is left as it is until it is back on.
        if e["repo"] not in enabled:
            continue
        if e["sessionsSince"] >= RETIRE_AFTER and not _improved(e["metric"]["name"], e["baseline"], e["current"]):
            try:
                fixes.undo(base, e["id"], reason="no measurable effect")
            except fixes.FixError as exc:
                # Retried on later runs, but logged once rather than every day the checkout stays broken.
                if not any(x["kind"] == "error" and x.get("entry") == e["id"] for x in cfg["log"]):
                    _log(cfg, "error", title=e["title"], repo=e["repo"], file=e["file"], entry=e["id"],
                         detail=exc.message)
                continue
            if e["fixId"] not in cfg["retired"]:
                cfg["retired"].append(e["fixId"])
            _log(cfg, "retire", title=e["title"], repo=e["repo"], file=e["file"],
                 detail=f"no measurable change after {e['sessionsSince']} sessions")
            summary["retired"].append(e["title"])

    cfg["lastRun"] = _now()
    save(base, cfg)
    return summary


def approve(store: Any, fix_id: str) -> dict[str, Any]:
    with fixes.write_lock(store.base):
        return _approve(store, fix_id)


def _approve(store: Any, fix_id: str) -> dict[str, Any]:
    cfg = config(store.base)
    item = next((p for p in cfg["pending"] if p["fixId"] == fix_id), None)
    if item is None:
        raise fixes.FixError("not_found", "That fix is no longer waiting.")
    # Not "autotune": the retire loop only removes what Autotune wrote unasked, never what the person approved.
    entry = fixes.apply(store.base, item["fix"], item["action"], source="approved", baseline=item.get("baseline"))
    cfg["pending"] = [p for p in cfg["pending"] if p["fixId"] != fix_id]
    _log(cfg, "approve", title=item["fix"]["title"], repo=item["fix"]["repo"], file=item["action"].get("file"),
         entry=entry["id"])
    save(store.base, cfg)
    return entry


def dismiss(store: Any, fix_id: str, *, forever: bool = False) -> None:
    with fixes.write_lock(store.base):
        _dismiss(store, fix_id, forever)


def _dismiss(store: Any, fix_id: str, forever: bool) -> None:
    cfg = config(store.base)
    cfg["pending"] = [p for p in cfg["pending"] if p["fixId"] != fix_id]
    if forever and fix_id not in cfg["dismissed"]:
        cfg["dismissed"].append(fix_id)
    save(store.base, cfg)


def status(store: Any) -> dict[str, Any]:
    cfg = config(store.base)
    return {
        "when": cfg.get("when"),
        "lastRun": cfg.get("lastRun"),
        "hookInstalled": hook_installed(),
        "repos": [
            {**r, **cfg["repos"].get(r["name"], {"enabled": False, "claudeMd": "ask"})}
            for r in fixes.status(store.base)["repos"]
        ],
        "pending": [{"fixId": p["fixId"], "title": p["fix"]["title"], "pattern": p["fix"]["pattern"],
                     "points": p["fix"]["points"], "repo": p["fix"]["repo"], "action": p["action"]} for p in cfg["pending"]],
        "log": list(reversed(cfg["log"][-50:])),
        "active": effects(store),
    }


# ------------------------------------------------------------------ SessionStart hook


def _claude_settings() -> Path:
    # Resolved so a write replaces a dotfiles-managed symlink's target, not the symlink itself.
    return (Path.home() / ".claude" / "settings.json").resolve()


def hook_installed() -> bool:
    try:
        data = json.loads(_claude_settings().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    try:
        groups = fixes.hook_groups(data, str(_claude_settings()), "SessionStart", create=False)
    except fixes.FixError:
        return False  # a hand-broken file holds no hook of ours; install_hook reports the shape
    return _has_hook(groups)

def _has_hook(groups: list[Any]) -> bool:
    return any(h.get("command") == HOOK_COMMAND for g in groups for h in g.get("hooks", []))


def install_hook() -> Path:
    """Add the daily run to Claude Code's user settings as a SessionStart hook (merged, idempotent)."""
    path = _claude_settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as exc:
        raise fixes.FixError("bad_json", f"{path} is not valid JSON; fix it by hand first.") from exc
    # Claude Code's own settings: an unexpected shape is refused, never overwritten.
    groups = fixes.hook_groups(data, str(path), "SessionStart")
    if not _has_hook(groups):
        groups.append({"hooks": [{"type": "command", "command": HOOK_COMMAND}]})
        path.parent.mkdir(parents=True, exist_ok=True)
        fixes._atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), private=False)
    return path


def uninstall_hook() -> None:
    path = _claude_settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        groups = fixes.hook_groups(data, str(path), "SessionStart", create=False)
    except fixes.FixError:
        return  # never rewrite a file whose shape we do not understand
    kept = fixes.drop_hook(groups, HOOK_COMMAND)
    if kept != groups:
        if kept:
            data["hooks"]["SessionStart"] = kept
        else:
            del data["hooks"]["SessionStart"]
        fixes._atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), private=False)
