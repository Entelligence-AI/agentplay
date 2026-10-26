"""Malformed input and races that must degrade to a clean answer, never a crash."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agentplay import builder, cli, discover, hook_detail, pipeline
from agentplay import prompt_images as pi
from agentplay import server as server_mod
from agentplay.server import serve
from agentplay.store import Store

# ---------------------------------------------------------------- prompt text


def _user(n: int, content: Any) -> str:
    rec = {"type": "user", "timestamp": f"2026-09-10T01:00:{n:02d}Z", "sessionId": "s",
           "message": {"role": "user", "content": content}}
    return json.dumps(rec) + "\n"


BAD_TEXT = [{"type": "text", "text": None}, {"type": "text", "text": 7}, {"type": "text"}]


def _transcript(tmp_path: Path) -> Path:
    f = tmp_path / "s.jsonl"
    f.write_text(
        _user(1, [{"type": "text", "text": "first"}, *BAD_TEXT])
        + _user(2, BAD_TEXT)
        + _user(3, "third")
    )
    return f


def test_user_turns_skip_non_string_text_blocks(tmp_path) -> None:
    with open(_transcript(tmp_path), encoding="utf-8") as fh:
        turns = builder.user_turns(fh)
    assert [text for _, _, text in turns] == ["first", "third"]


def test_claude_code_map_survives_non_string_text_blocks(tmp_path) -> None:
    doc = builder.build_claude_code_map(_transcript(tmp_path))
    assert sum(1 for e in doc["events"] if e["kind"] == "user") == 2


def test_local_task_titles_survive_non_string_text_blocks(tmp_path) -> None:
    c = discover.Candidate("s", "claude-code", str(_transcript(tmp_path)), 0.0)
    assert discover.local_task_titles(c) == ["first", "third"]


def test_first_prompt_title_ignores_non_string_text_blocks(tmp_path) -> None:
    f = tmp_path / "t.jsonl"
    f.write_text(_user(1, [*BAD_TEXT, {"type": "text", "text": "real prompt"}]))
    c = discover.Candidate("s", "claude-code", str(f), 0.0)
    assert discover.local_title(c) == "real prompt"


# ---------------------------------------------------------------- store.index


def test_index_is_never_none_when_another_thread_invalidates_it(tmp_path, monkeypatch) -> None:
    st = Store(tmp_path)
    real = st._disk_stamp

    def invalidating() -> tuple[int, int] | None:
        st._index = None  # what `reload_if_changed` does from another thread
        return real()

    monkeypatch.setattr(st, "_disk_stamp", invalidating)
    for _ in range(50):
        st._index = None
        assert st.index == {}


# ---------------------------------------------------------------- server


@pytest.fixture()
def httpd(tmp_path):
    server = serve(0, store=Store(tmp_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _request(server, method: str, headers: dict[str, str]) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.putrequest(method, "/api/account" if method == "POST" else "/api/status", skip_host=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.putheader("Content-Length", "2")
    conn.endheaders(b"{}")
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    return resp.status, body


@pytest.mark.parametrize("host", ["[::1", "[::1:80", "a]b", "localhost:99999", "localhost:abc"])
def test_malformed_host_is_403_json(httpd, host) -> None:
    for method in ("GET", "POST"):
        status, body = _request(httpd, method, {"Host": host})
        assert status == 403
        assert body["code"] == "forbidden"


@pytest.mark.parametrize("origin", ["http://[::1", "http://a]b", "http://localhost:99999"])
def test_malformed_origin_is_403_json(httpd, origin) -> None:
    for method in ("GET", "POST"):
        status, body = _request(httpd, method, {"Host": "localhost", "Origin": origin})
        assert status == 403
        assert body["code"] == "forbidden"


def test_local_host_with_a_valid_port_still_passes(httpd) -> None:
    status, _ = _request(httpd, "GET", {"Host": "localhost:7317"})
    assert status == 200


_THEME = b"document.documentElement.dataset.theme = 'dark';"


@pytest.fixture()
def site(tmp_path, monkeypatch):
    """A server over a stand-in viewer bundle with one inline script, like the real index.html."""
    static = tmp_path / "viewer"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_bytes(b"<!doctype html><script>" + _THEME + b"</script><div id=root></div>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    sibling = tmp_path / "viewer-private"  # shares the bundle dir's name as a prefix
    sibling.mkdir()
    (sibling / "secret.txt").write_text("SECRET")
    monkeypatch.setattr(server_mod, "viewer_root", lambda: static)
    server = serve(0, store=Store(tmp_path / "sm"))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _get(server, path: str, headers: dict[str, str] | None = None, method: str = "GET"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, resp.headers, body


@pytest.mark.parametrize(("path", "method"), [
    ("/", "GET"), ("/assets/app.js", "GET"), ("/api/status", "GET"), ("/api/nope", "GET"),
    ("/api/status", "PUT"),  # http.server's own 501 page
])
def test_every_response_carries_the_security_headers(site, path, method) -> None:
    _, headers, _ = _get(site, path, method=method)
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_the_page_csp_allows_only_its_own_inline_script(site) -> None:
    status, headers, body = _get(site, "/")
    assert status == 200 and _THEME in body
    csp = headers["Content-Security-Policy"]
    for directive in ("default-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "object-src 'none'"):
        assert directive in csp
    digest = base64.b64encode(hashlib.sha256(_THEME).digest()).decode()
    assert f"script-src 'self' 'sha256-{digest}';" in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe-inline'" not in csp


def test_json_is_same_origin_only(site) -> None:
    _, headers, _ = _get(site, "/api/status")
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


@pytest.mark.parametrize(("site_header", "status"), [
    (None, 200), ("same-origin", 200), ("none", 200), ("same-site", 403), ("cross-site", 403),
])
def test_only_same_origin_fetches_read_the_api(site, site_header, status) -> None:
    headers = {"Sec-Fetch-Site": site_header} if site_header else {}
    assert _get(site, "/api/status", headers)[0] == status
    # A link from anywhere still opens the page itself.
    assert _get(site, "/", headers)[0] == 200


@pytest.mark.parametrize("payload", [b'{"o":1e999}', b'{"o":"x"}', b"[]", b"not json"])
def test_a_bad_cursor_is_400(site, payload) -> None:
    cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    status, _, body = _get(site, f"/api/sessions?cursor={cursor}")
    assert status == 400 and json.loads(body)["code"] == "invalid_input"


def test_a_path_too_long_for_the_filesystem_gets_the_page(site) -> None:
    status, _, body = _get(site, "/" + "a" * 5000)
    assert status == 200 and _THEME in body


def test_static_files_stay_inside_the_bundle(site) -> None:
    status, _, body = _get(site, "/../viewer-private/secret.txt")
    assert status == 200 and b"SECRET" not in body and _THEME in body


def test_server_takes_a_burst_and_drops_a_stalled_client(site) -> None:
    assert site.request_queue_size == 128
    assert server_mod._Handler.timeout == 15
    site.RequestHandlerClass.timeout = 0.2
    stalled = socket.create_connection(("127.0.0.1", site.server_address[1]))
    stalled.sendall(b"GET /api/status HTTP/1.1\r\n")  # and never finishes
    stalled.settimeout(5)
    started = time.monotonic()
    assert stalled.recv(1024) == b""  # the server hung up
    assert time.monotonic() - started < 4
    stalled.close()


def test_store_never_reads_outside_maps(tmp_path) -> None:
    st = Store(tmp_path)
    st.put({"session": {"id": "s1"}}, {"sessionId": "s1"})
    (tmp_path / "outside.json").write_text('{"leaked": true}')
    assert st.get("s1") == {"session": {"id": "s1"}}
    for key in ("../outside", "/etc/passwd", "a/../../outside-0123456789", None, 7):
        st.index["bad"] = {"sessionId": "bad", "key": key}
        assert st.get("bad") is None
    # A well-formed key that is a symlink out of maps/ is refused too.
    link = st.maps_dir / "evil-0123456789.json"
    link.symlink_to(tmp_path / "outside.json")
    st.index["bad"] = {"sessionId": "bad", "key": "evil-0123456789"}
    assert st.get("bad") is None


# ---------------------------------------------------------------- transcript races


def _png_turn(tmp_path: Path) -> str:
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
    f = tmp_path / "t.jsonl"
    f.write_text(_user(1, [{"type": "text", "text": "look"}, block]))
    return str(f)


@pytest.mark.parametrize(
    ("exc", "status"), [(FileNotFoundError(), 410), (PermissionError(), 404)]
)
def test_prompt_images_scan_open_failure_after_stat(tmp_path, monkeypatch, exc, status) -> None:
    path = _png_turn(tmp_path)
    pi._scan.cache_clear()

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr("builtins.open", boom)
    with pytest.raises(pi.Unavailable) as e:
        pi.list_images(path)
    assert e.value.status == status


@pytest.mark.parametrize(
    ("exc", "status"), [(FileNotFoundError(), 410), (PermissionError(), 404)]
)
def test_prompt_image_read_open_failure_after_scan(tmp_path, monkeypatch, exc, status) -> None:
    path = _png_turn(tmp_path)
    pi.list_images(path)  # warm the scan cache

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr("builtins.open", boom)
    with pytest.raises(pi.Unavailable) as e:
        pi.read_image(path, 1, 0)
    assert e.value.status == status


def test_hook_detail_unreadable_transcript_is_404(tmp_path, monkeypatch) -> None:
    f = tmp_path / "t.jsonl"
    f.write_text("{}\n")
    hook_detail._scan.cache_clear()

    def boom(*_a, **_k):
        raise PermissionError

    monkeypatch.setattr("builtins.open", boom)
    with pytest.raises(hook_detail.Unavailable) as e:
        hook_detail.read_hook_detail(str(f), "u")
    assert e.value.status == 404


# ---------------------------------------------------------------- cli -v


def _report(**kw: Any) -> pipeline.BuildReport:
    base = {"found": 1, "built": 0, "unchanged": 0, "skipped": 0, "failed": 1, "seconds": 0.0,
            "errors": ["s1: boom"]}
    return pipeline.BuildReport(**{**base, **kw})


@pytest.mark.parametrize("argv", [["-v", "build"], ["build", "-v"], ["build", "--verbose"]])
def test_verbose_is_accepted_before_or_after_the_subcommand(monkeypatch, capsys, argv) -> None:
    monkeypatch.setattr(cli.pipeline, "build_all", lambda **_kw: _report())
    monkeypatch.setattr(cli, "_configure_logging", lambda verbose: None)
    assert cli.main(argv) == 0
    assert "s1: boom" in capsys.readouterr().out


def test_failures_are_hidden_without_verbose(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.pipeline, "build_all", lambda **_kw: _report())
    monkeypatch.setattr(cli, "_configure_logging", lambda verbose: None)
    assert cli.main(["build"]) == 0
    assert "s1: boom" not in capsys.readouterr().out


# ---------------------------------------------------------------- transcript size caps


def test_oversized_non_claude_transcript_is_skipped_before_it_is_read(monkeypatch) -> None:
    """The shared parser reads a file whole before checking its own cap; build_map refuses it first."""
    from agentplay._core.ingestion.coding_agent._sources._base import MAX_SESSION_BYTES

    class Source:
        def get_spans_by_session(self, session_id: str) -> list[Any]:
            raise AssertionError("read a transcript past the parser's cap")

    c = discover.Candidate("local:codex:big", "codex", "/Users/me/.codex/sessions/big.jsonl", 0.0)
    monkeypatch.setattr(discover, "source_bytes", lambda c: MAX_SESSION_BYTES + 1)
    with pytest.raises(discover.SkipSession, match="too_large"):
        discover.build_map(c, Source())
