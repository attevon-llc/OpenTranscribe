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

import contextlib
import json
import time
import uuid as uuid_pkg
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import websockets as ws_module
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.session import InMemoryStore
from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
from app.auth.token_service import TokenService
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.user import User
from tests.jwt_compat import jwt

INVALID_TOKEN_CLOSE = 4003


@pytest.fixture
def revocation(monkeypatch) -> TokenService:
    """A ``TokenService`` on a private in-memory store, wired into the WebSocket module."""
    service = TokenService()
    service._store = InMemoryStore()
    monkeypatch.setattr(ws_module, "token_service", service)
    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
    return service


@pytest.fixture
def client(db_session, monkeypatch) -> Iterator[TestClient]:
    """A TestClient for the real endpoint, using the test's own DB session."""

    @contextlib.contextmanager
    def _test_session_scope():
        yield db_session

    monkeypatch.setattr(ws_module, "session_scope", _test_session_scope)
    monkeypatch.setattr(ws_module, "setup_redis", lambda: None)
    app = FastAPI()
    app.add_api_websocket_route("/ws", ws_module.websocket_endpoint)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def account(db_session) -> User:
    unique = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"ws-epoch-{unique}@example.com",
        full_name="Socket Person",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="local",
        is_active=True,
        is_superuser=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _token(user: User, *, issued_at: int) -> str:
    """An otherwise-valid access token with a chosen ``iat``."""
    return jwt.encode(
        {
            "sub": str(user.uuid),
            "type": TOKEN_TYPE_ACCESS,
            "jti": uuid_pkg.uuid4().hex,
            "iat": issued_at,
            "exp": int(time.time()) + 600,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _stamp_epoch(service: TokenService, user: User, *, seconds_ago: int) -> int:
    epoch = int(time.time()) - seconds_ago
    service.store.set(f"{USER_REVOCATION_EPOCH_PREFIX}{user.uuid}", str(epoch), ex=600)
    return epoch


def _authenticate_frame(token: str) -> str:
    return json.dumps({"type": "authenticate", "token": token})


def test_first_message_token_issued_before_epoch_is_refused(revocation, client, account):
    epoch = _stamp_epoch(revocation, account, seconds_ago=0)
    stale = _token(account, issued_at=epoch - 60)

    with client.websocket_connect("/ws") as socket:
        socket.send_text(_authenticate_frame(stale))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == INVALID_TOKEN_CLOSE


def test_cookie_token_issued_before_epoch_is_refused(revocation, client, account):
    """A stale cookie does not establish the socket; the fallback frame is refused too."""
    epoch = _stamp_epoch(revocation, account, seconds_ago=0)
    stale = _token(account, issued_at=epoch - 60)

    with client.websocket_connect("/ws", headers={"cookie": f"access_token={stale}"}) as socket:
        socket.send_text(_authenticate_frame(stale))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == INVALID_TOKEN_CLOSE


def test_first_message_token_issued_after_epoch_connects(revocation, client, account):
    """Control: signing in again after the revocation opens a socket at once."""
    _stamp_epoch(revocation, account, seconds_ago=120)
    fresh = _token(account, issued_at=int(time.time()))

    with client.websocket_connect("/ws") as socket:
        socket.send_text(_authenticate_frame(fresh))
        established = json.loads(socket.receive_text())

    assert established["type"] == "connection_established"


def test_cookie_token_issued_after_epoch_connects(revocation, client, account):
    """Control: the cookie path still authenticates a token newer than the epoch."""
    _stamp_epoch(revocation, account, seconds_ago=120)
    fresh = _token(account, issued_at=int(time.time()))

    with client.websocket_connect("/ws", headers={"cookie": f"access_token={fresh}"}) as socket:
        established = json.loads(socket.receive_text())

    assert established["type"] == "connection_established"
