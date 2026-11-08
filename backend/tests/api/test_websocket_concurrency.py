"""``/ws`` must stay cheap for the event loop that every other user shares.

One API process serves every WebSocket on one event loop. The handshake authenticated on
that loop: a synchronous ``session_scope()`` (a pool checkout that can wait up to the pool
timeout, then the user query) and the token verification. A reconnect storm (every tab after
a backend restart) stalled every request in the process for the sum of those queries. The
work now runs in the threadpool.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import websockets as ws_module
from app.models.user import User
from tests.fixtures.ws_handshake import access_token
from tests.fixtures.ws_handshake import authenticate_frame
from tests.fixtures.ws_handshake import cookie_header
from tests.fixtures.ws_handshake import isolated_revocation
from tests.fixtures.ws_handshake import make_user
from tests.fixtures.ws_handshake import ws_test_client


@pytest.fixture
def client(db_session, monkeypatch) -> Iterator[TestClient]:
    isolated_revocation(monkeypatch)
    with ws_test_client(db_session, monkeypatch) as test_client:
        yield test_client


@pytest.fixture
def owner(db_session) -> User:
    return make_user(db_session)


@pytest.fixture
def threads(monkeypatch) -> dict[str, int]:
    """Record which thread authenticated the socket and which one registered it."""
    seen: dict[str, int] = {}
    authenticate = ws_module._try_authenticate_token

    def _recording_authenticate(*args: Any, **kwargs: Any) -> Any:
        seen["authenticate"] = threading.get_ident()
        return authenticate(*args, **kwargs)

    connect = ws_module.manager.connect

    def _recording_connect(*args: Any, **kwargs: Any) -> None:
        seen["event_loop"] = threading.get_ident()
        connect(*args, **kwargs)

    monkeypatch.setattr(ws_module, "_try_authenticate_token", _recording_authenticate)
    monkeypatch.setattr(ws_module.manager, "connect", _recording_connect)
    return seen


def _established(socket: Any) -> bool:
    return bool(json.loads(socket.receive_text())["type"] == "connection_established")


def test_the_mounted_ws_route_is_still_the_endpoint():
    """Moving the auth into a helper must not move the route decorator onto the helper."""
    from starlette.routing import WebSocketRoute

    from app.main import app

    routes = [r for r in app.routes if isinstance(r, WebSocketRoute) and r.path == "/api/ws"]
    assert [r.endpoint for r in routes] == [ws_module.websocket_endpoint]


def test_cookie_handshake_authenticates_off_the_event_loop(client, owner, threads):
    with client.websocket_connect("/ws", headers=cookie_header(access_token(owner))) as socket:
        assert _established(socket)

    assert set(threads) == {"authenticate", "event_loop"}
    assert threads["authenticate"] != threads["event_loop"], "auth ran on the event loop"


def test_first_message_handshake_authenticates_off_the_event_loop(client, owner, threads):
    with client.websocket_connect("/ws") as socket:
        socket.send_text(authenticate_frame(access_token(owner)))
        assert _established(socket)

    assert set(threads) == {"authenticate", "event_loop"}
    assert threads["authenticate"] != threads["event_loop"], "auth ran on the event loop"
