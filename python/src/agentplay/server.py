"""Local viewer server: the bundled web viewer plus a small read-only JSON API.

Binds to 127.0.0.1 only. Serves the static viewer from the package's
``viewer/`` directory and these endpoints over the local store:

``GET /api/status``            ``{available, mapCount, latestSessionDate, scanning}``
``GET /api/sessions``          paged session rows (``from``, ``to``, ``search``, ``pageSize``, ``cursor``);
                               ``search`` also matches files, commands, MCP servers and more (see ``search.py``)
``GET /api/map``               one entry per ``session_id`` param, each with a ``url``
``GET /api/map-doc?id=...``    the map document itself
``GET /api/stats``             session facts for the Insights view (``from``, ``to``: inclusive ``YYYY-MM-DD``)
``GET /api/account``           whether to offer the email signup card (see ``account.py``)
``GET /api/prompt-images?id=...``  per-turn metadata for images pasted into Claude Code prompts (this machine only)
``GET /api/prompt-image?id=...&turn=...&i=...``  one such image, read from the transcript on demand (this machine only)
``GET /api/hook-detail?id=...&uuid=...``  the message and command of one hook failure, read from the transcript on demand (this machine only)
``GET /api/health``            Run health score, components and fixes for a window (``from``, ``to``)
``GET /api/fix-preview``       a fix's file before and after (``fix``, ``i``: action index, ``file``, ``from``, ``to``)
``GET /api/autotune``          Autotune settings, repos, the approval queue and its log
``POST /api/account``          ``{"action": "signup", "email": ...}``, ``{"action": "skip"}``, ``{"action": "disconnect"}``
``POST /api/fix``              ``{"action": "apply", "fix", "i", "file"?, "hash"?, "from"?, "to"?}``, ``{"action": "undo", "entry"}``
``POST /api/repos``            ``{"action": "grant" | "revoke", "repo"}``
``POST /api/autotune``         ``{"action": "enable" | "disable" | "shared" | "approve" | "dismiss" | "never" | "run", ...}``

Pasted images are read from the original transcript when asked for and never
stored in a map, the index or an export; see ``prompt_images.py``. Hook failure
messages and commands are read the same way; see ``hook_detail.py``.

Writes stay under ``~/.agentplay`` except fixes, which ``fixes.py`` writes only
into repos the person granted, only to four agent files, and journals for undo.
The only thing that can leave the machine is an email the user typed into the
signup card.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from . import account, autotune, facts, fixes, health, hook_detail, prompt_images
from . import search as search_index
from .store import Store

MAX_PAGE = 200
DEFAULT_PAGE = 50
MAX_BODY = 4096
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
# Extra origins allowed to post (comma-separated), e.g. the viewer's Vite dev
# server at http://localhost:5173. Nothing else but this server's own origin.
DEV_ORIGINS_ENV = "AGENTPLAY_DEV_ORIGINS"
_FETCH_SITES = {"same-origin", "none"}
_INLINE_SCRIPT_RE = re.compile(rb"<script>(.*?)</script>", re.S)


def viewer_root() -> Path | None:
    """Directory holding the built viewer (``index.html``), or None if not bundled."""
    try:
        root = Path(str(resources.files("agentplay") / "viewer"))
    except (ModuleNotFoundError, TypeError):
        return None
    return root if (root / "index.html").is_file() else None


def _cursor_encode(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode().rstrip("=")


def _cursor_decode(raw: str | None) -> int | None:
    """The offset in a cursor from ``_cursor_encode``, 0 for none, None if it is not one."""
    if not raw:
        return 0
    try:
        pad = "=" * (-len(raw) % 4)
        return max(0, int(json.loads(base64.urlsafe_b64decode(raw + pad))["o"]))
    except (ValueError, KeyError, TypeError, OverflowError):  # OverflowError: {"o": 1e999}
        return None


_UUID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")


def _non_negative_int(raw: str) -> int | None:
    """``raw`` as an int if it is 1-9 plain ASCII digits, else None (no sign, space or underscore)."""
    return int(raw) if 0 < len(raw) <= 9 and raw.isascii() and raw.isdigit() else None


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in row.items()
        if k not in ("key", "sourcePath", "sourceMtime", "signals", "imported", "taskTitles", "facts", "factsVersion")
    }


# What `/api/stats` sends per session besides its facts: enough to filter and
# group (window, agent, repo, model) and to link back to the session.
_STATS_FIELDS = ("sessionId", "sessionDate", "startedAt", "durationMs", "agentTool", "models", "repos")


def _is_loopback(address: Any) -> bool:
    """Whether a peer address belongs to this machine's loopback interface.

    Fails closed: anything unparseable is treated as remote.

    Args:
        address: a ``(host, port, ...)`` tuple as ``client_address`` gives it,
            or a bare host string.

    Returns:
        True for ``127.0.0.0/8``, ``::1`` and IPv4-mapped forms of them.
    """
    host = address[0] if isinstance(address, tuple) and address else address
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback


def _local_origin(value: str | None) -> bool:
    """Whether a ``Host`` value names this machine (any port).

    Any port, because the viewer's dev server proxies to this one with its own
    Host. ``Origin`` is held to a stricter rule: see ``_Handler._allowed_origin``.
    """
    if not value:
        return False
    try:
        parts = urlsplit(value if "//" in value else f"//{value}")
        host, _ = parts.hostname or "", parts.port  # `.port` raises on a bad or out-of-range port
    except ValueError:
        return False
    return (f"[{host}]" if ":" in host else host) in _LOCAL_HOSTS


def _with_task_titles(doc: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Label a served map's tasks from the local index.

    Task labels are prompt text, so they live in the index and are merged here, at
    serve time, rather than being built into the map. `store.get` keeps returning a
    map with no prompt text in it, which is what `agentplay export` writes and what
    the format contract promises — see `discover.local_task_titles`.

    Callers must merge only for a loopback peer (`_is_loopback`): the API is
    unauthenticated, so on a server bound to a non-loopback host this would hand
    the user's prompt text to anyone who can reach the port.

    Only fills a task whose name is empty, so a name the builder derived from a
    named span still wins.
    """
    titles = row.get("taskTitles")
    tasks = doc.get("tasks")
    if not titles or not tasks:
        return doc
    out = []
    for t in tasks:
        turn = t.get("firstTurn")
        if not t.get("name") and isinstance(turn, int) and 1 <= turn <= len(titles):
            label = titles[turn - 1]
            if label:
                t = {**t, "name": label}
        out.append(t)
    return {**doc, "tasks": out}


