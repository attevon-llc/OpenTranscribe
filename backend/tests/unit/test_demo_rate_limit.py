"""The public demo's per-client-IP ceiling on every route, behind a proxy (#628, #668).

On the demo the auth routes' own limits are not enough: every visitor reaches the read
surface, which has no per-route limit. ``app/api/rate_limit.py`` gives the limiter a
default limit only when ``DEMO_MODE`` is on, and ``app/main.py`` installs slowapi's
``SlowAPIASGIMiddleware`` (slowapi docs: default limits apply to undecorated routes only
through the middleware) only then. These tests drive the real limiter factory and the real
key function (``utils/client_ip.resolve_client_ip``) through that middleware:

* two clients behind a trusted proxy get independent buckets;
* a client that is not a trusted proxy cannot pick a bucket by forging ``X-Forwarded-For``;
* outside demo mode there is no default limit and no middleware.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi import Request
from fastapi import Response
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware

from app.core.config import settings

BACKEND = Path(__file__).resolve().parents[2]
PROXY = ("172.20.0.5", 40000)  # the nginx container
CEILING = 3


@pytest.fixture
def demo_app(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    monkeypatch.setattr(settings, "DEMO_RATE_LIMIT_PER_MINUTE", CEILING)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", "memory://")
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXIES", "172.20.0.0/16")
    importlib.reload(importlib.import_module("app.utils.client_ip"))
    from app.api import rate_limit

    limiter = rate_limit._create_limiter()
    monkeypatch.setattr(rate_limit, "limiter", limiter)

    app = FastAPI()
    app.state.limiter = limiter

    def on_limit(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RateLimitExceeded)
        return rate_limit.rate_limit_exceeded_handler(request, exc)

    app.add_exception_handler(RateLimitExceeded, on_limit)
    app.add_middleware(SlowAPIASGIMiddleware)

    @app.get("/api/files")
    def files(request: Request) -> dict:
        return {"ok": True}

    yield app
    monkeypatch.undo()
    importlib.reload(importlib.import_module("app.utils.client_ip"))


def _statuses(client: TestClient, n: int, forwarded_for: str | None = None) -> list[int]:
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    return [client.get("/api/files", headers=headers).status_code for _ in range(n)]


def test_each_client_behind_the_proxy_has_its_own_ceiling(demo_app):
    via_proxy = TestClient(demo_app, client=PROXY)

    assert _statuses(via_proxy, CEILING + 1, "203.0.113.10") == [200] * CEILING + [429]
    assert _statuses(via_proxy, 1, "203.0.113.20") == [200]


def test_an_untrusted_client_cannot_forge_a_fresh_bucket(demo_app):
    direct = TestClient(demo_app, client=("198.51.100.7", 40000))

    statuses = [
        direct.get("/api/files", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
        for i in range(CEILING + 1)
    ]

    assert statuses == [200] * CEILING + [429]


def test_outside_demo_mode_there_is_no_default_limit(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    from app.api import rate_limit

    assert rate_limit.demo_default_limits() == []


def _middleware_of_main(demo: str) -> str:
    env = {**os.environ, "DEMO_MODE": demo}
    code = (
        "from app.main import app; "
        "print(sorted(m.cls.__name__ for m in app.user_middleware)); "
        "print(app.state.limiter._default_limits != [])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return proc.stdout


@pytest.mark.parametrize(("demo", "installed"), [("true", True), ("false", False)])
def test_main_installs_the_middleware_and_default_limit_only_on_the_demo(demo, installed):
    out = _middleware_of_main(demo)

    assert ("SlowAPIASGIMiddleware" in out) is installed
    assert out.strip().endswith(str(installed))
