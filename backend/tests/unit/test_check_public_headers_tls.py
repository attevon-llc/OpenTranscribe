"""check-public-headers.py against real TLS handshakes with local servers."""

from __future__ import annotations

import importlib.util
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

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


# --- real TLS handshakes against local servers ----------------------------------------------------

_TLS_GOOD = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(self)",
}


def _make_cert(directory: Path) -> tuple[Path, Path]:
    cert, key = directory / "srv.crt", directory / "srv.key"
    proc = subprocess.run(
        [
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30',
            '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
            '-keyout', str(key), '-out', str(cert),
        ],
        capture_output=True,
        check=False,
    )  # fmt: skip
    if proc.returncode != 0:
        pytest.skip("openssl CLI cannot create a test certificate here")
    return cert, key


def _legacy_tls_available() -> bool:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.maximum_version = ssl.TLSVersion.TLSv1_1
    except (ValueError, ssl.SSLError):
        return False
    return True


def _legacy_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1_1
    ctx.load_cert_chain(cert, key)
    return ctx


def _tls_server(ctx: ssl.SSLContext, stop: threading.Event) -> int:
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(8)
    lsock.settimeout(0.2)

    def handle(conn: socket.socket) -> None:
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                request = tls.recv(4096).decode("latin-1")
                status = "401 Unauthorized" if " /api/auth/me " in request else "200 OK"
                head = "".join(f"{k}: {v}\r\n" for k, v in _TLS_GOOD.items())
                tls.sendall(
                    f"HTTP/1.1 {status}\r\n{head}Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
                )
        except (OSError, ssl.SSLError):
            return

    def loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = lsock.accept()
            except OSError:
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
        lsock.close()

    threading.Thread(target=loop, daemon=True).start()
    return int(lsock.getsockname()[1])


@pytest.fixture
def tls_pair(tmp_path: Path) -> Iterator[tuple[int, int, int, Path]]:
    """(modern-only, legacy-only, TLS1.0-to-1.3 ports, certificate) on 127.0.0.1."""
    if not _legacy_tls_available():
        pytest.skip("local OpenSSL cannot offer TLS 1.0/1.1, so the legacy-TLS floor is untestable")
    cert, key = _make_cert(tmp_path)
    stop = threading.Event()
    modern = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    modern.minimum_version = ssl.TLSVersion.TLSv1_2
    modern.load_cert_chain(cert, key)
    modern_port = _tls_server(modern, stop)
    legacy_port = _tls_server(_legacy_context(cert, key), stop)
    mixed = _legacy_context(cert, key)
    mixed.maximum_version = ssl.TLSVersion.TLSv1_3
    mixed_port = _tls_server(mixed, stop)
    yield modern_port, legacy_port, mixed_port, cert
    stop.set()


def test_legacy_probe_rejected_by_modern_server(tls_pair: tuple[int, int, int, Path]) -> None:
    modern_port, _, _, _ = tls_pair
    assert chk.legacy_tls_accepted("127.0.0.1", modern_port) is False


def test_legacy_probe_accepted_by_old_server(tls_pair: tuple[int, int, int, Path]) -> None:
    _, legacy_port, _, _ = tls_pair
    assert chk.legacy_tls_accepted("127.0.0.1", legacy_port) is True


def test_cli_modern_tls_server_passes(
    tls_pair: tuple[int, int, int, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    modern_port, _, _, cert = tls_pair
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    assert chk.main([f"https://127.0.0.1:{modern_port}", "--skip-redirect-check"]) == 0


def test_cli_untrusted_certificate_never_passes(
    tls_pair: tuple[int, int, int, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    modern_port, _, _, _ = tls_pair
    empty = tmp_path / "empty.pem"
    empty.write_text("")
    monkeypatch.setenv("SSL_CERT_FILE", str(empty))
    assert chk.main([f"https://127.0.0.1:{modern_port}", "--skip-redirect-check"]) != 0


def test_cli_server_accepting_tls_1_1_is_a_finding(
    tls_pair: tuple[int, int, int, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default handshake negotiates the top version, so only the capped one sees this."""
    _, _, mixed_port, cert = tls_pair
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    assert chk.main([f"https://127.0.0.1:{mixed_port}", "--skip-redirect-check"]) == 1
