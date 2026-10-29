"""scripts/check-public-headers.py flags weak headers/cookies/TLS and fails closed on errors."""

from __future__ import annotations

import contextlib
import importlib.util
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-public-headers.py"
NOW = 1_800_000_000.0


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_public_headers", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


chk = _load()


class ResponseLike(Protocol):
    status: int
    headers: list[tuple[str, str]]


GOOD: dict[str, str] = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(self)",
    "Server": "nginx",
}
GOOD_COOKIES = [
    "access_token=a; HttpOnly; Secure; SameSite=Lax; Path=/",
    "refresh_token=r; HttpOnly; Secure; SameSite=Strict; Path=/api/auth",
    "csrf_token=c; Secure; SameSite=Lax; Path=/",
]


def _resp(
    headers: dict[str, str] | None = None, cookies: list[str] | None = None, status: int = 200
) -> ResponseLike:
    merged = dict(GOOD if headers is None else headers)
    pairs = list(merged.items()) + [("Set-Cookie", c) for c in (cookies or [])]
    response: ResponseLike = chk.Response(status, pairs)
    return response


def _good_tls() -> object:
    return chk.TlsInfo("TLSv1.3", NOW + 90 * 86400)


def _run(
    resp: ResponseLike,
    tls: object | None = None,
    redirect: object | None = None,
    base: str = "https://example.test",
) -> list[str]:
    """Every path answers with *resp*'s headers and the status that path should have."""
    redirect = redirect or chk.Response(301, [("Location", "https://example.test/")])

    def fetch(url: str) -> object:
        if url.startswith("http://"):
            return redirect
        status = 401 if url.endswith("/api/auth/me") else 200
        return chk.Response(status, resp.headers)

    return list(chk.run_checks(base, fetch, lambda h, p: tls or _good_tls(), now=NOW))


def _without(name: str) -> dict[str, str]:
    return {k: v for k, v in GOOD.items() if k != name}


def test_good_set_passes() -> None:
    assert _run(_resp(cookies=GOOD_COOKIES)) == []


@pytest.mark.parametrize(
    ("headers", "needle"),
    [
        (_without("Strict-Transport-Security"), "Strict-Transport-Security missing"),
        (
            {**GOOD, "Strict-Transport-Security": "max-age=0"},
            "max-age=0",
        ),
        ({**GOOD, "Strict-Transport-Security": "max-age=15551999"}, "max-age=15551999"),
        (_without("Content-Security-Policy"), "Content-Security-Policy missing"),
        (
            {
                **GOOD,
                "Content-Security-Policy": GOOD["Content-Security-Policy"] + "; x 'unsafe-eval'",
            },
            "unsafe-eval",
        ),
        (
            {
                **GOOD,
                "Content-Security-Policy": GOOD["Content-Security-Policy"] + "; img-src https:",
            },
            "bare scheme",
        ),
        (
            {**GOOD, "Content-Security-Policy": "script-src 'self'; frame-ancestors 'self'"},
            "object-src",
        ),
        (_without("X-Content-Type-Options"), "X-Content-Type-Options"),
        ({**GOOD, "X-Content-Type-Options": "sniff"}, "X-Content-Type-Options"),
        (_without("Referrer-Policy"), "Referrer-Policy missing"),
        ({**GOOD, "Referrer-Policy": "unsafe-url"}, "leaks full URLs"),
        (_without("Permissions-Policy"), "Permissions-Policy missing"),
        ({**GOOD, "Server": "nginx/1.25.3"}, "Server header leaks"),
        ({**GOOD, "X-Powered-By": "Express"}, "X-Powered-By"),
    ],
)
def test_planted_bad_header_is_flagged(headers: dict[str, str], needle: str) -> None:
    findings = _run(_resp(headers))
    assert any(needle in f for f in findings), findings


def test_hsts_exactly_at_the_floor_passes() -> None:
    assert _run(_resp({**GOOD, "Strict-Transport-Security": "max-age=15552000"})) == []


def test_no_frame_protection_is_flagged() -> None:
    headers = _without("X-Frame-Options")
    headers["Content-Security-Policy"] = "default-src 'self'; object-src 'none'"
    assert any("clickjacking" in f for f in _run(_resp(headers)))


def test_frame_ancestors_alone_is_enough() -> None:
    headers = _without("X-Frame-Options")
    assert _run(_resp(headers)) == []


@pytest.mark.parametrize(
    ("cookie", "needle"),
    [
        ("access_token=a; HttpOnly; SameSite=Lax", "lacks Secure"),
        ("access_token=a; Secure; SameSite=Lax", "lacks HttpOnly"),
        ("access_token=a; Secure; HttpOnly", "lacks SameSite"),
        ("access_token=a; Secure; HttpOnly; SameSite=None", "lacks SameSite"),
    ],
)
def test_bad_auth_cookie_is_flagged(cookie: str, needle: str) -> None:
    assert any(needle in f for f in _run(_resp(cookies=[cookie])))


