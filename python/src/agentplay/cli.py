"""Command line: ``agentplay [view|build|list|export|import|agents|fix|repos|autotune]``.

Run with no arguments to build maps for your sessions and open the viewer.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import shlex
import shutil
import sys
import threading
import warnings
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from . import __version__, discover, pipeline
from .store import Store

_TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _b(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _TTY else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _TTY else s



def _since(value: str | None) -> datetime | None:
    """Parse ``30d`` / ``12h`` / ``2026-09-01``."""
    if not value:
        return None
    m = re.fullmatch(r"(\d+)([dhw])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}[unit]
        return datetime.now(UTC) - delta
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        raise SystemExit(f"agentplay: can't read --since {value!r}; use 30d, 12h, 2w or YYYY-MM-DD") from None


def _agents(value: str | None) -> set[str] | None:
    if not value:
        return None
    wanted = {a.strip() for a in value.split(",") if a.strip()}
    unknown = wanted - set(discover.AGENTS)
    if unknown:
        raise SystemExit(f"agentplay: unknown agent(s) {sorted(unknown)}; choose from {', '.join(discover.AGENTS)}")
    return wanted


# --------------------------------------------------------------------------- build
# True while a progress bar is on screen, so log lines know to wipe it first.
_progress_live = False


def _progress(done: int, total: int, _sid: str) -> None:
    global _progress_live
    if not sys.stderr.isatty() or total == 0:
        return
    width = 24
    fill = int(width * done / total)
    sys.stderr.write(f"\r  building maps [{'█' * fill}{' ' * (width - fill)}] {done}/{total}")
    _progress_live = done < total
    if not _progress_live:
        sys.stderr.write("\r" + " " * 60 + "\r")
    sys.stderr.flush()


class _LogHandler(logging.StreamHandler):
    """Writes log records to the terminal without tearing the progress bar."""

    def emit(self, record: logging.LogRecord) -> None:
        if _progress_live:
            self.stream.write("\r\x1b[2K")  # the next progress tick redraws the bar
        super().emit(record)


class _LogFormatter(logging.Formatter):
    """``agentplay: warning: message``; ``-v`` adds the logger name."""

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 - logging API
        record.level = record.levelname.lower()
        return super().formatMessage(record)


def _log_handler(stream: TextIO, *, verbose: bool) -> logging.Handler:
    handler = _LogHandler(stream)
    fmt = "agentplay: %(level)s: %(name)s: %(message)s" if verbose else "agentplay: %(level)s: %(message)s"
    handler.setFormatter(_LogFormatter(fmt))
    return handler


def _configure_logging(verbose: bool) -> None:
    """Route every log record through one stderr handler owned by the CLI."""
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if not isinstance(h, _LogHandler)]
    root.addHandler(_log_handler(sys.stderr, verbose=verbose))
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)


_SKIP_LABELS = {
    "no_conversation": "no conversation",
    "empty_or_unreadable": "empty or unreadable",
    "detection_failed": "unrecognized format",
    "empty_events": "no events",
    "empty_store_session": "no messages",
    "no_spans": "no messages",
    "too_large": "too large",
    "imported_not_continued": "imported",
}


def _skip_breakdown(rep: pipeline.BuildReport) -> str:
    """`` (4 no conversation, 2 subagent)``, or nothing when no session was skipped."""
    if not rep.skip_reasons:
        return ""
    parts = ", ".join(
        f"{n} {_SKIP_LABELS.get(reason, reason.replace('_', ' '))}" for reason, n in rep.skip_reasons.most_common()
    )
    return f" ({parts})"


def _agent_breakdown(rep: pipeline.BuildReport) -> str:
    """``claude-code 44 · codex 5 · 3 not built``: per-agent counts that add up to ``found``.

    ``by_agent`` counts only the sessions that were built or already fresh, so
    the sessions skipped or failed this run are shown as one remainder.
    """
    parts = [f"{a or 'unknown'} {n}" for a, n in rep.by_agent.most_common()]
    rest = rep.found - sum(rep.by_agent.values())
    if rest > 0:
        parts.append(f"{rest} not built")
    return " · ".join(parts) or "none"


def cmd_build(args: argparse.Namespace, *, quiet: bool = False) -> pipeline.BuildReport:
    if getattr(args, "path_mode", None) == "hashed" and not os.environ.get("AGENTPLAY_HMAC_KEY"):
        print("agentplay: --path-mode hashed needs AGENTPLAY_HMAC_KEY; building with dirs_only instead.",
              file=sys.stderr)
    rep = pipeline.build_all(
        agents=_agents(args.agents),
        since=_since(args.since),
        rebuild=args.rebuild,
        path_mode=args.path_mode,
        limit=args.limit,
        titles=not args.no_titles,
        progress=_progress,
    )
    if getattr(args, "json", False) and not quiet:
        print(json.dumps({**rep.__dict__, "by_agent": dict(rep.by_agent)}, indent=2))
        return rep
    if quiet:
        # The caller owns the output: the viewer prints a one-liner when the
        # background scan lands.
        return rep
    print(f"Found {_b(str(rep.found))} sessions  {_dim(_agent_breakdown(rep))}")
    failed = f", {rep.failed} failed" if rep.failed else ""
    print(f"Built {rep.built} new, {rep.unchanged} unchanged, {rep.skipped} skipped{_skip_breakdown(rep)}{failed} "
          f"in {rep.seconds}s. Nothing left this machine.")
    if rep.failed and args.verbose:
        for e in rep.errors[:20]:
            print("  " + _dim(e))
    if rep.found == 0:
        print("\nNo sessions found. Looked in: " + ", ".join(
            str(p) for ps in discover.Homes.default().__dict__.values() for p in ps))
        print("Set AGENTPLAY_CLAUDE_HOMES (or _CODEX_, _CURSOR_, ...) if your agent keeps them elsewhere.")
    return rep


# --------------------------------------------------------------------------- view
def _background_scan(args: argparse.Namespace, scanning: threading.Event) -> None:
    """Scan for new sessions beside a running viewer.

    Failures are reported and dropped: a bad transcript must not take the viewer
    down with it, and everything already built stays servable. ``scanning`` is
    cleared either way, which is how the open viewer learns the scan is over.
    """
    try:
        rep = cmd_build(args, quiet=True)
    except Exception as exc:  # noqa: BLE001 - the viewer outlives a failed scan
        print(_dim(f"Scan failed: {type(exc).__name__}: {exc}"), file=sys.stderr, flush=True)
        return
    finally:
        scanning.clear()
    built = f"{rep.built} new" if rep.built else "nothing new"
    skipped = f", {rep.skipped} skipped{_skip_breakdown(rep)}" if rep.skipped else ""
    print(_dim(f"Scan done: {built} of {rep.found} sessions{skipped} in {rep.seconds}s."), flush=True)


def cmd_view(args: argparse.Namespace) -> int:
    from .server import serve, viewer_root

    # Serve first, then scan in the background. A scan can take a long time
    # (decoding database-backed agents such as Cursor dominates it), and
    # everything already built is servable immediately; the index is re-read
    # as new maps land.
    scanning = threading.Event()
    if not args.no_build:
        # Set before the server answers anything, so the viewer's first status
        # call already says a scan is under way.
        scanning.set()
    httpd = serve(args.port, scanning=scanning)
    port = httpd.server_address[1]
    if args.port and port != args.port:
        print(f"Port {args.port} is busy; using {port} instead.", file=sys.stderr, flush=True)
    url = f"http://127.0.0.1:{port}/"
    if viewer_root() is None:
        print(_dim("The web viewer is not bundled in this install (source checkout?). The API is still up; "
                   "run `make viewer` to build it."))
    # Flushed: stdout is block-buffered when it is not a tty, and "the viewer is
    # ready" is worthless to anything wrapping or piping us if it arrives at exit.
    print(f"Viewer → {_b(url)}   {_dim('(Ctrl+C to stop)')}", flush=True)
    if not args.no_build:
        print(_dim("Scanning for new sessions in the background…"), flush=True)
        threading.Thread(target=_background_scan, args=(args, scanning), daemon=True).start()
    if not args.no_open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- list
def cmd_list(args: argparse.Namespace) -> int:
    st = Store()
    agents = _agents(args.agents)
    since = _since(args.since)
    rows = []
    for row in st.rows():
        if row.get("role") in ("subagent", "helper"):
            continue
        if agents is not None and row.get("agentTool") not in agents:
            continue
        if since is not None:
            # Imported maps and discovery's zero sentinel have no transcript mtime.
            changed = row.get("sourceMtime")
            if changed is None or changed == 0:
                try:
                    started = datetime.fromisoformat(row.get("startedAt") or "")
                    changed = (started if started.tzinfo else started.replace(tzinfo=UTC)).timestamp()
                except ValueError:
                    continue
            if changed < since.timestamp():
                continue
        rows.append(row)
    rows = rows[: args.limit or 30]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        if agents is not None or since is not None:
            print("No sessions match these filters.")
        else:
            print("No maps yet. Run `agentplay build`.")
        return 0
    for r in rows:
        cost = f"${r.get('costUsd') or 0:.2f}"
        mins = (r.get("durationMs") or 0) / 60000
        title = (r.get("sessionTitle") or "")[:60]
        print(f"{(r.get('startedAt') or '')[:16].replace('T', ' ')}  {r.get('agentTool', ''):<14} "
              f"{cost:>8} {mins:6.0f}m {r.get('calls', 0):5} calls  {r.get('errors', 0):3} err  {title}")
        print("  " + _dim(r["sessionId"]))
    return 0


# --------------------------------------------------------------------------- export / agents
def cmd_export(args: argparse.Namespace) -> int:
    st = Store()
    sid = args.session
    if sid not in st.index:
        matches = [k for k in st.index if k.endswith(sid) or sid in k]
        if len(matches) != 1:
            print(f"agentplay: {'no' if not matches else 'several'} sessions match {sid!r}.", file=sys.stderr)
            return 2
        sid = matches[0]
    doc = st.get(sid)
    if doc is None:
        print("agentplay: map file missing; run `agentplay build --rebuild`.", file=sys.stderr)
        return 1
    body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    out = Path(args.output or f"{re.sub(r'[^A-Za-z0-9._-]+', '_', sid)}.json.gz")
    out.write_bytes(gzip.compress(body) if out.suffix == ".gz" else body)
    labels = (r.get("remote") or r.get("name") for r in doc.get("repos", []))
    repos = ", ".join(label for label in labels if label) or "none"
    mode = doc["session"].get("pathMode")
    hint = "" if mode in ("dirs_only", "hashed") else " Rebuild with --path-mode dirs_only to hide file names."
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB). It contains no prompt or output text, but it does "
          f"contain repo names ({repos}) and {len(doc.get('files', []))} file paths "
          f"(path mode: {mode}).{hint}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from . import builder, facts

    st = Store()
    n = 0
    for raw in args.files:
        path = Path(raw)
        try:
            data = path.read_bytes()
            doc = json.loads(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
        except (OSError, ValueError) as exc:
            print(f"agentplay: can't read {path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(doc, dict) or doc.get("schema") not in (builder.SCHEMA, builder.LEGACY_SCHEMA):
            print(f"agentplay: {path} is not a agentplay/v1 document", file=sys.stderr)
            return 1
        sid = str(doc.get("session", {}).get("id") or "")
        if not sid or not doc.get("session", {}).get("startedAt"):
            print(f"agentplay: {path} has no session.id / session.startedAt", file=sys.stderr)
            return 1
        started = doc["session"]["startedAt"]
        st.put(doc, {**builder.summary(doc), "sessionId": sid, "sessionDate": started[:10],
                     "sessionTitle": args.title or None, "contentHash": builder.content_hash(doc),
                     "imported": str(path), **facts.stamp(doc)})
        n += 1
    st.save_index()
    print(f"Imported {n} map(s).")
    return 0


def cmd_agents(_args: argparse.Namespace) -> int:
    homes = discover.Homes.default()
    present = homes.present()
    for agent in discover.AGENTS:
        paths = present.get(agent)
        mark = "●" if paths else "○"
        print(f"  {mark} {agent:<15} {', '.join(map(str, paths)) if paths else _dim('not found')}")
    return 0


def cmd_companion(args: argparse.Namespace) -> int:
    from . import companion

    return companion.main(args)


# --------------------------------------------------------------------------- parser
def _health_fixes(st: Store, days: int = 30) -> list[dict]:
    from . import autotune, health

    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    return health.fixes(autotune.root_rows(st, since=since))


def _pick_fix(st: Store, items: list[dict], target: str) -> dict | None:
    """A fix by id (the viewer shows ids, over any window), else by its 1-based number in ``items``.

    Ids come first: they are hex, so one can be all digits.
    """
    if not target:
        return None
    by_id = next((f for f in items if f["id"] == target), None)
    if by_id is None and len(target) == 12:
        by_id = next((f for f in _health_fixes(st, days=36500) if f["id"] == target), None)
    # Bounded before int(): a huge digit string would raise rather than be rejected.
    if by_id is not None or not target.isdigit() or len(target.lstrip("0")) > 6:
        return by_id
    n = int(target)
    return items[n - 1] if 1 <= n <= len(items) else None


def cmd_fix(args: argparse.Namespace) -> int:
    from . import fixes

    st = Store()
    # Undo needs only the journal: a report that fails to compute must never block a revert.
    items = [] if args.action == "undo" else _health_fixes(st)
    if args.action == "list":
        if not items:
            print("No fixes: nothing specific stands out in the last 30 days.")
        applied = {e["fixId"] for e in fixes.active(st.base)}
        for n, fx in enumerate(items, 1):
            mark = " (applied)" if fx["id"] in applied else ""
            pts = f"+{fx['points']:.1f} pts" if fx["points"] else "  0 pts"
            print(f"{n:>2}. {pts}  {fx['title']}{mark}")
            print(f"    {fx['pattern']}")
        return 0
    if args.action == "undo":
        try:
            entry = fixes.undo(st.base, args.target)
        except fixes.FixError as exc:
            print(f"agentplay: {exc.message}", file=sys.stderr)
            return 1
        print(f"Undid: {entry['title']} ({entry['repo']}, {entry['file']}).")
        return 0
    fx = _pick_fix(st, items, args.target)
    if fx is None:
        print("agentplay: give a number from `agentplay fix list`, or a fix id from the viewer.", file=sys.stderr)
        return 1
    if not fx["actions"]:
        print(f"agentplay: {fx['title']}: nothing to write; it names an integration to fix by hand.", file=sys.stderr)
        return 1
    if not 0 <= args.variant < len(fx["actions"]):
        print(f"agentplay: {fx['title']}: --variant takes 0 to {len(fx['actions']) - 1}.", file=sys.stderr)
        return 1
    action = fx["actions"][args.variant]
    try:
        entry = fixes.apply(st.base, fx, action, rel=args.to, source="cli")
    except fixes.FixError as exc:
        print(f"agentplay: {exc.message}", file=sys.stderr)
        return 1
    print(f"Wrote {entry['file']} in {entry['repo']}: {fx['title']}. Undo with `agentplay fix undo {entry['id']}`.")
    return 0


def cmd_repos(args: argparse.Namespace) -> int:
    from . import fixes

    st = Store()
    try:
        if args.action == "grant":
            print(f"agentplay can now write fixes to {fixes.grant(st.base, args.repo, args.path)}.")
        elif args.action == "revoke":
            fixes.revoke(st.base, args.repo)
            print(f"agentplay will no longer write to {args.repo}.")
        else:
            for r in fixes.status(st.base)["repos"]:
                print(f"{'●' if r['granted'] else '○'} {r['name']}  {r['root'] or '(no checkout on this machine)'}")
    except fixes.FixError as exc:
        print(f"agentplay: {exc.message}", file=sys.stderr)
        return 1
    return 0


def cmd_autotune(args: argparse.Namespace) -> int:
    from . import autotune, fixes

    st = Store()
    try:
        if args.action == "on":
            # Checked before anything changes: a typo must not leave Autotune without a schedule.
            if args.at and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", args.at):
                print("agentplay: --at takes a 24-hour time, like 09:00.", file=sys.stderr)
                return 1
            # Held from read to save: a run in between would otherwise lose its log and queue.
            with fixes.write_lock(st.base):
                for repo in args.repo or [r["name"] for r in fixes.status(st.base)["repos"] if r["granted"]]:
                    autotune.set_repo(st.base, repo, enabled=True)
                cfg = autotune.config(st.base)
                if args.at:
                    # One schedule only: a leftover SessionStart hook would run first and the cron run would skip.
                    autotune.uninstall_hook()
                    cfg["when"] = args.at
                    autotune.save(st.base, cfg)
                    print(f"Add this to your crontab (crontab -e) to run daily at {args.at}:")
                    hh, mm = args.at.split(":")
                    # cron's PATH rarely holds pipx, uv or venv bin dirs, so name the command by absolute path.
                    found = shutil.which("agentplay")
                    exe = shlex.quote(os.path.abspath(found)) if found else f"{shlex.quote(sys.executable)} -m agentplay"
                    # cron ends the command at an unescaped %, before the shell sees it.
                    cmd = autotune.HOOK_COMMAND.replace("agentplay", exe, 1).replace("%", "\\%")
                    print(f"  {int(mm)} {int(hh)} * * * {cmd}")
                else:
                    path = autotune.install_hook()
                    cfg["when"] = "session-start"
                    autotune.save(st.base, cfg)
                    print(f"Autotune runs at most once a day, when a Claude Code session starts ({path}).")
        elif args.action == "off":
            # One lock for the whole opt-out, as for `on`: a daily run cannot land halfway through it.
            with fixes.write_lock(st.base):
                for repo in args.repo or list(autotune.config(st.base)["repos"]):
                    autotune.set_repo(st.base, repo, enabled=False)
                if not args.repo:
                    autotune.uninstall_hook()
            print("Autotune is off.")
        elif args.action == "run":
            res = autotune.run(st, force=not args.daily)
            if not args.quiet:
                if res.get("skipped"):
                    print(f"Autotune: {res['skipped']}.")
                for k in ("applied", "queued", "retired"):
                    for title in res[k]:
                        print(f"{k}: {title}")
        elif args.action == "undo":
            if not args.entry:
                print("agentplay: give the entry id from `agentplay autotune status`.", file=sys.stderr)
                return 1
            entry = fixes.undo(st.base, args.entry)
            print(f"Undid: {entry['title']}.")
        else:
            s = autotune.status(st)
            print(f"Runs: {s['when']}; last run {s['lastRun'] or 'never'}; hook {'installed' if s['hookInstalled'] else 'not installed'}.")
            for r in s["repos"]:
                print(f"{'●' if r.get('enabled') and r['granted'] else '○'} {r['name']}")
            for p in s["pending"]:
                print(f"waiting: {p['title']} ({p['repo']}) - approve it in the viewer")
            for e in s["active"]:
                print(f"active: {e['title']} ({e['repo']}, {e['file']}) - undo with `agentplay autotune undo {e['id']}`")
    except fixes.FixError as exc:
        print(f"agentplay: {exc.message}", file=sys.stderr)
        return 1
    return 0


def _build_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since", help="only sessions changed since (30d, 12h, 2w, YYYY-MM-DD)")
    p.add_argument("--agents", help=f"comma-separated subset of: {', '.join(discover.AGENTS)}")
    p.add_argument("--rebuild", action="store_true", help="rebuild every selected map")
    p.add_argument("--path-mode", default="relative", choices=["relative", "dirs_only", "hashed"],
                   help="how file paths are recorded in maps (default: relative)")
    p.add_argument("--limit", type=int, help="newest N sessions only")
    p.add_argument("--no-titles", action="store_true", help="don't keep local session titles in the index")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agentplay",
        description="Replay your coding-agent sessions as a map and a timeline. Local only.",
    )
    ap.add_argument("--version", action="version", version=f"agentplay {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true")
    # Also accepted after the subcommand. SUPPRESS leaves the top-level value alone
    # when it isn't repeated there.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")

    v = sub.add_parser("view", parents=[common], help="build maps and open the viewer (default)")
    _build_flags(v)
    v.add_argument("--port", type=int, default=7317)
    v.add_argument("--no-open", action="store_true", help="don't open a browser")
    v.add_argument("--no-build", action="store_true", help="serve what's already built")
    v.set_defaults(fn=cmd_view)

    b = sub.add_parser("build", parents=[common], help="build maps for new or changed sessions")
    _build_flags(b)
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=cmd_build)

    ls = sub.add_parser("list", parents=[common], help="list built sessions")
    ls.add_argument("--agents", help=f"comma-separated subset of: {', '.join(discover.AGENTS)}")
    ls.add_argument("--since", help="only sessions changed since (30d, 12h, 2w, YYYY-MM-DD); missing modification times use start time")
    ls.add_argument("--limit", type=int)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)

    e = sub.add_parser("export", parents=[common], help="write one session's map to a file you can share or open elsewhere")
    e.add_argument("session", help="session id (or a unique suffix of one)")
    e.add_argument("-o", "--output", help="output path (.json or .json.gz)")
    e.set_defaults(fn=cmd_export)

    im = sub.add_parser("import", parents=[common], help="add map files (someone else's, or another machine's) to your local store")
    im.add_argument("files", nargs="+", help=".json or .json.gz map files")
    im.add_argument("--title", help="title to show in the session list")
    im.set_defaults(fn=cmd_import)

    a = sub.add_parser("agents", parents=[common], help="show which agents were found on this machine")
    a.set_defaults(fn=cmd_agents)

    c = sub.add_parser("companion", parents=[common], help="read-only data for the macOS companion app")
    csub = c.add_subparsers(dest="companion_cmd", required=True)
    snap = csub.add_parser("snapshot", help="print one JSON snapshot (no prompt text, paths or arguments)")
    snap.add_argument("--json", action="store_true", help="JSON output (the only format)")
    snap.add_argument("--pretty", action="store_true")
    snap.add_argument("--refresh", action="store_true", help="build recently changed sessions first")
    snap.add_argument("--agents", help="with --refresh: only these agents (comma-separated)")
    snap.add_argument("--refresh-days", type=int, default=2,
                      help="with --refresh: sessions changed in the last N days (0: every session)")
    snap.add_argument("--max-runs", type=int)
    snap.set_defaults(fn=cmd_companion)
    cfx = csub.add_parser("fix", help="preview, apply or undo a Run health fix (JSON)")
    cfx.add_argument("action", choices=["preview", "apply", "undo"])
    cfx.add_argument("id", help="fix id (preview, apply) or journal entry id (undo)")
    cfx.add_argument("--grant", action="store_true", help="with apply: allow writes to the fix's repo first")
    cfx.set_defaults(fn=cmd_companion)
    cco = csub.add_parser("coach", help="ask Claude (Haiku, your own Claude Code) for up to three suggestions (JSON)")
    cco.add_argument("--claude", required=True, help="path to the claude executable")
    cco.add_argument("--force", action="store_true", help="ask even if the last answer is under a day old")
    cco.set_defaults(fn=cmd_companion)
    cat = csub.add_parser("autotune", help="approve or dismiss a change Autotune is holding (JSON)")
    cat.add_argument("action", choices=["approve", "dismiss"])
    cat.add_argument("id", help="fix id")
    cat.set_defaults(fn=cmd_companion)

    fx = sub.add_parser("fix", parents=[common], help="list, apply or undo Run health fixes")
    fx.add_argument("action", choices=["list", "apply", "undo"])
    fx.add_argument("target", nargs="?", default="", help="fix number or id (apply), or entry id (undo)")
    fx.add_argument("--variant", type=int, default=0, help="which of a fix's actions to apply (default: the first)")
    fx.add_argument("--to", choices=["CLAUDE.md", "CLAUDE.local.md"], help="instruction file to write to")
    fx.set_defaults(fn=cmd_fix)

    rp = sub.add_parser("repos", parents=[common], help="choose which repos agentplay may write fixes to")
    rp.add_argument("action", choices=["list", "grant", "revoke"], nargs="?", default="list")
    rp.add_argument("repo", nargs="?", default="")
    rp.add_argument("--path", help="the checkout to use (default: the one builds recorded)")
    rp.set_defaults(fn=cmd_repos)

    at = sub.add_parser("autotune", parents=[common], help="apply fixes daily and retire the ones that don't help")
    at.add_argument("action", choices=["on", "off", "run", "status", "undo"], nargs="?", default="status")
    at.add_argument("entry", nargs="?", default="", help="entry id to undo (from `autotune status`)")
    at.add_argument("--repo", action="append", help="repo name (repeatable; default: every granted repo)")
    at.add_argument("--at", help="run daily at HH:MM from cron instead of on session start")
    at.add_argument("--daily", action="store_true", help="skip if it already ran today")
    at.add_argument("--quiet", action="store_true")
    at.set_defaults(fn=cmd_autotune)
    return ap


def _utf8_stdio() -> None:
    """Write UTF-8 to a pipe or file, whatever the locale says.

    On Windows a redirected stream falls back to the ANSI code page (cp1252),
    which cannot encode the marks we print (`●`, `→`) or a session title in most
    scripts, so `agentplay agents | ...` and any agent running us crashed. A
    console is left alone: it already takes Unicode.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    _utf8_stdio()
    _configure_logging(args.verbose)
    if not sys.warnoptions:
        # Python's advice for applications: warnings (from dependencies, or from
        # parsing a transcript's own code) are not the user's to act on.
        # `-W` or PYTHONWARNINGS still opts back in.
        warnings.simplefilter("ignore")
    if not getattr(args, "fn", None):
        args = ap.parse_args(["view", *(argv or sys.argv[1:])])
    try:
        rv = args.fn(args)
    except KeyboardInterrupt:
        return 130
    return 0 if rv is None or isinstance(rv, pipeline.BuildReport) else int(rv)


if __name__ == "__main__":
    sys.exit(main())
