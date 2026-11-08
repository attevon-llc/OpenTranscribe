"""Past its deadline, a request issues no more SQL (scale_foundations_plan.md 2.4.6).

#1337 M2 #5: API work kept running, and holding pool connections, for 120-292 s after
clients had given up at 60 s, as a stream of short statements that neither
``statement_timeout`` nor the idle-in-transaction backstop could see.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.config import settings
from app.db.base import engine as app_engine

pytestmark = pytest.mark.unit

_PROBE_GUC = "opentranscribe.deadline_probe"


def test_a_statement_past_the_deadline_never_reaches_postgres():
    from app.db.request_deadline import RequestDeadlineExceededError
    from app.db.request_deadline import reset_request_deadline
    from app.db.request_deadline import set_request_deadline

    marker = uuid.uuid4().hex
    with app_engine.connect() as conn:
        token = set_request_deadline(-1.0)
        try:
            with pytest.raises(RequestDeadlineExceededError):
                conn.execute(text(f"SELECT set_config('{_PROBE_GUC}', '{marker}', false)"))
        finally:
            reset_request_deadline(token)
        conn.rollback()
        seen = conn.execute(text(f"SELECT current_setting('{_PROBE_GUC}', true)")).scalar()

    assert seen != marker


def test_a_statement_before_the_deadline_runs():
    """Control for the test above: same statement, deadline still ahead."""
    from app.db.request_deadline import reset_request_deadline
    from app.db.request_deadline import set_request_deadline

    marker = uuid.uuid4().hex
    with app_engine.connect() as conn:
        token = set_request_deadline(60.0)
        try:
            conn.execute(text(f"SELECT set_config('{_PROBE_GUC}', '{marker}', false)"))
        finally:
            reset_request_deadline(token)
        seen = conn.execute(text(f"SELECT current_setting('{_PROBE_GUC}', true)")).scalar()
        conn.rollback()

    assert seen == marker


def test_the_deadline_is_visible_inside_a_worker_thread():
    """Sync handlers and run_in_threadpool run on anyio threads, which copy the context."""
    from app.db.request_deadline import _deadline
    from app.db.request_deadline import reset_request_deadline
    from app.db.request_deadline import set_request_deadline

    async def scenario() -> tuple[float | None, float | None]:
        token = set_request_deadline(30.0)
        try:
            return _deadline.get(), await run_in_threadpool(_deadline.get)
        finally:
            reset_request_deadline(token)

    in_loop, in_thread = asyncio.run(scenario())

    assert in_loop is not None
    assert in_thread == in_loop


def _deadline_app() -> TestClient:
    from app.db.request_deadline import _deadline
    from app.middleware.request_deadline import RequestDeadlineMiddleware

    def report(request: Request) -> JSONResponse:
        deadline = _deadline.get()
        remaining = None if deadline is None else deadline - time.monotonic()
        return JSONResponse({"remaining": remaining})

    probe = Starlette(
        routes=[
            Route("/api/plain", report, methods=["GET"]),
            Route("/metrics", report, methods=["GET"]),
            Route(f"{settings.API_PREFIX}/files/{{file_uuid}}/download-stream", report),
        ]
    )
    probe.add_middleware(RequestDeadlineMiddleware)
    return TestClient(probe)


def test_the_middleware_sets_a_deadline_for_an_ordinary_request():
    with _deadline_app() as client:
        body = client.get("/api/plain").json()

    assert 0 < body["remaining"] <= settings.REQUEST_DEADLINE_SECONDS


@pytest.mark.parametrize(
    "path",
    [
        "/metrics",
        f"{settings.API_PREFIX}/files/0b6c1f5e-3d2a-4c8e-9f41-7a5d2e8c6b10/download-stream",
    ],
)
def test_an_exempt_route_gets_no_deadline(path):
    with _deadline_app() as client:
        body = client.get(path).json()

    assert body["remaining"] is None


def test_zero_turns_the_deadline_off(monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_DEADLINE_SECONDS", 0.0)

    with _deadline_app() as client:
        body = client.get("/api/plain").json()

    assert body["remaining"] is None


def test_every_exempt_template_is_a_real_route():
    """The list cannot rot: a renamed route fails here instead of losing its exemption."""
    from app.main import app
    from app.middleware.request_deadline import EXEMPT_ROUTES

    routes = {
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or ())
        if hasattr(route, "path")
    }
    missing = [
        f"{e.method} {e.template}" for e in EXEMPT_ROUTES if (e.method, e.template) not in routes
    ]
    unexplained = [e.template for e in EXEMPT_ROUTES if not e.reason.strip()]

    assert len(EXEMPT_ROUTES) == 8
    assert missing == []
    assert unexplained == []


def test_the_app_installs_the_middleware_innermost():
    """Added first, so it wraps only the router (Starlette: last added = outermost)."""
    from app.main import app
    from app.middleware.request_deadline import RequestDeadlineMiddleware

    innermost: object = app.user_middleware[-1].cls
    assert innermost is RequestDeadlineMiddleware