def _page_csp(html: bytes) -> str:
    """The viewer's Content-Security-Policy, allowing exactly the inline scripts ``html`` carries.

    The only inline script is the theme-before-first-paint snippet in
    ``index.html``; hashing it at serve time keeps the policy right when it changes.
    Styles stay ``'unsafe-inline'``: the UI libraries set ``style`` attributes and
    inject positioning styles. Fonts are bundled, some as ``data:`` URLs in the CSS.
    """
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m).digest()).decode() + "'"
        for m in _INLINE_SCRIPT_RE.findall(html)
    )
    return (
        f"default-src 'self'; script-src 'self' {hashes}".rstrip() + "; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


class _Server(ThreadingHTTPServer):
    # The default backlog of 5 resets connections when the viewer fires a burst
    # of requests (about ten at once on a cold load).
    request_queue_size = 128


class _Handler(BaseHTTPRequestHandler):
    server_version = "agentplay"
    # A client that stalls mid-request gives up its thread instead of holding it forever.
    timeout = 15
    store: Store
    static: Path | None
    # Set while `agentplay view` is scanning in the background, so the viewer
    # can tell "no sessions yet" from "no sessions".
    scanning: threading.Event | None = None
    lock = threading.Lock()
    # Facts for rows written before facts existed (or by an older FACTS_VERSION),
    # computed from the stored map on first read. Kept in memory, keyed by the
    # map's content hash: the server never writes the index, which a background
    # scan may be saving at the same moment. The next build persists them.
    facts_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    # Uncapped `files` for sessions past facts.MAX_FACT_FILES, keyed like `facts_cache`.
    files_cache: dict[tuple[str, str], dict[str, Any]] = {}
    # search.json as last read (by mtime and size) and postings computed for
    # sessions it does not cover yet. In memory only, like `facts_cache`.
    search_file: tuple[Any, dict[str, Any]] = (None, {})
    search_mem: dict[tuple[str, str], dict[str, Any]] = {}

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        return

    # ------------------------------------------------------------------ io
    def end_headers(self) -> None:
        # On every response, http.server's own error pages included.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _json(self, body: Any, status: int = 200) -> None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, content_type: str, data: bytes) -> None:
        """Send a user's image: private, and inert if a browser is steered into rendering it as a page."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json({"success": False, "code": code, "error": message}, status)

    def _static(self, path: str) -> None:
        if self.static is None:
            self._error(404, "not_found", "The viewer is not bundled in this install. Build it with `make viewer`.")
            return
        rel = path.lstrip("/") or "index.html"
        root = self.static.resolve()
        try:
            target = (root / rel).resolve()
            found = target.is_relative_to(root) and target.is_file()
        except (OSError, ValueError):  # e.g. a name too long for the filesystem
            found = False
        if not found:
            target = root / "index.html"  # SPA fallback
        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Only an HTML page runs anything, so only it needs the script hashes.
        html = data if target.suffix == ".html" else b""
        self.send_header("Content-Security-Policy", _page_csp(html))
        self.end_headers()
        self.wfile.write(data)

    def _from_this_machine(self) -> bool:
        """Whether a read may include the user's own data (task labels, the email).

        A loopback peer is not enough: a DNS-rebinding page reaches 127.0.0.1 under
        its own hostname and reads the answer as same-origin. So the Host must name
        this machine, and so must the Origin whenever a browser sends one.
        """
        origin = self.headers.get("Origin")
        return (_is_loopback(self.client_address) and _local_origin(self.headers.get("Host"))
                and (origin is None or self._allowed_origin(origin)))

    def _allowed_origin(self, origin: str | None) -> bool:
        """Whether ``origin`` is this server's own, or one listed in ``AGENTPLAY_DEV_ORIGINS``.

        Not any local port: another local web app (or anything a page can make
        listen on localhost) would otherwise be trusted too.
        """
        if not origin:
            return False
        port = self.server.server_address[1]
        allowed = {f"http://{h}:{port}" for h in _LOCAL_HOSTS}
        allowed.update(o.strip().rstrip("/").lower() for o in os.environ.get(DEV_ORIGINS_ENV, "").split(","))
        allowed.discard("")
        return origin.strip().lower() in allowed

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        # Every read is the user's own data (session titles are prompt text, maps
        # name private repos and files), so a DNS-rebinding page gets nothing.
        if not self._from_this_machine():
            return self._error(403, "forbidden", "Only the local viewer can read this.")
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        # A browser labels every fetch with where it came from. The viewer's own
        # calls are same-origin; anything else (an <img> or <script> on another
        # site) gets nothing. Pages stay open to a typed URL or a link.
        site = self.headers.get("Sec-Fetch-Site")
        if url.path.startswith("/api/") and site is not None and site.lower() not in _FETCH_SITES:
            return self._error(403, "forbidden", "Only the local viewer can read this.")
        if url.path.startswith("/api/"):
            # `agentplay view` scans in the background, so the index can change
            # while we are serving. Cheap: one stat unless it actually moved.
            with self.lock:
                self.store.reload_if_changed()
        try:
            if url.path == "/api/status":
                return self._status()
            if url.path == "/api/sessions":
                return self._sessions(q)
            if url.path == "/api/map":
                return self._maps(q)
            if url.path == "/api/map-doc":
                return self._map_doc(q)
            if url.path == "/api/stats":
                return self._stats(q)
            if url.path == "/api/health":
                return self._health(q)
            if url.path == "/api/fix-preview":
                return self._fix_preview(q)
            if url.path == "/api/autotune":
                return self._json({"success": True, **autotune.status(self.store)})
            if url.path == "/api/prompt-images":
                return self._prompt_images(q)
            if url.path == "/api/prompt-image":
                return self._prompt_image(q)
            if url.path == "/api/hook-detail":
                return self._hook_detail(q)
            if url.path == "/api/account":
                # The email is the user's: only the viewer on this machine sees it,
                # and only it is ever offered the card.
                if not self._from_this_machine():
                    return self._json({"success": True, "status": "none", "email": None, "enabled": False})
                return self._json({"success": True, **account.public_state()})
            if url.path.startswith("/api/"):
                return self._error(404, "not_found", "Unknown endpoint.")
            return self._static(url.path)
        except BrokenPipeError:
            return None
        except Exception:  # noqa: BLE001 - never leak a traceback to the page
            return self._error(500, "internal_error", "Something went wrong reading your local sessions.")

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        url = urlsplit(self.path)
        if url.path not in ("/api/account", "/api/fix", "/api/repos", "/api/autotune"):
            return self._error(404, "not_found", "Unknown endpoint.")
        # Every write this server takes. A browser on another site can reach
        # 127.0.0.1, so: same machine, a local Origin and Host (no DNS rebinding),
        # and a JSON body, which a cross-site form can't send without a preflight
        # this server never answers.
        if not (_is_loopback(self.client_address) and self._allowed_origin(self.headers.get("Origin"))
                and _local_origin(self.headers.get("Host"))):
            return self._error(403, "forbidden", "Only the local viewer can do this.")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._error(415, "unsupported_media_type", "Send JSON.")
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if not 0 < size <= MAX_BODY:
            return self._error(400, "invalid_input", "Bad request body.")
        try:
            body = json.loads(self.rfile.read(size))
        except ValueError:
            return self._error(400, "invalid_input", "Bad request body.")
        action = body.get("action") if isinstance(body, dict) else None
        if url.path != "/api/account":
            try:
                # Shared with the CLI and the SessionStart hook (a file lock, not `self.lock`:
                # a write reads the index through `self.lock` itself).
                with fixes.write_lock(self.store.base):
                    return self._write(url.path, str(action or ""), body)
            except fixes.FixError as exc:
                return self._error(409, exc.code, exc.message)
            except BrokenPipeError:
                return None
            except Exception:  # noqa: BLE001 - never leak a traceback to the page
                return self._error(500, "internal_error", "Something went wrong writing that.")
        try:
            if action == "skip":
                return self._json({"success": True, **account.skip()})
            if action == "disconnect":
                return self._json({"success": True, **account.disconnect()})
            if action == "signup":
                email = str(body.get("email") or "").strip()
                if not account.valid_email(email):
                    return self._error(400, "invalid_email", "That doesn't look like an email address.")
                try:
                    return self._json({"success": True, **account.sign_up(email)})
                except (OSError, ValueError):
                    return self._error(502, "unreachable", "We couldn't reach Entelligence. Check your connection and try again.")
            return self._error(400, "invalid_input", "Unknown action.")
        except BrokenPipeError:
            return None
        except Exception:  # noqa: BLE001 - never leak a traceback to the page
            return self._error(500, "internal_error", "Something went wrong saving that.")

    # ------------------------------------------------------------------ run health and fixes
    def _window(self, lo: str, hi: str) -> list[dict[str, Any]]:
        out = []
        for r in self._rows():
            day = r.get("sessionDate") or ""
            if (lo and day < lo) or (hi and day and day > hi):
                continue
            with self.lock:
                f = self._facts(r)
            if f is not None:
                out.append({**r, "facts": f})
        return out

    @staticmethod
    def _prev_range(lo: str, hi: str) -> tuple[str, str] | None:
        from datetime import date, timedelta
        try:
            a, b = date.fromisoformat(lo), date.fromisoformat(hi)
        except ValueError:
            return None
        span = (b - a).days + 1
        return (a - timedelta(days=span)).isoformat(), (a - timedelta(days=1)).isoformat()

    def _health(self, q: dict[str, list[str]]) -> None:
        lo, hi = (q.get("from") or [""])[0], (q.get("to") or [""])[0]
        rows = self._window(lo, hi)
        prev = self._prev_range(lo, hi)
        report = health.report(rows, self._window(*prev) if prev else None)
        ids = {w["sessionId"] for c in report["components"] for w in c["worst"]}
        ids |= {sid for fx in report["fixes"] for sid in fx["sessions"][:5]}
        titles = {r["sessionId"]: r.get("sessionTitle") for r in rows if r["sessionId"] in ids}
        st = fixes.status(self.store.base)
        self._json({"success": True, **report, "titles": titles, "repos": st["repos"],
                    "applied": [{"id": e["id"], "fixId": e["fixId"], "file": e["file"], "kind": e["action"].get("kind")}
                                for e in st["applied"]]})

    def _find_fix(self, fix_id: str, lo: str, hi: str) -> dict[str, Any]:
        fx = next((f for f in health.fixes(self._window(lo, hi)) if f["id"] == fix_id), None)
        if fx is None:
            raise fixes.FixError("not_found", "That fix no longer applies to these sessions.")
        return fx

    def _fix_preview(self, q: dict[str, list[str]]) -> None:
        try:
            fx = self._find_fix((q.get("fix") or [""])[0], (q.get("from") or [""])[0], (q.get("to") or [""])[0])
            raw = (q.get("i") or ["0"])[0]
            i = 0 if raw == "0" else _non_negative_int(raw)
            if i is None or i >= len(fx["actions"]):
                return self._error(400, "invalid_input", "No such action.")
            rel = (q.get("file") or [None])[0]
            self._json({"success": True, **fixes.preview(self.store.base, fx["repo"], fx, fx["actions"][i], rel)})
        except fixes.FixError as exc:
            self._error(409, exc.code, exc.message)

    def _write(self, path: str, action: str, body: dict[str, Any]) -> None:
        base = self.store.base
        if path == "/api/repos":
            repo = str(body.get("repo") or "")
            if action == "grant":
                return self._json({"success": True, "root": fixes.grant(base, repo)})
            if action == "revoke":
                fixes.revoke(base, repo)
                return self._json({"success": True})
        elif path == "/api/fix":
            if action == "apply":
                fx = self._find_fix(str(body.get("fix") or ""), str(body.get("from") or ""), str(body.get("to") or ""))
                i = body.get("i", 0)
                if not isinstance(i, int) or not 0 <= i < len(fx["actions"]):
                    return self._error(400, "invalid_input", "No such action.")
                rel = body.get("file") if isinstance(body.get("file"), str) else None
                expect = body.get("hash") if isinstance(body.get("hash"), str) else None
                entry = fixes.apply(base, fx, fx["actions"][i], rel=rel, expect_hash=expect)
                return self._json({"success": True, "entry": entry})
            if action == "undo":
                return self._json({"success": True, "entry": fixes.undo(base, str(body.get("entry") or ""))})
        elif path == "/api/autotune":
            repo = str(body.get("repo") or "")
            fix_id = str(body.get("fixId") or "")
            if action in ("enable", "disable"):
                autotune.set_repo(base, repo, enabled=action == "enable")
            elif action == "shared":
                autotune.set_repo(base, repo, claude_md="auto" if body.get("value") else "ask")
            elif action == "approve":
                autotune.approve(self.store, fix_id)
            elif action in ("dismiss", "never"):
                autotune.dismiss(self.store, fix_id, forever=action == "never")
            elif action == "run":
                return self._json({"success": True, **autotune.run(self.store, force=True)})
            else:
                return self._error(400, "invalid_input", "Unknown action.")
            return self._json({"success": True, **autotune.status(self.store)})
        return self._error(400, "invalid_input", "Unknown action.")

    def _rows(self) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.store.rows() if r.get("role") not in ("subagent", "helper") and r.get("calls")]

    def _status(self) -> None:
        rows = self._rows()
        latest = max((r.get("sessionDate") or "" for r in rows), default="") or None
        scanning = bool(self.scanning and self.scanning.is_set())
        self._json({"success": True, "available": bool(rows), "mapCount": len(rows), "latestSessionDate": latest,
                    "scanning": scanning})

    def _search_terms(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        path = self.store.base / "search.json"
        try:
            st = path.stat()
            stamp: Any = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        with self.lock:
            if self.search_file[0] != stamp:
                type(self).search_file = (stamp, search_index.load(path) if stamp else {})
            indexed = self.search_file[1]
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            sid = str(r.get("sessionId") or "")
            h = str(r.get("contentHash") or "")
            entry = indexed.get(sid)
            if isinstance(entry, dict) and entry.get("hash") == h and isinstance(entry.get("terms"), dict):
                out[sid] = entry["terms"]
                continue
            key = (sid, h)
            if key not in self.search_mem:
                doc = self.store.get(sid)
                try:
                    self.search_mem[key] = search_index.postings(doc) if doc is not None else {}
                except (KeyError, TypeError, ValueError, AttributeError):
                    self.search_mem[key] = {}
            out[sid] = self.search_mem[key]
        return out

    def _sessions(self, q: dict[str, list[str]]) -> None:
        rows = self._rows()
        lo = (q.get("from") or [""])[0]
        hi = (q.get("to") or [""])[0]
        search = (q.get("search") or [""])[0].strip().lower()
        try:
            size = min(MAX_PAGE, max(1, int((q.get("pageSize") or [DEFAULT_PAGE])[0])))
        except ValueError:
            size = DEFAULT_PAGE
        in_window = [
            r for r in rows
            if not ((lo and (r.get("sessionDate") or "") and r["sessionDate"] < lo)
                    or (hi and (r.get("sessionDate") or "") and r["sessionDate"] > hi))
        ]
        hits: dict[str, dict[str, Any]] = {}
        note = None
        if search:
            terms = self._search_terms(in_window)
            hits = search_index.query(terms, search)
            if not hits and " " in search:
                fallback = search_index.argument_fallback(terms, search)
                if fallback:
                    prefix, hits = fallback
                    note = {"code": "arguments", "command": prefix}
        out = []
        for r in in_window:
            if search:
                hay = " ".join(
                    [str(r.get("sessionTitle") or ""), str(r.get("sessionId") or ""), str(r.get("agentTool") or ""),
                     " ".join(map(str, r.get("repos") or [])), " ".join(map(str, r.get("models") or []))]
                ).lower()
                if search not in hay and r.get("sessionId") not in hits:
                    continue
            out.append(r)
        start = _cursor_decode((q.get("cursor") or [None])[0])
        if start is None:
            return self._error(400, "invalid_input", "That page cursor is not valid.")
        page = out[start:start + size]
        more = start + size < len(out)
        self._json({
            "success": True,
            "sessions": [{**_public_row(r), **({"search": hits[r["sessionId"]]} if r.get("sessionId") in hits else {})}
                         for r in page],
            "searchNote": note,
            "nextCursor": _cursor_encode(start + size) if more else None,
            "hasMore": more,
        })

    def _facts(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if row.get("factsVersion") == facts.FACTS_VERSION:
            return row.get("facts")
        sid = str(row.get("sessionId") or "")
        key = (sid, str(row.get("contentHash") or ""))
        if key not in self.facts_cache:
            doc = self.store.get(sid)
            self.facts_cache[key] = facts.stamp(doc)["facts"] if doc is not None else None
        return self.facts_cache[key]

    def _all_files(self, row: dict[str, Any], f: dict[str, Any]) -> dict[str, Any]:
        """A capped session's full ``files`` (stored facts keep ``facts.MAX_FACT_FILES``).

        The heatmap sums files across sessions, so a path outside each session's
        top files can still be among the busiest overall. Rare (sessions past the
        cap), read from the stored map once per content hash.
        """
        key = (str(row.get("sessionId") or ""), str(row.get("contentHash") or ""))
        if key not in self.files_cache:
            doc = self.store.get(key[0])
            self.files_cache[key] = facts.file_counts(doc) if doc is not None else f.get("files") or {}
        return self.files_cache[key]

    def _stats(self, q: dict[str, list[str]]) -> None:
        lo = (q.get("from") or [""])[0]
        hi = (q.get("to") or [""])[0]
        out = []
        for r in self._rows():
            day = r.get("sessionDate") or ""
            if (lo and day < lo) or (hi and day and day > hi):
                continue
            with self.lock:
                f = self._facts(r)
                if f is not None and f.get("filesRest"):
                    # The full list replaces the capped one, so its remainder would double-count.
                    f = {**{k: v for k, v in f.items() if k != "filesRest"}, "files": self._all_files(r, f)}
            if f is None:
                continue
            out.append({**{k: r.get(k) for k in _STATS_FIELDS}, "facts": f})
        self._json({"success": True, "factsVersion": facts.FACTS_VERSION, "sessions": out})

    def _maps(self, q: dict[str, list[str]]) -> None:
        ids = (q.get("session_id") or [])[:20]
        if not ids:
            return self._error(400, "invalid_input", "Pass at least one session_id.")
        index = self.store.index
        maps = []
        for sid in ids:
            row = index.get(sid)
            if row is None:
                maps.append({"sessionId": sid, "status": "not_found"})
                continue
            maps.append({
                "sessionId": sid,
                "url": f"/api/map-doc?id={quote(sid, safe='')}",
                "schemaVersion": 1,
                "contentHash": row.get("contentHash") or sid,
                "bytesGz": None,
            })
        self._json({"success": True, "expiresIn": 3600, "maps": maps})

    def _map_doc(self, q: dict[str, list[str]]) -> None:
        sid = (q.get("id") or [""])[0]
        doc = self.store.get(sid)
        if doc is None:
            return self._error(404, "not_found", "We couldn't find that session.")
        # Task labels are prompt text and the API is unauthenticated: a network
        # client gets the stored map, which carries none.
        if self._from_this_machine():
            doc = _with_task_titles(doc, self.store.index.get(sid) or {})
        self._json(doc)

    def _image_source(self, sid: str) -> str | None:
        """Transcript path for a session whose transcript extras may be read, else None after replying.

        Pasted images and hook failure details are the user's own data, so only
        the viewer on this machine gets them. Only a Claude Code session built from a local transcript has any:
        the path comes from the index row alone, never from the request.
        """
        if not self._from_this_machine():
            self._error(403, "forbidden", "Only the local viewer can read this.")
            return None
        if not sid:
            self._error(400, "invalid_input", "Missing session id.")
            return None
        row = self.store.index.get(sid) or {}
        path = row.get("sourcePath")
        if (row.get("imported") or row.get("agentTool") != "claude-code"
                or not isinstance(path, str) or not path.endswith(".jsonl")):
            self._error(404, "not_found", "We couldn't find that session.")
            return None
        return path

    def _prompt_images(self, q: dict[str, list[str]]) -> None:
        path = self._image_source((q.get("id") or [""])[0])
        if path is None:
            return
        try:
            turns = prompt_images.list_images(path)
        except prompt_images.Unavailable as e:
            return self._error(e.status, e.code, e.message)
        self._json({"success": True, "turns": turns})

    def _prompt_image(self, q: dict[str, list[str]]) -> None:
        path = self._image_source((q.get("id") or [""])[0])
        if path is None:
            return
        turn, index = (_non_negative_int((q.get(k) or [""])[0]) for k in ("turn", "i"))
        if turn is None or index is None:
            return self._error(400, "invalid_input", "turn and i must be non-negative integers.")
        try:
            content_type, data = prompt_images.read_image(path, turn, index)
        except prompt_images.Unavailable as e:
            return self._error(e.status, e.code, e.message)
        self._bytes(content_type, data)

    def _hook_detail(self, q: dict[str, list[str]]) -> None:
        path = self._image_source((q.get("id") or [""])[0])
        if path is None:
            return
        uuid = (q.get("uuid") or [""])[0]
        if not _UUID_RE.fullmatch(uuid):
            return self._error(400, "invalid_input", "uuid must be a transcript record id.")
        try:
            message, command = hook_detail.read_hook_detail(path, uuid)
        except prompt_images.Unavailable as e:
            return self._error(e.status, e.code, e.message)
        self._json({"success": True, "message": message, "command": command})


def serve(
    port: int = 7317,
    *,
    store: Store | None = None,
    host: str = "127.0.0.1",
    scanning: threading.Event | None = None,
) -> ThreadingHTTPServer:
    """Create (but do not start) the local server. Call ``serve_forever()`` on the result.

    ``scanning``: set while a background scan runs; ``/api/status`` reports it.
    """
    handler = type("Handler", (_Handler,), {
        "store": store or Store(), "static": viewer_root(), "scanning": scanning,
        # Per server, so two servers in one process never share computed facts or postings.
        "facts_cache": {}, "files_cache": {}, "search_mem": {}, "search_file": (None, {}),
    })
    try:
        return _Server((host, port), handler)
    except OSError:
        if port == 0:
            raise
        return _Server((host, 0), handler)  # port taken: pick a free one


__all__ = ["serve", "viewer_root", "HTTPStatus"]