def test_csrf_cookie_may_be_script_readable() -> None:
    assert _run(_resp(cookies=["csrf_token=c; Secure; SameSite=Lax"])) == []


def test_redirect_to_http_or_missing_redirect_is_flagged() -> None:
    no_redirect = chk.Response(200, [])
    assert any("redirect" in f for f in _run(_resp(), redirect=no_redirect))
    downgrade = chk.Response(301, [("Location", "http://example.test/")])
    assert any("redirect" in f for f in _run(_resp(), redirect=downgrade))


def test_non_default_port_needs_explicit_redirect_decision() -> None:
    findings = _run(_resp(), base="https://example.test:8443")
    assert any("--http-url" in f for f in findings)


def test_old_tls_version_is_flagged() -> None:
    findings = _run(_resp(), tls=chk.TlsInfo("TLSv1.1", NOW + 90 * 86400))
    assert any("below TLSv1.2" in f for f in findings)


def test_certificate_expiry_boundary() -> None:
    soon = chk.TlsInfo("TLSv1.3", NOW + 13 * 86400)
    assert any("expires in 13 days" in f for f in _run(_resp(), tls=soon))
    ok = chk.TlsInfo("TLSv1.3", NOW + 15 * 86400)
    assert _run(_resp(), tls=ok) == []


def test_unverified_certificate_is_flagged() -> None:
    bad = chk.TlsInfo("TLSv1.3", None, verified=False, verify_error="self signed")
    assert any("does not verify" in f for f in _run(_resp(), tls=bad))


def test_unknown_expiry_is_a_finding_not_a_pass() -> None:
    assert any("expiry unknown" in f for f in _run(_resp(), tls=chk.TlsInfo("TLSv1.3", None)))


def test_non_http_base_is_unusable() -> None:
    with pytest.raises(chk.TargetUnusable):
        chk.run_checks("ftp://x", lambda u: _resp(), lambda h, p: _good_tls())


# --- real sockets: a local server on an ephemeral 127.0.0.1 port ---------------------------------


def _serve(headers: dict[str, str], cookies: list[str]) -> tuple[HTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(401 if self.path == "/api/auth/me" else 200)
            for k, v in headers.items():
                self.send_header(k, v)
            for c in cookies:
                self.send_header("Set-Cookie", c)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def version_string(self) -> str:
            return "fixture"

        def log_message(self, *args: object) -> None:
            return None

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def serve() -> Iterator[Callable[[dict[str, str], list[str]], str]]:
    servers: list[HTTPServer] = []

    def start(headers: dict[str, str], cookies: list[str]) -> str:
        srv, url = _serve(headers, cookies)
        servers.append(srv)
        return url

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def serve_status() -> Iterator[Callable[[int], str]]:
    servers: list[HTTPServer] = []

    def start(status: int) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(status)
                for k, v in _without("Strict-Transport-Security").items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return None

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_cli_good_server_exits_0(
    serve: Callable[[dict[str, str], list[str]], str], capsys: pytest.CaptureFixture[str]
) -> None:
    url = serve(_without("Strict-Transport-Security"), GOOD_COOKIES)
    assert chk.main([url, "--allow-http"]) == 0
    assert "OK" in capsys.readouterr().out


def test_cli_bad_server_exits_1(
    serve: Callable[[dict[str, str], list[str]], str], capsys: pytest.CaptureFixture[str]
) -> None:
    headers = _without("Strict-Transport-Security")
    headers.pop("X-Content-Type-Options")
    url = serve(headers, [])
    assert chk.main([url, "--allow-http"]) == 1
    assert "X-Content-Type-Options" in capsys.readouterr().out


def test_cli_http_base_without_allow_flag_is_a_finding(
    serve: Callable[[dict[str, str], list[str]], str],
) -> None:
    url = serve(_without("Strict-Transport-Security"), GOOD_COOKIES)
    assert chk.main([url]) == 1


def test_cli_connection_refused_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert chk.main([f"http://127.0.0.1:{port}", "--allow-http"]) == 2
    assert "UNUSABLE" in capsys.readouterr().err


def test_cli_malformed_url_exits_2() -> None:
    assert chk.main(["not-a-url"]) == 2


def test_parse_not_after_reads_cert_dict() -> None:
    assert chk._not_after({"notAfter": "Jan  1 00:00:00 2030 GMT"}) == pytest.approx(1893456000.0)
    assert chk._not_after({}) is None


# --- status codes, deadline, hostile inputs, stricter parsing -------------------------------------


def _run_by_path(
    responses: dict[str, object],
    redirect: object | None = None,
    base: str = "https://example.test",
    notes: list[str] | None = None,
) -> list[str]:
    redirect = redirect or chk.Response(301, [("Location", "https://example.test/")])

    def fetch(url: str) -> object:
        if url.startswith("http://"):
            return redirect
        return responses[url.removeprefix(base.rstrip("/"))]

    return list(chk.run_checks(base, fetch, lambda h, p: _good_tls(), now=NOW, notes=notes))


def _ok_responses() -> dict[str, object]:
    return {
        "/": _resp(),
        "/api/auth/session": _resp(),
        "/api/auth/me": _resp(status=401),
    }


def test_expected_statuses_pass() -> None:
    assert _run_by_path(_ok_responses()) == []


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/", 500),
        ("/", 502),
        ("/", 301),
        ("/api/auth/session", 500),
        ("/api/auth/session", 401),
        ("/api/auth/me", 200),
        ("/api/auth/me", 502),
    ],
)
def test_wrong_status_is_a_finding(path: str, status: int) -> None:
    responses = _ok_responses()
    responses[path] = _resp(status=status)
    findings = _run_by_path(responses)
    assert any(path in f and f"status {status}" in f for f in findings), findings


