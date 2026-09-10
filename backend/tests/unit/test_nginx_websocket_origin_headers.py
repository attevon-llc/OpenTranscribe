"""The /api/ws proxy must forward the origin the BROWSER addressed (issue #903).

``api/websockets._origin_is_allowed`` refuses a cross-origin WebSocket handshake.
Because ``CORS_ORIGINS`` is set by no shipped compose file — it defaults to the two
Vite dev URLs — every production deployment is admitted by the **same-origin
fallback** instead: ``_request_origin()`` rebuilds the address the handshake was
sent to from ``Host`` + ``X-Forwarded-Proto`` and compares it to ``Origin``.

That comparison is only as good as what nginx forwards, and as shipped it was wrong
in two independent ways, in every production mode:

1. **``proxy_set_header Host $host`` DROPS THE PORT.** nginx's ``$host`` is the host
   *name* only; ``$http_host`` is the verbatim ``Host`` header including ``:port``.
   A browser at ``https://box:5182`` sends ``Origin: https://box:5182`` while the
   backend reconstructs ``https://box`` — different origins, handshake refused.
   The shipped defaults put every non-nginx-overlay mode on a port:
   ``FRONTEND_PORT=5173`` and ``PKI_HTTPS_PORT=5182``.

2. **``proxy_set_header`` IS AN ARRAY DIRECTIVE.** A ``location`` that declares *any*
   ``proxy_set_header`` inherits *none* from the enclosing ``server``. So
   ``nginx/site.conf.template``'s ``/api/ws`` block, which declares ``Upgrade`` and
   ``Connection``, silently discarded the server-level ``Host`` /
   ``X-Forwarded-Proto`` / ``X-Real-IP`` / ``X-Forwarded-For`` / ``X-Forwarded-Host``
   — the backend saw nginx's ``$proxy_host`` default (``backend:8080``) and no
   forwarded scheme. Dropping ``X-Real-IP``/``X-Forwarded-For`` also let the
   client's own forged copies through on that one path.

Neither is visible to ``test_ws_origin_gate.py`` (which exercises the gate's logic
with synthetic headers) nor to the e2e suite (which browses ``http://localhost:5173``
— one of the two origins the *default allowlist* rescues, so the fallback is never
the thing under test). Static, for the same reason ``test_nginx_websocket_timeouts.py``
is static: the dynamic proof needs a running prod/PKI stack and a non-loopback origin.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_nginx_websocket_timeouts import _server_blocks
from tests.unit.test_nginx_websocket_timeouts import _ws_location_blocks

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: nginx variables that preserve the client's ``Host`` header verbatim, port included.
#: ``$host`` is deliberately NOT here — it is the shipped bug this file pins.
PORT_PRESERVING_HOST_VARS = ("$http_host", "$host:$server_port")


def _ws_blocks_of(relative_path: str) -> list[tuple[str, str]]:
    """Every ``/api/ws`` location body in a config, tagged with a location string."""
    path = REPO_ROOT / relative_path
    text = path.read_text(encoding="utf-8")
    servers = _server_blocks(text)
    assert servers, f"{relative_path}: no server block found — the parser is not reading this file"
    blocks = [
        (f"{relative_path} (server block {i})", body)
        for i, server in enumerate(servers)
        for body in _ws_location_blocks(server)
    ]
    assert blocks, (
        f"{relative_path}: no `location /api/ws` block — a WebSocket request falls through "
        f"to the generic /api/ prefix block and the handshake fails outright"
    )
    return blocks


def _host_header_value(body: str) -> str | None:
    match = re.search(r"proxy_set_header\s+Host\s+(\S+?)\s*;", body)
    return match.group(1) if match else None


#: Every shipped config that proxies /api/ws, and how many such blocks it must have.
#: Named explicitly rather than globbed: a config that stops matching the glob would
#: silently drop out of this gate, which is the failure mode being guarded against.
WS_CONFIGS = (
    ("frontend/nginx.conf", 1),
    ("frontend/nginx-pki.conf", 2),
    ("scripts/pki/nginx-pki-dev.conf", 1),
    ("nginx/site.conf.template", 1),
)


@pytest.mark.parametrize(("relative_path", "expected_blocks"), WS_CONFIGS)
def test_every_shipped_ws_config_is_actually_read(relative_path: str, expected_blocks: int):
    """Guard the guard: a parser that finds no block asserts nothing about it."""
    blocks = _ws_blocks_of(relative_path)
    assert len(blocks) == expected_blocks, (
        f"{relative_path}: expected {expected_blocks} /api/ws location(s), found {len(blocks)} — "
        f"the config's shape moved and the assertions below may be checking the wrong block"
    )


@pytest.mark.parametrize(("relative_path", "_expected"), WS_CONFIGS)
def test_ws_block_forwards_a_host_header_that_keeps_the_port(relative_path: str, _expected: int):
    """``$host`` strips ``:port``, so the same-origin fallback can never match a
    browser Origin that carries one — and the shipped ports (5173, 5182) all do."""
    blocks = _ws_blocks_of(relative_path)
    assert blocks, f"{relative_path}: nothing to assert against — an empty loop passes"
    for where, body in blocks:
        value = _host_header_value(body)
        assert value is not None, (
            f"{where}: /api/ws sets no `proxy_set_header Host` of its own. proxy_set_header "
            f"is an ARRAY directive — declaring Upgrade/Connection here discards every "
            f"server-level header, so the backend sees nginx's $proxy_host default "
            f"(backend:8080) and refuses the handshake as cross-origin."
        )
        assert value in PORT_PRESERVING_HOST_VARS, (
            f"{where}: /api/ws forwards `Host {value}`, which drops the port. "
            f"_request_origin() then rebuilds e.g. https://box instead of https://box:5182 "
            f"and _origin_is_allowed() refuses a legitimate same-origin handshake. "
            f"Use one of {PORT_PRESERVING_HOST_VARS}."
        )


@pytest.mark.parametrize(("relative_path", "_expected"), WS_CONFIGS)
def test_ws_block_forwards_the_client_facing_scheme(relative_path: str, _expected: int):
    """Without X-Forwarded-Proto the backend falls back to the *connection's* scheme,
    which behind TLS termination is ``http`` while the browser's Origin says ``https``."""
    blocks = _ws_blocks_of(relative_path)
    assert blocks, f"{relative_path}: nothing to assert against — an empty loop passes"
    for where, body in blocks:
        assert re.search(r"proxy_set_header\s+X-Forwarded-Proto\s+\$scheme\s*;", body), (
            f"{where}: /api/ws sets no `proxy_set_header X-Forwarded-Proto $scheme`. "
            f"The same-origin fallback would compare an http:// reconstruction against an "
            f"https:// Origin and refuse it."
        )


@pytest.mark.parametrize(("relative_path", "_expected"), WS_CONFIGS)
def test_ws_block_does_not_pass_the_clients_own_forwarding_headers_through(
    relative_path: str, _expected: int
):
    """A location that overrides proxy_set_header inherits none, so it must restate
    X-Real-IP and X-Forwarded-For itself — otherwise the CLIENT's forged values reach
    the backend verbatim on this one path, and rate limiting reads an attacker's number."""
    blocks = _ws_blocks_of(relative_path)
    assert blocks, f"{relative_path}: nothing to assert against — an empty loop passes"
    for where, body in blocks:
        assert re.search(r"proxy_set_header\s+X-Real-IP\s+\$remote_addr\s*;", body), (
            f"{where}: /api/ws does not set X-Real-IP — the client's own header passes through"
        )
        assert re.search(
            r"proxy_set_header\s+X-Forwarded-For\s+\$proxy_add_x_forwarded_for\s*;", body
        ), f"{where}: /api/ws does not set X-Forwarded-For from $proxy_add_x_forwarded_for"
