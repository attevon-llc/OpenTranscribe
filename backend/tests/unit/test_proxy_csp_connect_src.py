"""The reverse-proxy CSP header must not allow WebSockets to any host (issue #1028).

``nginx/site.conf.template`` sends its own ``Content-Security-Policy`` header on top of the
SPA's ``<meta>`` policy. A bare ``ws:``/``wss:`` (or ``http:``/``https:``) source allows a
connection to ANY host; ``'self'`` already covers same-host ws:/wss: in CSP Level 3. The SPA's
meta policy is pinned separately by ``frontend/src/csp.test.ts`` and the ``postbuild`` check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEMPLATE = Path(__file__).resolve().parents[3] / "nginx" / "site.conf.template"
BARE_SCHEMES = {"ws:", "wss:", "http:", "https:"}


def _csp_policies() -> list[str]:
    text = re.sub(r"#[^\n]*", "", TEMPLATE.read_text(encoding="utf-8"))
    return re.findall(r'add_header\s+Content-Security-Policy\s+"([^"]*)"', text)


def _directives(policy: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0].lower()] = tokens[1:]
    return out


def test_template_sends_exactly_one_csp_header():
    """Guard the guard: the assertions below need a policy to look at."""
    assert len(_csp_policies()) == 1


def test_connect_src_is_self_only():
    """RED before the fix: connect-src was `'self' ws: wss:`."""
    (policy,) = _csp_policies()
    assert _directives(policy)["connect-src"] == ["'self'"]


def test_no_directive_carries_a_bare_network_scheme():
    (policy,) = _csp_policies()
    offenders = [
        f"{name} {src}"
        for name, sources in _directives(policy).items()
        for src in sources
        if src.lower() in BARE_SCHEMES
    ]
    assert offenders == []
