"""Frontend nginx: COOP/CORP on every response, one Cache-Control per response (issue #1030).

``add_header`` is an ARRAY directive: a ``location`` that declares any ``add_header`` of its
own inherits NONE from the enclosing ``server``. So a header is only on every response if it
is declared in the server block AND in every location that has its own ``add_header`` group —
which is exactly how the static-asset and HTML locations already restate the OWASP set.

Pinned here, per ``add_header`` group:

* ``Cross-Origin-Opener-Policy: same-origin`` (severs opener handles to cross-origin windows)
  and ``Cross-Origin-Resource-Policy: same-origin`` (stops other origins embedding the SPA's
  documents and assets) — both were missing everywhere.
* No ``Cross-Origin-Embedder-Policy``: ``require-corp`` breaks presigned object-storage media
  and thumbnails, so its absence is deliberate, not an oversight.
* At most one ``Cache-Control``. ``expires`` emits its own ``Cache-Control`` beside an explicit
  ``add_header Cache-Control``, so HTML went out with two (``no-cache`` and ``no-store, ...``)
  and static assets with two (``max-age=31536000`` and ``public, no-transform``).

Static, like ``test_nginx_websocket_timeouts.py``; the live header check against a running
container is in the PR's verification notes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.test_nginx_websocket_timeouts import _extract_blocks
from tests.unit.test_nginx_websocket_timeouts import _find_matching_brace
from tests.unit.test_nginx_websocket_timeouts import _server_blocks

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every frontend-image nginx config, with its server-block count. Named, not globbed,
#: so a config that stops matching cannot silently drop out of the gate.
CONFIGS = (("frontend/nginx.conf", 1), ("frontend/nginx-pki.conf", 2))

#: Headers every add_header group must carry, with their exact values.
REQUIRED = {
    "cross-origin-opener-policy": '"same-origin"',
    "cross-origin-resource-policy": '"same-origin"',
    "x-content-type-options": '"nosniff"',
    "x-frame-options": '"SAMEORIGIN"',
}

_LOCATION_OPENER = r"^\s*location\b[^{;]*\{"


def _strip_comments(text: str) -> str:
    return re.sub(r"#[^\n]*", "", text)


def _without_locations(server_body: str) -> str:
    """The server block's own directives, with every nested location body removed."""
    out, pos = [], 0
    for match in re.finditer(_LOCATION_OPENER, server_body, re.MULTILINE):
        if match.start() < pos:
            continue
        open_brace = server_body.index("{", match.end() - 1)
        out.append(server_body[pos : match.start()])
        pos = _find_matching_brace(server_body, open_brace) + 1
    out.append(server_body[pos:])
    return "".join(out)


def _add_headers(body: str) -> list[tuple[str, str]]:
    return [
        (name.lower(), value)
        for name, value in re.findall(r"^\s*add_header\s+(\S+)\s+(\"[^\"]*\"|\S+)", body, re.M)
    ]


def _header_groups(relative_path: str) -> list[tuple[str, str]]:
    """``(where, body)`` for every block that declares its own add_header group."""
    text = _strip_comments((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    groups = []
    for i, server in enumerate(_server_blocks(text)):
        groups.append((f"{relative_path} server {i}", _without_locations(server)))
        for header, body in _extract_blocks(server, _LOCATION_OPENER):
            if _add_headers(body):
                groups.append((f"{relative_path} server {i} `{header.strip()}`", body))
    return groups


@pytest.mark.parametrize(("relative_path", "servers"), CONFIGS)
def test_every_config_is_actually_parsed(relative_path: str, servers: int):
    """Guard the guard: 3 add_header groups per server (server, assets, HTML)."""
    text = _strip_comments((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    assert len(_server_blocks(text)) == servers
    assert len(_header_groups(relative_path)) == 3 * servers


@pytest.mark.parametrize(("relative_path", "_servers"), CONFIGS)
def test_every_header_group_sends_coop_and_corp(relative_path: str, _servers: int):
    """RED before the fix: no group declared either header."""
    groups = _header_groups(relative_path)
    assert groups, f"{relative_path}: no add_header group found"
    for where, body in groups:
        headers = dict(_add_headers(body))
        for name, value in REQUIRED.items():
            assert headers.get(name) == value, (
                f"{where}: `{name}` is {headers.get(name)!r}, expected {value}. A location "
                f"with its own add_header inherits none from the server block."
            )


@pytest.mark.parametrize(("relative_path", "_servers"), CONFIGS)
def test_no_config_sends_coep(relative_path: str, _servers: int):
    """COEP require-corp would block presigned media and thumbnails — keep it out."""
    text = _strip_comments((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    assert "cross-origin-embedder-policy" not in text.lower()


@pytest.mark.parametrize(("relative_path", "_servers"), CONFIGS)
def test_at_most_one_cache_control_per_group(relative_path: str, _servers: int):
    """RED before the fix: `expires` plus an explicit Cache-Control emitted two headers."""
    groups = _header_groups(relative_path)
    assert groups, f"{relative_path}: no add_header group found"
    for where, body in groups:
        explicit = [v for n, v in _add_headers(body) if n == "cache-control"]
        uses_expires = re.search(r"^\s*expires\s", body, re.M) is not None
        assert len(explicit) + int(uses_expires) <= 1, (
            f"{where}: {len(explicit)} explicit Cache-Control header(s) plus "
            f"{'an' if uses_expires else 'no'} `expires` directive (which emits its own)."
        )


@pytest.mark.parametrize(("relative_path", "_servers"), CONFIGS)
def test_html_is_not_cached_and_assets_are(relative_path: str, _servers: int):
    """Removing `expires` must not change what the caches are told."""
    groups = _header_groups(relative_path)
    html = [b for w, b in groups if r"\.html$" in w]
    assets = [b for w, b in groups if r"\.(js|css" in w]
    assert html, f"{relative_path}: no HTML location found"
    assert assets, f"{relative_path}: no static-asset location found"
    for body in html:
        assert "no-store" in dict(_add_headers(body))["cache-control"]
    for body in assets:
        assert "max-age=31536000" in dict(_add_headers(body))["cache-control"]
