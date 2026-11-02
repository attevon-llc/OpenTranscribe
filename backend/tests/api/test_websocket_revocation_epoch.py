"""The WebSocket handshake honours the per-user revocation epoch.

Logout-all, a password reset, an admin session termination and a role change stamp a
per-user epoch; an access token issued before it must stop working everywhere, not only
on HTTP routes. The handshake verified the token and checked its JTI, but never passed
the token's ``iat``, so the epoch comparison received ``None`` and a token issued before
logout-all still opened a socket and kept receiving that user's events.

Driven through the real ``/ws`` endpoint (Starlette's TestClient), with a real user row
and a private in-memory revocation store, on both the first-message and the cookie path.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth.token_service import TokenService
from app.models.user import User
from tests.fixtures.ws_handshake import INVALID_TOKEN_CLOSE
from tests.fixtures.ws_handshake import access_token
from tests.fixtures.ws_handshake import authenticate_frame
from tests.fixtures.ws_handshake import cookie_header
from tests.fixtures.ws_handshake import isolated_revocation
from tests.fixtures.ws_handshake import make_user
from tests.fixtures.ws_handshake import stamp_epoch
from tests.fixtures.ws_handshake import ws_test_client


@pytest.fixture
def revocation(monkeypatch) -> TokenService:
    return isolated_revocation(monkeypatch)


@pytest.fixture
def client(db_session, monkeypatch) -> Iterator[TestClient]:
    with ws_test_client(db_session, monkeypatch) as test_client:
        yield test_client


@pytest.fixture
def account(db_session) -> User:
    return make_user(db_session)


def test_first_message_token_issued_before_epoch_is_refused(revocation, client, account):
    epoch = stamp_epoch(revocation, account, seconds_ago=0)
    stale = access_token(account, issued_at=epoch - 60)

    with client.websocket_connect("/ws") as socket:
        socket.send_text(authenticate_frame(stale))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == INVALID_TOKEN_CLOSE


def test_cookie_token_issued_before_epoch_is_refused(revocation, client, account):
    """A stale cookie does not establish the socket; the fallback frame is refused too."""
    epoch = stamp_epoch(revocation, account, seconds_ago=0)
    stale = access_token(account, issued_at=epoch - 60)

    with client.websocket_connect("/ws", headers=cookie_header(stale)) as socket:
        socket.send_text(authenticate_frame(stale))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == INVALID_TOKEN_CLOSE


def test_first_message_token_issued_after_epoch_connects(revocation, client, account):
    """Control: signing in again after the revocation opens a socket at once."""
    stamp_epoch(revocation, account, seconds_ago=120)
    fresh = access_token(account, issued_at=int(time.time()))

    with client.websocket_connect("/ws") as socket:
        socket.send_text(authenticate_frame(fresh))
        established = json.loads(socket.receive_text())

    assert established["type"] == "connection_established"


def test_cookie_token_issued_after_epoch_connects(revocation, client, account):
    """Control: the cookie path still authenticates a token newer than the epoch."""
    stamp_epoch(revocation, account, seconds_ago=120)
    fresh = access_token(account, issued_at=int(time.time()))

    with client.websocket_connect("/ws", headers=cookie_header(fresh)) as socket:
        established = json.loads(socket.receive_text())

    assert established["type"] == "connection_established"
