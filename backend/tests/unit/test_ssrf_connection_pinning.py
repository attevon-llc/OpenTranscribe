"""The SSRF guard must connect to the address it validated (DNS rebinding).

`resolve_public_addresses` returned the resolved addresses SPECIFICALLY so callers could
pin them — its docstring said so — and **no production caller did**. Its only callers were
`is_safe_url`, which discards them, and one test. So every SSRF check in the product was
validate-then-re-resolve:

    assert_safe_outbound_url(url)      # resolution #1: judged
    await client.get(url)              # resolution #2: connected to

A hostname whose DNS alternates between a public address and 127.0.0.1 / 169.254.169.254
passes on answer #1 and is dialled on answer #2, defeating the check without beating it.
`auth/oidc/discovery.py` runs that pair at **login time for every user** and TTL-caches the
result; `api/endpoints/llm_settings.py`'s model-discovery handlers run it on a `base_url`
supplied as a query parameter by any authenticated user.

The tests here are built around a **resolver stub that answers differently on each call** —
public/loopback first, something else second. A test that only asserts "the pinned URL
contains an IP" would pass against code that then threw the IP away, so the proof shape is
two real servers on two loopback addresses: pinned code reaches the FIRST answer, and
re-resolving code reaches the SECOND.

TLS is the hard half — connecting to an IP while verifying the certificate against the
hostname — so `TestTlsIsNotWeakened` runs a real TLS server with a real private CA and
asserts both directions: a matching certificate is accepted, and a non-matching one is
still REJECTED. Without that second case a "fix" that passed `verify=False` would look
identical to a correct one.
"""

from __future__ import annotations

import asyncio
import datetime
import http.server
import socket
import ssl
import threading
from collections.abc import Iterator
from typing import Any
from typing import cast

import pytest

from app.utils.url_validation import PinnedTarget
from app.utils.url_validation import resolve_pinned_target

# ── Loopback fixtures: two distinct addresses, both allowed under allow_private ──────
#
# 127.0.0.1 and 127.0.0.2 are both on the loopback interface on Linux (127.0.0.0/8 is
# entirely local), so a test can bind two servers that are genuinely different peers with
# no network, no privileges and no external dependency.

