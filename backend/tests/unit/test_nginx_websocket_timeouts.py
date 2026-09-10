"""The /api/ws WebSocket proxy must not inherit a 300s idle timeout (issue #846).

An idle WebSocket sends zero bytes in either direction (no heartbeat exists on
either side -- see the issue body). nginx's server-level `proxy_read_timeout 300s`
(set for large file uploads) therefore silently closes any WebSocket connection
that sits idle for 300 seconds -- which is exactly the gap between coarse
progress updates on a long transcription. The fix is a per-location override, not
a server-level bump (that would also apply to the MinIO streaming/upload blocks
and hide real upstream hangs there).

This file enumerates every nginx config that ships (or documents) an `/api/ws`
proxy, and for the SERVER block(s) each one defines, asserts:
  1. an `/api/ws` location block actually exists (its ABSENCE, in
     scripts/pki/nginx-pki-dev.conf, is itself the #846-adjacent bug -- a
     request there falls through to the generic /api/ prefix block, which sets
     no proxy_http_version/Upgrade headers at all, so the handshake fails
     outright rather than merely timing out), and
  2. that block declares proxy_read_timeout and proxy_send_timeout at >= 86400s
     ITSELF -- inheriting from the server block is exactly the bug -- and
     proxy_http_version 1.1 plus both upgrade headers.

Static only: a 300s idle-timeout is invisible to any test that finishes in
under 300s, which is why this shipped. The dynamic proof (connect, send
nothing, watch it drop at ~300s pre-fix / survive post-fix) is documented as a
manual verification step in the PR, not encoded here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

MIN_WS_TIMEOUT_SECONDS = 86400


def _find_matching_brace(text: str, open_brace_index: int) -> int:
    """Return the index of the `{`'s matching `}` (simple depth counter)."""
    depth = 0
    for i in range(open_brace_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError(f"unbalanced braces starting at index {open_brace_index}")


def _extract_blocks(text: str, opener_pattern: str) -> list[tuple[str, str]]:
    """Find every `<opener> {...}` block, brace-matched (not first-match-only).

    Returns a list of (header_text, body_text) pairs in document order. Handles
    nested braces correctly and does not stop at the first match -- required
    because nginx-pki.conf has two `server {` blocks and two `location /api/ws {`
    blocks (one per server), and a first-match parser would validate the first
    and silently never see the second.
    """
    blocks: list[tuple[str, str]] = []
    for match in re.finditer(opener_pattern, text, re.MULTILINE):
        open_brace = text.index("{", match.end() - 1)
        close_brace = _find_matching_brace(text, open_brace)
        blocks.append((match.group(0), text[open_brace + 1 : close_brace]))
    return blocks


def _server_blocks(text: str) -> list[str]:
    """Body text of every top-level `server { ... }` block."""
    return [body for _header, body in _extract_blocks(text, r"^\s*server\s*\{")]


def _ws_location_blocks(server_body: str) -> list[str]:
    """Body text of every `location /api/ws { ... }` block inside a server body.

    Matched on the exact literal path `/api/ws` (with an optional trailing
    space/tab before `{`) so a broader prefix location (e.g. `/api/`) is never
    mistaken for the WebSocket-specific one.
    """
    return [body for _header, body in _extract_blocks(server_body, r"location\s+/api/ws\s*\{")]


def _assert_ws_block_sets_long_timeouts_and_upgrade(body: str, where: str) -> None:
    read_match = re.search(r"proxy_read_timeout\s+(\d+)s\s*;", body)
    assert read_match, f"{where}: /api/ws block declares no proxy_read_timeout of its own"
    assert int(read_match.group(1)) >= MIN_WS_TIMEOUT_SECONDS, (
        f"{where}: /api/ws proxy_read_timeout is {read_match.group(1)}s, "
        f"needs >= {MIN_WS_TIMEOUT_SECONDS}s"
    )

    send_match = re.search(r"proxy_send_timeout\s+(\d+)s\s*;", body)
    assert send_match, f"{where}: /api/ws block declares no proxy_send_timeout of its own"
    assert int(send_match.group(1)) >= MIN_WS_TIMEOUT_SECONDS, (
        f"{where}: /api/ws proxy_send_timeout is {send_match.group(1)}s, "
        f"needs >= {MIN_WS_TIMEOUT_SECONDS}s"
    )

    assert re.search(r"proxy_http_version\s+1\.1\s*;", body), (
        f"{where}: /api/ws block sets no proxy_http_version 1.1 -- required to carry the "
        f"Upgrade handshake at all"
    )
    assert re.search(r"proxy_set_header\s+Upgrade\s+\$http_upgrade\s*;", body), (
        f"{where}: /api/ws block sets no Upgrade header"
    )
    assert re.search(r'proxy_set_header\s+Connection\s+["\']?[Uu]pgrade["\']?\s*;', body), (
        f"{where}: /api/ws block sets no Connection: upgrade header"
    )


def test_frontend_nginx_conf_ws_block_sets_its_own_long_timeouts():
    path = REPO_ROOT / "frontend" / "nginx.conf"
    servers = _server_blocks(path.read_text(encoding="utf-8"))
    assert len(servers) == 1, f"expected exactly one server block in {path}, found {len(servers)}"
    ws_blocks = _ws_location_blocks(servers[0])
    assert len(ws_blocks) == 1, f"expected exactly one /api/ws location in {path}"
    _assert_ws_block_sets_long_timeouts_and_upgrade(ws_blocks[0], str(path))


def _nginx_pki_conf_server_blocks() -> list[str]:
    path = REPO_ROOT / "frontend" / "nginx-pki.conf"
    servers = _server_blocks(path.read_text(encoding="utf-8"))
    assert len(servers) == 2, f"expected exactly two server blocks in {path}, found {len(servers)}"
    return servers


def test_frontend_nginx_pki_conf_plain_http_server_block_sets_its_own_long_timeouts():
    """nginx-pki.conf's FIRST server block (listen 8080, plain HTTP)."""
    path = REPO_ROOT / "frontend" / "nginx-pki.conf"
    server_body = _nginx_pki_conf_server_blocks()[0]
    ws_blocks = _ws_location_blocks(server_body)
    assert len(ws_blocks) == 1, (
        f"expected exactly one /api/ws location in the :8080 server of {path}"
    )
    _assert_ws_block_sets_long_timeouts_and_upgrade(ws_blocks[0], f"{path} (:8080 server block)")


def test_frontend_nginx_pki_conf_mtls_server_block_sets_its_own_long_timeouts():
    """nginx-pki.conf's SECOND server block (listen 8443 ssl, mTLS). A parser that
    stops at the first /api/ws match never sees this one -- that is the trap."""
    path = REPO_ROOT / "frontend" / "nginx-pki.conf"
    server_body = _nginx_pki_conf_server_blocks()[1]
    ws_blocks = _ws_location_blocks(server_body)
    assert len(ws_blocks) == 1, (
        f"expected exactly one /api/ws location in the :8443 server of {path}"
    )
    _assert_ws_block_sets_long_timeouts_and_upgrade(ws_blocks[0], f"{path} (:8443 server block)")


def test_pki_dev_nginx_conf_has_an_api_ws_block_at_all():
    """scripts/pki/nginx-pki-dev.conf has historically defined NO /api/ws location,
    so a WebSocket request there falls through to the generic /api/ prefix block
    (no proxy_http_version, no Upgrade headers) and the handshake fails outright.
    An absent block cannot be found by scanning for a present one -- this test
    must fail LOUDLY on the missing-block case, not silently pass by finding
    nothing to check."""
    path = REPO_ROOT / "scripts" / "pki" / "nginx-pki-dev.conf"
    text = path.read_text(encoding="utf-8")
    servers = _server_blocks(text)
    assert len(servers) == 1, f"expected exactly one server block in {path}, found {len(servers)}"

    ws_blocks = _ws_location_blocks(servers[0])
    assert ws_blocks, (
        f"{path} defines no `location /api/ws {{ ... }}` block -- a WebSocket request "
        f"there prefix-matches the generic /api/ block, which sets no proxy_http_version "
        f"or Upgrade headers, so the handshake fails outright"
    )
    _assert_ws_block_sets_long_timeouts_and_upgrade(ws_blocks[0], str(path))

    # Issue #620: this listener does no mTLS, so a client-claimed cert header must
    # never reach the backend through the WebSocket path either -- mirroring the
    # /api/ block's own three drops immediately above it in this same file.
    for header in ("X-Client-Cert", "X-Client-Cert-Verify", "X-Client-Cert-DN"):
        assert re.search(rf'proxy_set_header\s+{header}\s+""\s*;', ws_blocks[0]), (
            f"{path}: /api/ws block does not drop {header} -- a forged cert header "
            f"could reach the backend over the WebSocket path"
        )


def test_nginx_site_conf_template_ws_block_already_sets_its_own_long_timeouts():
    """The reference implementation (a separate, optional front-nginx overlay) --
    already correct, kept here as the standard the other three configs are held to.

    This template has TWO server blocks (an HTTP :80 -> HTTPS redirect, and the
    real HTTPS :443 server) -- only the latter carries /api/ws, so this asserts
    the union across server blocks holds exactly one, rather than assuming a
    single server block like the plain frontend configs.
    """
    path = REPO_ROOT / "nginx" / "site.conf.template"
    servers = _server_blocks(path.read_text(encoding="utf-8"))
    assert len(servers) == 2, f"expected exactly two server blocks in {path}, found {len(servers)}"
    ws_blocks = [block for server in servers for block in _ws_location_blocks(server)]
    assert len(ws_blocks) == 1, f"expected exactly one /api/ws location in {path}"
    _assert_ws_block_sets_long_timeouts_and_upgrade(ws_blocks[0], str(path))
