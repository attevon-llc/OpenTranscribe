"""The frontend dev server must answer its two entry routes with 200 HTML, not a 500.

A SvelteKit dev server can come up healthy (the container healthcheck only needs a socket)
while server-side rendering of a route throws, and every browser test then fails with a
blank page that reads like a broken selector. A plain HTTP GET of ``/`` and ``/login`` on a
fresh stack separates "the shell does not render" from "the test is wrong" in under a second.
No browser, no login, no data written.
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("path", ["/", "/login"])
def test_entry_route_renders_html_not_an_error(base_url: str, path: str) -> None:
    response = requests.get(base_url.rstrip("/") + path, timeout=60)

    assert response.status_code == 200, (
        f"GET {path} -> {response.status_code}; a dev-server SSR failure, not a test problem. "
        f"Body starts: {response.text[:300]!r}"
    )
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text.lower()
    assert "<html" in body, f"GET {path} returned 200 without an HTML document"
    assert "internal server error" not in body
