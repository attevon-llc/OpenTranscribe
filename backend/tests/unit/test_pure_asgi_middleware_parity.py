"""Behaviour parity for the middleware converted from BaseHTTPMiddleware to pure ASGI.

Starlette docs ("Middleware" -> "Pure ASGI Middleware", and the BaseHTTPMiddleware
limitations) and the ASGI spec: a pure middleware sees raw messages, so a streamed
response must reach the client chunk by chunk, and websocket/lifespan scopes pass through.
"""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from app.middleware.audit import AuditMiddleware
from app.middleware.csrf import CSRFMiddleware
from app.middleware.observability import ObservabilityMiddleware
from app.middleware.robots import RobotsHeaderMiddleware

WRAPPERS = (AuditMiddleware, CSRFMiddleware, ObservabilityMiddleware, RobotsHeaderMiddleware)


async def _ok(request):
    return JSONResponse({"state": request.state.__dict__.get("_state", {}).get("request_id")})


async def _echo_body(request):
    return JSONResponse({"n": len(await request.body())})


async def _ws(websocket):
    await websocket.accept()
    await websocket.send_text("hi")
    await websocket.close()


def _app(*middleware):
    routes = [
        Route("/api/ok", _ok, methods=["GET", "POST"]),
        Route("/api/body", _echo_body, methods=["POST"]),
        WebSocketRoute("/api/ws/x", _ws),
    ]
    app = Starlette(routes=routes)
    for m in reversed(middleware):
        app.add_middleware(m)
    return app


def test_websocket_passes_through_every_converted_middleware():
    client = TestClient(_app(*WRAPPERS))
    with client.websocket_connect("/api/ws/x") as ws:
        assert ws.receive_text() == "hi"


def test_audit_adds_request_id_header_and_honours_inbound_one():
    client = TestClient(_app(AuditMiddleware))
    assert client.get("/api/ok", headers={"X-Request-ID": "abc"}).headers["x-request-id"] == "abc"
    assert len(client.get("/api/ok").headers["x-request-id"]) == 36


def test_robots_header_only_under_api_prefix():
    client = TestClient(_app(RobotsHeaderMiddleware))
    assert client.get("/api/ok").headers["x-robots-tag"] == "noindex, nofollow"
    assert "x-robots-tag" not in client.get("/elsewhere").headers


def test_csrf_rejection_is_403_json_and_does_not_reach_the_app():
    client = TestClient(_app(CSRFMiddleware))
    client.cookies.set("access_token", "x")
    resp = client.post("/api/ok")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "CSRF token missing or invalid"}


def test_csrf_matching_token_passes_and_body_is_not_consumed():
    client = TestClient(_app(CSRFMiddleware))
    client.cookies.set("access_token", "x")
    client.cookies.set("csrf_token", "t")
    resp = client.post("/api/body", content=b"12345", headers={"x-csrf-token": "t"})
    assert resp.status_code == 200
    assert resp.json() == {"n": 5}


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_streaming_response_is_not_buffered(wrapper):
    """The first body chunk must be sent before the app produces the second."""
    gate = asyncio.Event()

    async def stream(scope, receive, send):
        async def gen():
            yield b"one"
            await gate.wait()
            yield b"two"

        await StreamingResponse(gen())(scope, receive, send)

    app = wrapper(stream)
    sent: list[dict] = []

    async def run():
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()  # a live client: no disconnect until cancelled

        async def send(message):
            sent.append(message)
            if message.get("body") == b"one":
                gate.set()

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/stream",
            "headers": [],
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("t", 80),
            "client": ("c", 1),
            "http_version": "1.1",
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=5)

    asyncio.run(run())
    bodies = [m["body"] for m in sent if m["type"] == "http.response.body" and m["body"]]
    assert bodies == [b"one", b"two"]


def test_observability_reraises_and_records_500():
    async def boom(scope, receive, send):
        raise RuntimeError("x")

    async def run():
        async def receive():
            return {"type": "http.request"}

        async def send(_):
            pass

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/boom",
            "headers": [],
            "query_string": b"",
            "root_path": "",
        }
        await ObservabilityMiddleware(boom)(scope, receive, send)

    with pytest.raises(RuntimeError):
        asyncio.run(run())