def test_outage_page_with_good_headers_never_passes_cli(
    serve_status: Callable[[int], str], capsys: pytest.CaptureFixture[str]
) -> None:
    url = serve_status(502)
    assert chk.main([url, "--allow-http"]) == 1
    assert "OK" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test:99999",
        "http://127.0.0.1:abc/",
        "http://a..b/",
        "https://[::1",
        "http://",
        "",
    ],
)
def test_unusable_inputs_exit_2(url: str) -> None:
    assert chk.main([url, "--allow-http"]) == 2


def test_ipv6_base_builds_bracketed_http_twin() -> None:
    assert chk.default_http_url("https://[::1]/") == "http://[::1]/"
    assert chk.default_http_url("https://[::1]:8443/") is None
    assert chk.default_http_url("https://Example.TEST/") == "http://example.test/"


def test_no_cookie_is_reported_unverified_not_passed() -> None:
    notes: list[str] = []
    _run_by_path(_ok_responses(), notes=notes)
    assert "cookies: none observed (UNVERIFIED)" in notes


def test_observed_cookies_produce_no_unverified_note() -> None:
    notes: list[str] = []
    responses = _ok_responses()
    responses["/"] = _resp(cookies=GOOD_COOKIES)
    _run_by_path(responses, notes=notes)
    assert not any("cookies" in n for n in notes)


def test_untestable_legacy_tls_is_noted_not_passed() -> None:
    notes: list[str] = []
    tls = chk.TlsInfo("TLSv1.3", NOW + 90 * 86400, legacy_tls=None)
    chk.run_checks(
        "https://example.test",
        lambda url: (
            _resp()
            if not url.startswith("http://")
            else chk.Response(301, [("Location", "https://example.test/")])
        ),
        lambda h, p: tls,
        now=NOW,
        notes=notes,
    )
    assert any("TLS 1.0/1.1" in n and "UNVERIFIED" in n for n in notes)


def test_accepted_legacy_tls_is_a_finding() -> None:
    tls = chk.TlsInfo("TLSv1.3", NOW + 90 * 86400, legacy_tls=True)
    assert any("TLS 1.0/1.1" in f for f in _run(_resp(), tls=tls))


@pytest.mark.parametrize(
    ("csp", "needle"),
    [
        ("default-src *; object-src 'none'; frame-ancestors 'self'", "wildcard"),
        (
            "default-src 'self'; script-src * ; object-src 'none'; frame-ancestors 'self'",
            "wildcard",
        ),
        (
            "default-src 'self'; script-src data: 'self'; object-src 'none'; frame-ancestors 'self'",
            "data:",
        ),
        (
            "default-src 'self'; object-src *; object-src 'none'; frame-ancestors 'self'",
            "object-src",
        ),
        ("default-src 'self'; object-src 'none'; frame-ancestors *", "frame-ancestors"),
        ("object-src 'none'; frame-ancestors 'self'", "neither default-src nor script-src"),
    ],
)
def test_weak_csp_is_flagged(csp: str, needle: str) -> None:
    headers = _without("X-Frame-Options")
    headers["Content-Security-Policy"] = csp
    findings = _run(_resp(headers))
    assert any(needle in f for f in findings), findings


def test_first_occurrence_of_a_directive_wins() -> None:
    good_first = "default-src 'self'; object-src 'none'; object-src *; frame-ancestors 'self'"
    assert _run(_resp({**GOOD, "Content-Security-Policy": good_first})) == []


