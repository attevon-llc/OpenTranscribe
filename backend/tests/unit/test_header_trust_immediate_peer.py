"""Identity headers are judged by the hop that sent them, under the server the image runs.

``auth/header_trust.py`` lets a DN header stand in for a certificate only when the host that
delivered it is a configured trusted proxy (nginx). It reads ``request.client``. uvicorn's
proxy-header handling, on whenever the CLI does not say ``--no-proxy-headers``, replaces
``request.client`` with the ``X-Forwarded-For`` client before the app runs and keeps no copy
(uvicorn docs, "Proxies and Forwarded Headers"; ``uvicorn/middleware/proxy_headers.py``).
Behind a trusted nginx that is the end user, so a header-mode PKI login was judged by the
user's address and refused.

These tests start ``scripts/api-start.sh`` with a fake ``uvicorn`` to read the flags the image
really passes, apply uvicorn's own middleware exactly when those flags turn it on, and send the
request to an endpoint that runs the real PKI header check and the real client resolver.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import cookies
from app.auth.header_trust import parse_trusted_proxies
from app.auth.pki_auth import _extract_user_info_from_request
from app.utils import client_ip
from tests.unit.test_api_entrypoint import _run_script

#: The compose overlays' RATE_LIMIT_TRUSTED_PROXIES default; PKI_TRUSTED_PROXIES uses the same.
TRUSTED = "127.0.0.1/32,172.16.0.0/12"
NGINX = "172.18.0.5"
VISITOR = "203.0.113.9"
DN_HEADER = "X-Client-Cert-DN"
ADMIN_DN = "CN=pkiadmin,OU=Admins,O=OpenTranscribe"


def _probe(request: Request) -> JSONResponse:
    identity = _extract_user_info_from_request(
        request,
        None,
        None,
        networks=parse_trusted_proxies(TRUSTED),
        cert_dn_header=DN_HEADER,
    )
    return JSONResponse(
        {
            "identity": identity is not None,
            "client": client_ip.resolve_client_ip(request),
            "secure": cookies._secure_for_request(request),
        }
    )


def _uvicorn_rewrites_client(argv: list[str]) -> bool:
    """uvicorn's ``--proxy-headers`` defaults to on (uvicorn settings, "HTTP")."""
    return "--no-proxy-headers" not in argv


@pytest.fixture
def served(tmp_path, monkeypatch) -> Iterator:
    monkeypatch.setattr(client_ip, "_TRUSTED_NETWORKS", client_ip._parse_networks(TRUSTED))
    monkeypatch.setattr(cookies, "_HARDENED", True)
    monkeypatch.setattr(cookies, "_ALLOW_INSECURE", True)
    argv, _ = _run_script(tmp_path, RATE_LIMIT_TRUSTED_PROXIES=TRUSTED)
    app = Starlette(routes=[Route("/", _probe)])
    if _uvicorn_rewrites_client(argv):
        allow = (
            argv[argv.index("--forwarded-allow-ips") + 1]
            if "--forwarded-allow-ips" in argv
            else "127.0.0.1"
        )
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=allow)

    def call(peer: str, **headers: str) -> dict:
        with TestClient(app, client=(peer, 40000)) as http:
            body: dict = http.get("/", headers=headers).json()
            return body

    yield call


def test_header_mode_pki_through_trusted_nginx_is_accepted(served):
    seen = served(NGINX, **{"X-Forwarded-For": VISITOR, DN_HEADER: ADMIN_DN})

    assert seen["identity"] is True
    assert seen["client"] == VISITOR


def test_identity_headers_from_an_untrusted_peer_are_refused(served):
    seen = served("198.51.100.20", **{DN_HEADER: ADMIN_DN})

    assert seen["identity"] is False


@pytest.mark.parametrize("peer", ["192.168.1.50", "10.4.0.7", "198.51.100.20"])
def test_a_forged_forwarded_for_cannot_make_an_untrusted_peer_look_trusted(served, peer):
    seen = served(peer, **{"X-Forwarded-For": NGINX, DN_HEADER: ADMIN_DN})

    assert seen["identity"] is False
    assert seen["client"] == peer


def test_a_trusted_proxy_reports_the_scheme(served):
    assert served(NGINX, **{"X-Forwarded-Proto": "https"})["secure"] is True
    assert served(NGINX, **{"X-Forwarded-Proto": "http"})["secure"] is False


def test_an_untrusted_peer_cannot_claim_https(served):
    assert served("198.51.100.20", **{"X-Forwarded-Proto": "https"})["secure"] is False


def test_forwarded_proto_is_none_without_a_trusted_peer():
    request = Request({"type": "http", "headers": [(b"x-forwarded-proto", b"https")]})

    assert client_ip.forwarded_proto(request) is None
