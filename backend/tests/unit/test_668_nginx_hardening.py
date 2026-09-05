"""Issue #668 — nginx/reverse-proxy hardening.

Verified against `master` before making any change here (see the branch's PR description
for the full write-up):

* **Finding 1** (`RATE_LIMIT_TRUSTED_PROXIES` empty behind nginx collapses every per-IP
  rate limit into one global bucket) is **already fixed on master**. Every proxy-fronting
  compose overlay (`docker-compose.nginx.yml`, `.prod.yml`, `.pki.yml`, `.pki-dev.yml`)
  ships a coded `${RATE_LIMIT_TRUSTED_PROXIES:-127.0.0.1/32,172.16.0.0/12}` default
  (`backend/tests/unit/test_proxy_trust_overlays.py` pins this), and
  `app/utils/client_ip.resolve_client_ip` fails closed with no trusted proxy configured
  (`backend/tests/unit/test_client_ip_resolution.py`). This file adds the one thing that
  was still missing: an *empirical* test that two distinct client IPs behind a trusted
  proxy land in independent rate-limit buckets, per the issue's own instruction not to
  trust a config-file read for this class of bug.
* **Finding 2** (nginx buffers the full request body before auth can reject it) is
  **already fixed on master** — `nginx/site.conf.template` sets
  `proxy_request_buffering off` on both `location /api/` (the upload path) and
  `location /s3/` (direct MinIO uploads). Pinned below so a future edit cannot drop it
  silently the way the issue warned a server-level-only fix would.
* **Finding 3** (no `robots.txt`, no `X-Robots-Tag` on API responses) was **not** fixed —
  this is the actual change on this branch: `frontend/static/robots.txt` (served through
  nginx's catch-all `location /` in every proxy topology, and directly by Vite in dev) and
  `app.middleware.robots.RobotsHeaderMiddleware`.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]


def _request(peer: str, **headers) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers={k.replace("_", "-"): v for k, v in headers.items()},
    )


@pytest.fixture(autouse=True)
def _restore_resolver_module():
    yield
    import app.utils.client_ip as client_ip

    importlib.reload(client_ip)


def _module_with_proxies(monkeypatch, trusted: str):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXIES", trusted)
    import app.utils.client_ip as client_ip

    return importlib.reload(client_ip)


# ─── Finding 1: empirically, not by reading config ───────────────────────────────


def test_two_clients_behind_a_trusted_proxy_get_independent_rate_limit_keys(monkeypatch):
    """The exact empirical check issue #668 asked for.

    Two browsers behind the same nginx (same socket peer, the proxy's own address)
    sending different `X-Forwarded-For` values must resolve to two different rate-limit
    keys. Reading `resolve_client_ip` is what the key function actually calls — this is
    not a re-implementation, it is the real dependency the limiter is built on
    (`app/auth/rate_limit.py::_get_key_func`).
    """
    mod = _module_with_proxies(monkeypatch, "172.20.0.0/16")  # the nginx container's subnet

    client_a = _request("172.20.0.5", X_Forwarded_For="203.0.113.10")
    client_b = _request("172.20.0.5", X_Forwarded_For="203.0.113.20")

    key_a = mod.resolve_client_ip(client_a)
    key_b = mod.resolve_client_ip(client_b)

    assert key_a == "203.0.113.10"
    assert key_b == "203.0.113.20"
    assert key_a != key_b, (
        "two distinct clients behind the proxy collapsed onto the same rate-limit bucket"
    )


def test_an_untrusted_direct_client_cannot_assert_its_own_bucket(monkeypatch):
    """The other half of the same empirical check: a client with no proxy in front of it
    (or one that reaches the backend port directly, bypassing nginx) must not be able to
    pick its own rate-limit identity by sending a forged header."""
    mod = _module_with_proxies(monkeypatch, "172.20.0.0/16")

    attacker = _request("203.0.113.99", X_Forwarded_For="10.10.10.10")

    assert mod.resolve_client_ip(attacker) == "203.0.113.99"


def test_rate_limiter_key_func_is_the_same_resolver_under_test(monkeypatch):
    """Guards the test above against drifting from the real limiter: if `_get_key_func`
    stopped calling `resolve_client_ip`, the two tests above would still pass while
    testing nothing about the actual rate limiter."""
    import inspect

    from app.auth import rate_limit

    assert "resolve_client_ip" in inspect.getsource(rate_limit._get_key_func)


# ─── Finding 2: nginx body-buffering pin ──────────────────────────────────────────


def test_upload_locations_disable_request_buffering():
    """A server-level `client_max_body_size` alone is not enough — it must be paired with
    `proxy_request_buffering off` on every location that accepts a large body, or nginx
    buffers the whole request (up to 15G) before the app's auth layer ever runs."""
    template = (REPO_ROOT / "nginx" / "site.conf.template").read_text(encoding="utf-8")

    for location in ("location /api/ {", "location /s3/ {"):
        start = template.index(location)
        # The location block; next "    }" at the same 4-space indent closes it.
        end = template.index("\n    }", start)
        block = template[start:end]
        assert "proxy_request_buffering off;" in block, (
            f"{location} accepts large uploads but does not disable request buffering "
            "— an unauthenticated POST can fill nginx's buffer before FastAPI sees it"
        )


# ─── Finding 3: robots.txt + X-Robots-Tag (the actual fix on this branch) ─────────


def test_robots_txt_disallows_api_and_media_paths():
    robots = (REPO_ROOT / "frontend" / "static" / "robots.txt").read_text(encoding="utf-8")

    assert "User-agent: *" in robots
    for path in ("/api/", "/files/", "/s3/", "/minio/", "/flower/"):
        assert f"Disallow: {path}" in robots, f"robots.txt does not disallow {path}"


def _tiny_app() -> Starlette:
    from app.middleware.robots import RobotsHeaderMiddleware

    async def api_endpoint(request: Request):
        return PlainTextResponse("ok")

    async def spa_endpoint(request: Request):
        return PlainTextResponse("<html></html>")

    app = Starlette(
        routes=[
            Route("/api/health", api_endpoint),
            Route("/", spa_endpoint),
        ]
    )
    app.add_middleware(RobotsHeaderMiddleware)
    return app


def test_x_robots_tag_present_on_api_responses():
    client = TestClient(_tiny_app())
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.headers.get("X-Robots-Tag") == "noindex, nofollow"


def test_x_robots_tag_absent_outside_the_api_prefix():
    """The SPA and `/docs/` must stay indexable for a deliberately public instance
    (issue #628's demo) — the header is scoped to `API_PREFIX` only."""
    client = TestClient(_tiny_app())
    response = client.get("/")

    assert response.status_code == 200
    assert "X-Robots-Tag" not in response.headers


def test_robots_middleware_is_registered_on_the_real_app():
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert "RobotsHeaderMiddleware" in source