def test_wildcard_frame_ancestors_does_not_count_even_with_xfo_missing() -> None:
    headers = _without("X-Frame-Options")
    headers["Content-Security-Policy"] = "default-src 'self'; object-src 'none'; frame-ancestors *"
    assert any("clickjacking" in f or "frame-ancestors" in f for f in _run(_resp(headers)))


def test_second_csp_header_can_supply_a_missing_protection() -> None:
    weak = "default-src 'self' 'unsafe-eval'; object-src 'none'; frame-ancestors 'self'"
    strong = "default-src 'self'; object-src 'none'"
    resp = chk.Response(200, [*_resp().headers, ("Content-Security-Policy", weak)])
    resp.headers = [(k, v) for k, v in resp.headers if k != "Content-Security-Policy"] + [
        ("Content-Security-Policy", weak),
        ("Content-Security-Policy", strong),
    ]
    assert not any("unsafe-eval" in f for f in chk.check_csp(resp, "x"))
    only_weak = chk.Response(200, [("Content-Security-Policy", weak)])
    assert any("unsafe-eval" in f for f in chk.check_csp(only_weak, "x"))


@pytest.mark.parametrize(
    "value",
    [
        "x-max-age=31536000",
        "max-age=31536000abc",
        "max-age=31536000; max-age=0",
        "includeSubDomains",
        "max-age=",
    ],
)
def test_malformed_hsts_is_flagged(value: str) -> None:
    findings = _run(_resp({**GOOD, "Strict-Transport-Security": value}))
    assert any("Strict-Transport-Security" in f for f in findings), findings


def test_hsts_quoted_and_spaced_forms_are_accepted() -> None:
    value = 'includeSubDomains; MAX-AGE="31536000"; preload'
    assert _run(_resp({**GOOD, "Strict-Transport-Security": value})) == []


@pytest.mark.parametrize("value", ["no-referrer, unsafe-url", "unsafe-url", "bogus"])
def test_bad_referrer_policy_is_flagged(value: str) -> None:
    assert any("Referrer-Policy" in f for f in _run(_resp({**GOOD, "Referrer-Policy": value})))


def test_referrer_fallback_list_with_safe_last_token_passes() -> None:
    assert _run(_resp({**GOOD, "Referrer-Policy": "no-referrer, strict-origin"})) == []


def test_empty_permissions_policy_is_flagged() -> None:
    assert any("Permissions-Policy" in f for f in _run(_resp({**GOOD, "Permissions-Policy": " "})))


@pytest.mark.parametrize("server", ["awselb/2.0", "AmazonS3", "nginx", "uvicorn", "cloudflare"])
def test_server_without_a_software_version_is_fine(server: str) -> None:
    assert _run(_resp({**GOOD, "Server": server})) == []


@pytest.mark.parametrize("server", ["nginx/1.25.3", "Apache/2.4.58 (Ubuntu)", "nginx/1.25"])
def test_server_with_a_software_version_is_flagged(server: str) -> None:
    assert any("Server header leaks" in f for f in _run(_resp({**GOOD, "Server": server})))


@pytest.mark.parametrize(
    "location",
    ["https://evil.example/", "/relative", "http://example.test/", ""],
)
def test_redirect_must_go_to_https_on_the_same_host(location: str) -> None:
    resp = chk.Response(301, [("Location", location)])
    assert any("redirect" in f for f in _run(_resp(), redirect=resp))


def test_redirect_303_to_same_host_https_is_accepted() -> None:
    resp = chk.Response(303, [("Location", "https://EXAMPLE.test:8443/x")])
    assert _run(_resp(), redirect=resp) == []


def _bounded_server(
    handler_body: Callable[[socket.socket], None],
) -> tuple[int, Callable[[], None]]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def loop() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            threading.Thread(target=handler_body, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()

    def close() -> None:
        stop.set()
        srv.close()

    return srv.getsockname()[1], close


def _run_cli(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_slow_drip_headers_hit_the_overall_deadline() -> None:
    stop = threading.Event()

    def drip(conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not stop.is_set():
                conn.sendall(b"X-A: b\r\n")
                stop.wait(0.2)

    port, close = _bounded_server(drip)
    try:
        start = time.monotonic()
        result = _run_cli(f"http://127.0.0.1:{port}", "--allow-http", "--deadline", "2")
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        close()
    assert result.returncode == 2, result.stdout + result.stderr
    assert "deadline" in result.stderr
    assert elapsed < 15


def test_endless_chunked_body_is_not_read_to_the_end() -> None:
    stop = threading.Event()

    def endless(conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            chunk = b"400\r\n" + b"a" * 1024 + b"\r\n"
            while not stop.is_set():
                conn.sendall(chunk)

    port, close = _bounded_server(endless)
    try:
        resp = chk.http_fetch(f"http://127.0.0.1:{port}/")
    finally:
        stop.set()
        close()
    assert resp.status == 200