FIRST_ANSWER = "127.0.0.1"
SECOND_ANSWER = "127.0.0.2"


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Answers every GET with the address it is bound to and the Host header it saw."""

    server_version = "PinTest/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        which: str = self.server.which  # type: ignore[attr-defined]
        host = self.headers.get("Host", "")
        if self.path.startswith("/redirect-to-metadata"):
            # The second half of an SSRF: pass validation, then bounce the client at IMDS.
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/redirect-elsewhere"):
            # Same shape, but at a host that FAILS FAST instead of hanging: 169.254.x is
            # unroutable, so a client that follows it produces a timeout rather than a
            # legible error, which makes for a control that proves nothing in particular.
            # server_address is typed as a union (AF_UNIX sockets give a str), but an
            # HTTPServer's is always (host, port).
            port = cast("tuple[str, int]", self.server.server_address)[1]
            self.send_response(302)
            self.send_header("Location", f"http://elsewhere.test:{port}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = f'{{"served_by": "{which}", "host_header": "{host}"}}'.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return None


def _serve(bind_address: str, port: int = 0, ssl_context: ssl.SSLContext | None = None):
    """Start a throwaway HTTP(S) server on *bind_address*, return (server, port)."""
    server = http.server.HTTPServer((bind_address, port), _RecordingHandler)
    server.which = bind_address  # type: ignore[attr-defined]
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def rebinding_pair() -> Iterator[tuple[int, list[str]]]:
    """Two servers on two loopback addresses sharing one port, plus a call log.

    Yields ``(port, calls)``. ``calls`` records every hostname the stubbed resolver was
    asked for, so a test can prove there was exactly ONE resolution.
    """
    first, port = _serve(FIRST_ANSWER)
    second, _ = _serve(SECOND_ANSWER, port)
    try:
        yield port, []
    finally:
        first.shutdown()
        second.shutdown()


@pytest.fixture
def rebinding_dns(monkeypatch, rebinding_pair):
    """Stub ``socket.getaddrinfo``: FIRST_ANSWER once, then SECOND_ANSWER forever.

    This is the attacker. Validation sees an address it accepts; every later resolution —
    the one an unpinned HTTP client performs at connect time — gets a different one.

    The host is normalised to ``str`` because **anyio ASCII-encodes it before calling
    getaddrinfo**, so httpx arrives here with ``b"rebind.test"``. Comparing against the
    ``str`` alone silently delegated httpx to the real resolver, which made the "unpinned
    code reaches the second answer" control fail with NXDOMAIN instead of proving anything.
    """
    port, calls = rebinding_pair
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, prt, *args, **kwargs):
        name = host.decode("ascii") if isinstance(host, bytes) else host
        if name != "rebind.test":
            return real_getaddrinfo(host, prt, *args, **kwargs)
        calls.append(name)
        answer = FIRST_ANSWER if len(calls) == 1 else SECOND_ANSWER
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, prt))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return port, calls


# ── resolve_pinned_target: the shape of what callers get ────────────────────────────


class TestResolvePinnedTarget:
    def test_rewrites_host_to_the_validated_address(self, rebinding_dns):
        port, calls = rebinding_dns

        target, reason = resolve_pinned_target(
            f"http://rebind.test:{port}/api/tags", allow_private=True
        )

        assert reason == ""
        assert target is not None
        # The URL that will be dialled names the address that was JUDGED, not the name.
        assert target.url == f"http://{FIRST_ANSWER}:{port}/api/tags"
        assert target.address == FIRST_ANSWER
        # ...while everything needed to stay correct on the wire is preserved.
        assert target.hostname == "rebind.test"
        assert target.headers == {"Host": f"rebind.test:{port}"}
        assert target.original_url == f"http://rebind.test:{port}/api/tags"
        assert calls == ["rebind.test"], "there must be exactly ONE resolution"

    def test_https_carries_sni_hostname_for_certificate_verification(self, rebinding_dns):
        port, _ = rebinding_dns
        target, _ = resolve_pinned_target(f"https://rebind.test:{port}/", allow_private=True)

        assert target is not None
        # Without this the TLS handshake would verify the certificate against the IP.
        assert target.httpx_extensions == {"sni_hostname": "rebind.test"}

    def test_plaintext_sends_no_sni(self, rebinding_dns):
        port, _ = rebinding_dns
        target, _ = resolve_pinned_target(f"http://rebind.test:{port}/", allow_private=True)

        assert target is not None
        assert target.httpx_extensions == {}

    def test_refusal_propagates_the_reason_and_no_target(self):
        target, reason = resolve_pinned_target("http://169.254.169.254/latest/meta-data/")

        assert target is None
        assert "metadata" in reason.lower()

    def test_literal_address_is_left_alone(self):
        """Nothing resolved, so nothing can rebind — do not rewrite an equivalent URL."""
        target, _ = resolve_pinned_target("http://192.168.1.50:11434/v1", allow_private=True)

        assert target is not None
        assert target.pinned is False
        assert target.url == "http://192.168.1.50:11434/v1"
        assert target.headers == {}, "no Host override is needed when the URL names the IP"

    def test_ipv6_address_is_bracketed_in_the_rewritten_url(self, monkeypatch):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda h, p, *a, **k: [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", p, 0, 0))
            ],
        )
        target, _ = resolve_pinned_target("https://v6.example.com/x")

        assert target is not None
        # An unbracketed v6 literal would be parsed as host+port and dial the wrong place.
        assert target.url == "https://[2606:4700::1111]/x"

    def test_userinfo_and_query_survive_the_rewrite(self, rebinding_dns):
        port, _ = rebinding_dns
        target, _ = resolve_pinned_target(
            f"http://user:pw@rebind.test:{port}/v1/models?a=1", allow_private=True
        )

        assert target is not None
        assert target.url == f"http://user:pw@{FIRST_ANSWER}:{port}/v1/models?a=1"
        assert target.headers == {"Host": f"rebind.test:{port}"}


# ── The proof: the connection lands on the FIRST answer, not the second ─────────────


def _fetch_with_httpx(target: PinnedTarget) -> dict:
    import httpx

    async def go() -> dict:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                target.url,
                headers=target.headers,
                extensions=target.httpx_extensions,
                follow_redirects=False,
            )
            return dict(response.json())

    return asyncio.run(go())


class TestConnectionGoesToTheValidatedAddress:
    """The control for every test above: does the SOCKET land on the pinned address?

    Asserting on `target.url` alone would pass against code that computed a pinned URL and
    then ignored it. These tests read back which of the two servers actually answered.
    """

    def test_httpx_pinned_request_reaches_the_first_answer(self, rebinding_dns):
        port, calls = rebinding_dns
        target, _ = resolve_pinned_target(f"http://rebind.test:{port}/", allow_private=True)
        assert target is not None

        payload = _fetch_with_httpx(target)

        assert payload["served_by"] == FIRST_ANSWER, (
            "the request reached the address the SECOND DNS answer named — it re-resolved"
        )
        assert payload["host_header"] == f"rebind.test:{port}"
        assert calls == ["rebind.test"], "the client must not have resolved again"

    def test_unpinned_request_reaches_the_second_answer(self, rebinding_dns):
        """The control that makes the test above mean something.

        This is the OLD behaviour, reproduced deliberately: validate, discard, hand the
        hostname to the client. If the loopback pair could not distinguish the two
        answers, this test would also report FIRST_ANSWER and the one above would be
        vacuous.
        """
        port, calls = rebinding_dns
        url = f"http://rebind.test:{port}/"

        target, reason = resolve_pinned_target(url, allow_private=True)
        assert target is not None and reason == ""  # validation passed on answer #1

        import httpx

        async def go() -> dict:
            async with httpx.AsyncClient(timeout=5) as client:
                # The defect: the HOSTNAME is handed to the client, not the address.
                response = await client.get(url)
                return dict(response.json())

        payload = asyncio.run(go())

        assert payload["served_by"] == SECOND_ANSWER
        assert len(calls) > 1, "an unpinned client resolves a second time, by definition"

    def test_oidc_discovery_fetches_the_validated_address(self, rebinding_dns):
        """The real caller, end to end, with no stubbing of the fetch.

        `_fetch_json` is what runs at login for every user. The server answers a valid
        discovery document, so a re-resolving implementation would succeed too — the
        assertion is on WHICH server answered.
        """
        from app.auth.oidc import discovery as oidc_discovery

        port, calls = rebinding_dns
        oidc_discovery.clear_discovery_caches()
        try:
            document = asyncio.run(
                oidc_discovery._fetch_json(
                    f"http://rebind.test:{port}/.well-known/openid-configuration",
                    timeout=5.0,
                    purpose="test",
                )
            )
        finally:
            oidc_discovery.clear_discovery_caches()

        assert document is not None, "the fetch must succeed against the pinned server"
        assert document["served_by"] == FIRST_ANSWER
        assert calls == ["rebind.test"]

    def test_llm_health_check_fetches_the_validated_address(self, rebinding_dns, monkeypatch):
        """`LLMService.health_check` — the `requests` half of the same defect.

        The stub server answers 200 to everything, so a re-resolving implementation would
        also return True. The proof is the resolution COUNT: pinned code resolves once.
        """
        from app.core.config import settings
        from app.services import llm_service as llm_module

        port, calls = rebinding_dns
        monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True, raising=False)

        service = llm_module.LLMService(
            llm_module.LLMConfig(
                provider=llm_module.LLMProvider.CUSTOM,
                model="m",
                api_key="k",
                base_url=f"http://rebind.test:{port}/v1",
            )
        )
        try:
            assert service.health_check() is True
        finally:
            service.close()

        assert calls == ["rebind.test"], "health_check must not re-resolve the hostname"


# ── TLS: the pin must not have been bought by disabling verification ────────────────


def _private_ca():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ot-pin-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf_for(ca_key, ca_cert, dns_name: str, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    path = tmp_path / f"{dns_name}.pem"
    path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@pytest.fixture
def tls_server(tmp_path):
    """A TLS server on 127.0.0.1 whose certificate names `pinned.test`, and its CA bundle.

    Factory: call with a DNS name to serve a certificate for that name. Two names are used
    — the one the client asks for, and a different one — so the negative case proves
    verification is still on rather than merely absent.
    """
    from cryptography.hazmat.primitives import serialization

    ca_key, ca_cert = _private_ca()
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    servers = []

    def start(cert_dns_name: str) -> int:
        chain = _leaf_for(ca_key, ca_cert, cert_dns_name, tmp_path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(chain))
        server, port = _serve(FIRST_ANSWER, ssl_context=context)
        servers.append(server)
        return int(port)

    try:
        yield start, str(ca_bundle)
    finally:
        for server in servers:
            server.shutdown()


@pytest.fixture
def pin_to_loopback(monkeypatch):
    """Resolve `pinned.test` (and only it) to 127.0.0.1."""
    real_getaddrinfo = socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host in ("pinned.test", "impostor.test"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (FIRST_ANSWER, port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class TestTlsIsNotWeakened:
    """Pinning must not have been achieved by turning certificate verification off.

    Each client gets both directions. The negative case is the load-bearing one: a fix
    that passed `verify=False` would satisfy the positive case exactly as well.
    """

    def test_httpx_accepts_a_certificate_matching_the_pinned_hostname(
        self, tls_server, pin_to_loopback
    ):
        import httpx

        start, ca_bundle = tls_server
        port = start("pinned.test")

        target, _ = resolve_pinned_target(f"https://pinned.test:{port}/", allow_private=True)
        assert target is not None

        async def go() -> int:
            async with httpx.AsyncClient(
                verify=ssl.create_default_context(cafile=ca_bundle), timeout=5
            ) as client:
                response = await client.get(
                    target.url,
                    headers=target.headers,
                    extensions=target.httpx_extensions,
                )
                return int(response.status_code)

        # The socket goes to 127.0.0.1; the certificate is checked against `pinned.test`.
        assert asyncio.run(go()) == 200

    def test_httpx_still_rejects_a_certificate_for_a_different_name(
        self, tls_server, pin_to_loopback
    ):
        """The control. Without it, `verify=False` would pass the test above."""
        import httpx

        start, ca_bundle = tls_server
        port = start("somebody-else.test")  # signed by the same CA, wrong name

        target, _ = resolve_pinned_target(f"https://pinned.test:{port}/", allow_private=True)
        assert target is not None

        async def go() -> int:
            async with httpx.AsyncClient(
                verify=ssl.create_default_context(cafile=ca_bundle), timeout=5
            ) as client:
                response = await client.get(
                    target.url,
                    headers=target.headers,
                    extensions=target.httpx_extensions,
                )
                return int(response.status_code)

        with pytest.raises(httpx.ConnectError) as exc:
            asyncio.run(go())
        assert "CERTIFICATE_VERIFY_FAILED" in str(exc.value)

    def test_httpx_verification_is_bound_to_the_hostname_not_the_address(
        self, tls_server, pin_to_loopback
    ):
        """Dropping `sni_hostname` must FAIL — that is what makes it a real control.

        If the certificate were somehow accepted without it, `httpx_extensions` would be
        decoration and the pin could be "simplified" away in a later refactor.
        """
        import httpx

        start, ca_bundle = tls_server
        port = start("pinned.test")

        target, _ = resolve_pinned_target(f"https://pinned.test:{port}/", allow_private=True)
        assert target is not None

        async def go() -> int:
            async with httpx.AsyncClient(
                verify=ssl.create_default_context(cafile=ca_bundle), timeout=5
            ) as client:
                response = await client.get(target.url, headers=target.headers)  # no SNI
                return int(response.status_code)

        with pytest.raises(httpx.ConnectError) as exc:
            asyncio.run(go())
        assert "IP address mismatch" in str(exc.value)

    def test_requests_accepts_a_certificate_matching_the_pinned_hostname(
        self, tls_server, pin_to_loopback
    ):
        from app.utils.url_validation import pinned_requests_session

        start, ca_bundle = tls_server
        port = start("pinned.test")

        target, _ = resolve_pinned_target(f"https://pinned.test:{port}/", allow_private=True)
        assert target is not None

        with pinned_requests_session(target) as session:
            response = session.get(target.url, headers=target.headers, verify=ca_bundle, timeout=5)
        assert response.status_code == 200

    def test_requests_still_rejects_a_certificate_for_a_different_name(
        self, tls_server, pin_to_loopback
    ):
        import requests

        from app.utils.url_validation import pinned_requests_session

        start, ca_bundle = tls_server
        port = start("somebody-else.test")

        target, _ = resolve_pinned_target(f"https://pinned.test:{port}/", allow_private=True)
        assert target is not None

        with (
            pinned_requests_session(target) as session,
            pytest.raises(requests.exceptions.SSLError),
        ):
            session.get(target.url, headers=target.headers, verify=ca_bundle, timeout=5)

    def test_aiohttp_accepts_a_certificate_matching_the_pinned_hostname(
        self, tls_server, pin_to_loopback
    ):
        from app.utils.url_validation import pinned_aiohttp_session

        start, ca_bundle = tls_server
        port = start("pinned.test")

        target, _ = resolve_pinned_target(f"https://pinned.test:{port}/", allow_private=True)
        assert target is not None

        async def go() -> int:
            context = ssl.create_default_context(cafile=ca_bundle)
            async with pinned_aiohttp_session(target, timeout_seconds=5) as session:
                # `original_url`: aiohttp pins at the RESOLVER, so the request keeps its
                # hostname and derives SNI + certificate name from it with no override.
                async with session.get(target.original_url, ssl=context) as response:
                    return int(response.status)

        assert asyncio.run(go()) == 200

    def test_aiohttp_still_rejects_a_certificate_for_a_different_name(
        self, tls_server, pin_to_loopback
    ):
        import aiohttp

        from app.utils.url_validation import pinned_aiohttp_session

        start, ca_bundle = tls_server
        port = start("somebody-else.test")

        target, _ = resolve_pinned_target(f"https://pinned.test:{port}/", allow_private=True)
        assert target is not None

        async def go() -> int:
            context = ssl.create_default_context(cafile=ca_bundle)
            async with pinned_aiohttp_session(target, timeout_seconds=5) as session:
                async with session.get(target.original_url, ssl=context) as response:
                    return int(response.status)

        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            asyncio.run(go())


class TestAiohttpResolverPin:
    def test_resolver_answers_only_the_pinned_address(self, rebinding_dns):
        """aiohttp keeps the hostname, so the pin has to live in the resolver."""
        from app.utils.url_validation import pinned_aiohttp_session

        port, calls = rebinding_dns
        target, _ = resolve_pinned_target(f"http://rebind.test:{port}/", allow_private=True)
        assert target is not None

        async def go() -> dict:
            async with pinned_aiohttp_session(target, timeout_seconds=5) as session:
                async with session.get(target.original_url, allow_redirects=False) as response:
                    return dict(await response.json())

        payload = asyncio.run(go())

        assert payload["served_by"] == FIRST_ANSWER
        # aiohttp asked OUR resolver, which never calls getaddrinfo again.
        assert calls == ["rebind.test"]

    def test_resolver_refuses_a_different_hostname(self, rebinding_dns):
        """A redirect to another host must not be handed the approved address."""
        from app.utils.url_validation import pinned_aiohttp_session

        port, _ = rebinding_dns
        target, _ = resolve_pinned_target(f"http://rebind.test:{port}/", allow_private=True)
        assert target is not None

        async def go() -> None:
            async with pinned_aiohttp_session(target, timeout_seconds=5) as session:
                async with session.get(f"http://elsewhere.test:{port}/"):
                    pass

        import aiohttp

        with pytest.raises(aiohttp.ClientConnectorDNSError) as exc:
            asyncio.run(go())
        # aiohttp wraps the resolver's OSError, so the refusal is on the cause.
        assert "pinned to" in str(exc.value.os_error)


class TestRedirectsAreNotFollowed:
    """A pin covers ONE hop, so the second hop must not happen.

    Without this, an attacker needs no DNS control at all: host a public URL that passes
    validation and answer it with ``302 Location: http://169.254.169.254/``. Every client
    here follows redirects by default except httpx, so this is a real behaviour change and
    needs a real test.
    """

    def test_aiohttp_does_not_follow_a_redirect_to_metadata(self, rebinding_dns):
        from app.utils.url_validation import pinned_aiohttp_session

        port, _ = rebinding_dns
        target, _ = resolve_pinned_target(
            f"http://rebind.test:{port}/redirect-to-metadata", allow_private=True
        )
        assert target is not None

        async def go() -> tuple[int, str]:
            async with pinned_aiohttp_session(target, timeout_seconds=5) as session:
                async with session.get(target.original_url, allow_redirects=False) as response:
                    return int(response.status), str(response.headers.get("Location", ""))

        status_code, location = asyncio.run(go())

        # The 302 is returned to us, NOT chased to 169.254.169.254.
        assert status_code == 302
        assert location == "http://169.254.169.254/latest/meta-data/"

    def test_requests_does_not_follow_a_redirect_to_metadata(self, rebinding_dns):
        from app.utils.url_validation import pinned_requests_session

        port, _ = rebinding_dns
        target, _ = resolve_pinned_target(
            f"http://rebind.test:{port}/redirect-to-metadata", allow_private=True
        )
        assert target is not None

        with pinned_requests_session(target) as session:
            response = session.get(
                target.url, headers=target.headers, timeout=5, allow_redirects=False
            )

        assert response.status_code == 302
        assert response.headers["Location"] == "http://169.254.169.254/latest/meta-data/"

    def test_client_really_would_follow_it_without_the_flag(self, rebinding_dns):
        """The control: prove `allow_redirects=False` is what stops it, not luck.

        Same request, flag flipped. The client leaves our server and attempts the second
        hop — caught here only by the pinned resolver's hostname refusal, which is
        defence in depth *after* the attempt. `allow_redirects=False` prevents the attempt.

        Without this test, `allow_redirects=False` could be deleted and the two tests above
        would still pass on any server that did not actually redirect.
        """
        import aiohttp

        from app.utils.url_validation import pinned_aiohttp_session

        port, _ = rebinding_dns
        target, _ = resolve_pinned_target(
            f"http://rebind.test:{port}/redirect-elsewhere", allow_private=True
        )
        assert target is not None

        async def go(follow: bool) -> int:
            async with pinned_aiohttp_session(target, timeout_seconds=5) as session:
                async with session.get(target.original_url, allow_redirects=follow) as response:
                    return int(response.status)

        # Flag on: the client chases the Location and hits the resolver's refusal.
        with pytest.raises(aiohttp.ClientConnectorDNSError) as exc:
            asyncio.run(go(True))
        assert "elsewhere.test" in str(exc.value.os_error)

        # Flag off (what the production code passes): the 302 comes back to us untouched.
        assert asyncio.run(go(False)) == 302


# ── The shared-address-space / Alibaba IMDS gap ─────────────────────────────────────


class TestSharedAddressSpaceIsNotPublic:
    """RFC 6598 (100.64.0.0/10) was reachable in BOTH modes, not just under the flag.

    `_reject_reason` leaned on `ip.is_private`, and `ipaddress` reports 100.64.0.0/10 as
    neither private NOR global — measured identical on CPython 3.9, 3.11 and 3.12. So
    every check returned None and the range passed the strict check, despite the code
    comment claiming `is_private` "covers the shared-address space".

    Alibaba Cloud's instance metadata lives at 100.100.100.200, inside that range.
    """

    def test_alibaba_metadata_is_refused_in_strict_mode(self):
        from app.utils.url_validation import is_safe_url

        safe, reason = is_safe_url("http://100.100.100.200/latest/meta-data/")

        assert safe is False
        assert reason

    def test_alibaba_metadata_is_refused_under_allow_private(self):
        """`allow_private=True` applies ONLY the metadata carve-out, so it must list this.

        Every OIDC discovery fetch passes the flag, and so does any deployment running a
        self-hosted model server — the mode where this address is most reachable.
        """
        from app.utils.url_validation import is_safe_url

        safe, reason = is_safe_url("http://100.100.100.200/latest/meta-data/", allow_private=True)

        assert safe is False
        assert "metadata" in reason.lower()

    @pytest.mark.parametrize("address", ["100.64.0.1", "100.127.255.254"])
    def test_shared_address_space_is_refused_in_strict_mode(self, address):
        """Not just the metadata address: the whole range is not publicly routable."""
        from app.utils.url_validation import is_safe_url

        safe, reason = is_safe_url(f"http://{address}/")

        assert safe is False
        assert reason

    def test_ipv6_mapped_form_is_refused_too(self):
        """`IPv6Address('::ffff:100.100.100.200').is_global` is True — it must unmap first."""
        from app.utils.url_validation import is_safe_url

        safe, reason = is_safe_url("http://[::ffff:100.100.100.200]/", allow_private=True)

        assert safe is False
        assert "metadata" in reason.lower()

    def test_ordinary_public_addresses_still_pass(self):
        """Control: a catch-all that refuses everything would satisfy the tests above."""
        from app.utils.url_validation import is_safe_url

        for address in ("93.184.216.34", "8.8.8.8", "1.1.1.1"):
            safe, reason = is_safe_url(f"http://{address}/")
            assert safe is True, f"{address} must remain reachable: {reason}"
