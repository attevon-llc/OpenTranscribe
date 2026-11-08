"""Every write route of the real app is refused in demo mode, and a new one cannot skip it (#628).

The guard (``app/middleware/demo_readonly.py``) is default-deny, so the structural question is
not "does each route call a guard" (the shape ``test_hold_freeze_coverage.py`` checks) but
"does each route, driven through the real middleware stack, come back refused". This file
walks the real route table, sends every unsafe method of every HTTP route and opens every
WebSocket through ``TestClient(app)`` with ``DEMO_MODE`` on, and requires exactly
``403 {"detail": {"code": "demo_read_only"}}`` or a WebSocket close with code 1008. The
request never reaches a handler, so no database is needed.

A route that is not refused must be in ``DEMO_WRITE_ALLOWLIST`` with a written reason, and an
allowlist entry naming no real route is stale. A route type the walk does not know fails.

Self-tested on a synthetic app: without the middleware the crawl must report its write route
and its WebSocket, with it the crawl must report nothing, and with ``DEMO_MODE`` off the
middleware must let a write through; without that, an empty result would be
indistinguishable from a crawl that checked nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field

import pytest
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.routing import Route
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.middleware import demo_readonly
from app.middleware.demo_readonly import DEMO_WRITE_ALLOWLIST
from app.middleware.demo_readonly import REFUSAL_CODE
from app.middleware.demo_readonly import SAFE_METHODS
from app.middleware.demo_readonly import WEBSOCKET_REFUSAL_CODE
from app.middleware.demo_readonly import DemoReadOnlyMiddleware
from app.middleware.demo_readonly import is_allowed_in_demo
from app.middleware.demo_readonly import write_key
from app.middleware.upload_limit import UploadBodyLimitMiddleware

#: The real app has 344 unsafe (method, route) pairs today; a walk that finds far fewer has
#: stopped seeing routes, and every whole-list assertion below would pass on an empty list.
MIN_REAL_WRITE_ROUTES = 250


@dataclass
class RouteTable:
    writes: list[tuple[str, str]] = field(default_factory=list)  # (method, template path)
    sockets: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def walk(routes: list, prefix: str = "") -> RouteTable:
    """Every unsafe (method, path) and WebSocket path, branching on the route type."""
    table = RouteTable()
    for route in routes:
        path = prefix + getattr(route, "path", "")
        if isinstance(route, WebSocketRoute):
            table.sockets.append(path)
        elif isinstance(route, Route):
            for method in sorted(set(route.methods or ()) - SAFE_METHODS):
                table.writes.append((method, path))
        elif isinstance(route, Mount) and route.routes:
            sub = walk(list(route.routes), path)
            table.writes += sub.writes
            table.sockets += sub.sockets
            table.unknown += sub.unknown
        else:
            table.unknown.append(f"{type(route).__name__} {path or repr(route)}")
    return table


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _refused(response) -> bool:
    if response.status_code != 403:
        return False
    try:
        detail = response.json().get("detail")
    except ValueError:
        return False
    return isinstance(detail, dict) and detail.get("code") == REFUSAL_CODE


def unrefused(app) -> list[str]:
    """Routes of ``app`` that a demo deployment does not refuse (allowlisted ones excluded)."""
    table = walk(list(app.routes))
    client = TestClient(app, raise_server_exceptions=False)
    offenders = [f"unknown route type: {u}" for u in table.unknown]
    for method, path in table.writes:
        if write_key(method, path) in DEMO_WRITE_ALLOWLIST:
            continue
        response = client.request(method, _concrete(path), json={})
        if not _refused(response):
            offenders.append(f"{method} {path} -> {response.status_code}")
    for path in table.sockets:
        try:
            with client.websocket_connect(_concrete(path)):
                offenders.append(f"WEBSOCKET {path} -> accepted")
        except WebSocketDisconnect as exc:
            if exc.code != WEBSOCKET_REFUSAL_CODE:
                offenders.append(f"WEBSOCKET {path} -> closed {exc.code}")
    return offenders


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)


@pytest.fixture(scope="module")
def real_app():
    from app.main import app

    return app


# --- the real app ------------------------------------------------------------------------


def test_every_write_route_and_websocket_of_the_real_app_is_refused(real_app, demo_on):
    table = walk(list(real_app.routes))
    assert len(table.writes) > MIN_REAL_WRITE_ROUTES
    assert table.sockets, "the real app's WebSocket route was not seen"

    assert unrefused(real_app) == []


def test_every_real_route_type_is_modelled(real_app):
    assert walk(list(real_app.routes)).unknown == []


def test_every_allowlist_entry_names_a_real_write_route(real_app):
    real = {write_key(m, p) for m, p in walk(list(real_app.routes)).writes}

    assert sorted(set(DEMO_WRITE_ALLOWLIST) - real) == []


@pytest.mark.parametrize("key", sorted(DEMO_WRITE_ALLOWLIST))
def test_every_allowlist_entry_has_a_reason_and_is_let_through(key):
    method, _, path = key.partition(" ")

    assert len(DEMO_WRITE_ALLOWLIST[key].split()) >= 4
    assert "{" not in path, "the allowlist matches exact paths; a template can never match"
    assert is_allowed_in_demo(method, path)


def test_the_guard_runs_inside_cors_and_outside_the_upload_limit(real_app):
    order = [m.cls for m in real_app.user_middleware]  # outermost first

    assert order.index(CORSMiddleware) < order.index(DemoReadOnlyMiddleware)
    assert order.index(DemoReadOnlyMiddleware) < order.index(UploadBodyLimitMiddleware)


# --- the decision function ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("post", "/api/auth/login", True),
        ("POST", "/api/auth/login/", True),
        ("GET", "/api/files/x", True),
        ("HEAD", "/api/files", True),
        ("POST", "/api/auth/login/extra", False),
        ("POST", "/API/auth/login", False),
        ("PUT", "/api/auth/login", False),
        ("PROPFIND", "/api/files", False),
        ("POST", "/no/such/route", False),
    ],
)
def test_the_decision_is_exact_and_fails_closed(method, path, allowed):
    assert is_allowed_in_demo(method, path) is allowed


# --- self-test: the crawl must be able to fail -------------------------------------------


def _synthetic_app(with_guard: bool) -> FastAPI:
    app = FastAPI()

    @app.post("/api/things")
    def create_thing() -> dict:
        return {"created": True}

    @app.delete("/api/things/{thing_id}")
    def delete_thing(thing_id: str) -> dict:
        return {"deleted": thing_id}

    @app.websocket("/api/sock")
    async def sock(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    if with_guard:
        app.add_middleware(DemoReadOnlyMiddleware)
    return app


def test_selftest_an_unguarded_write_route_and_websocket_are_reported(demo_on):
    offenders = unrefused(_synthetic_app(with_guard=False))

    assert offenders == [
        "POST /api/things -> 200",
        "DELETE /api/things/{thing_id} -> 200",
        "WEBSOCKET /api/sock -> accepted",
    ]


def test_selftest_a_guarded_app_reports_nothing(demo_on):
    assert unrefused(_synthetic_app(with_guard=True)) == []


def test_selftest_an_unknown_route_type_is_reported():
    class Strange:
        path = "/api/strange"

    assert walk([Strange()]).unknown == ["Strange /api/strange"]


def test_with_demo_mode_off_the_guard_passes_writes_through(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    client = TestClient(_synthetic_app(with_guard=True))

    assert client.post("/api/things").json() == {"created": True}
    with client.websocket_connect("/api/sock") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_the_refusal_names_the_code_and_echoes_nothing(demo_on):
    client = TestClient(_synthetic_app(with_guard=True))

    response = client.post("/api/things?probe=<script>", json={"x": "secret"})

    assert response.status_code == 403
    assert response.json() == {
        "detail": {"code": REFUSAL_CODE, "message": demo_readonly.REFUSAL_MESSAGE}
    }
