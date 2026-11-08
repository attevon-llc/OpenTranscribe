"""Revoking a user's sessions closes the WebSockets they already have open.

The handshake refuses a token issued before the revocation epoch, but a socket opened
before logout-all (or a password reset, an admin termination, a role change) had already
passed that check, and nothing re-checked it: it kept delivering the user's events for
as long as the browser kept it open.

End to end in one process: a real revocation (``revoke_all_user_tokens_in_transaction``,
the core of logout-all) publishes through ``utils/websocket_notify``; a stand-in Redis
hands the published bytes to the real subscriber dispatch on the app's event loop, which
closes the socket the client is holding.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from app.api import websockets as ws_module
from app.api.endpoints.auth import sessions
from app.auth.token_service import TokenService
from app.models.user import User
from app.services import gdpr_erasure_service
from app.services import user_deletion_service
from app.utils import websocket_notify
from tests.fixtures.ws_handshake import INVALID_TOKEN_CLOSE
from tests.fixtures.ws_handshake import access_token
from tests.fixtures.ws_handshake import authenticate_frame
from tests.fixtures.ws_handshake import cookie_header
from tests.fixtures.ws_handshake import isolated_revocation
from tests.fixtures.ws_handshake import make_user
from tests.fixtures.ws_handshake import ws_test_client


class _LoopbackRedis:
    """Delivers each publish to the app's subscriber dispatch, as Redis pub/sub would."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self.published: list[tuple[str, str]] = []

    def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        message: dict[str, Any] = {"type": "message", "data": payload}
        portal = self._client.portal
        assert portal is not None
        portal.call(ws_module._dispatch_notification, message)
        return 1


@pytest.fixture
def revocation(monkeypatch) -> TokenService:
    return isolated_revocation(monkeypatch)


@pytest.fixture
def client(db_session, monkeypatch) -> Iterator[TestClient]:
    with ws_test_client(db_session, monkeypatch) as test_client:
        monkeypatch.setattr(
            websocket_notify, "get_request_redis", lambda: _LoopbackRedis(test_client)
        )
        yield test_client


@pytest.fixture
def owner(db_session) -> User:
    return make_user(db_session)


@pytest.fixture
def bystander(db_session) -> User:
    return make_user(db_session)


def _established(socket: Any) -> bool:
    return bool(json.loads(socket.receive_text())["type"] == "connection_established")


def _next_frame(socket: Any) -> str:
    """Ping, then read: an open socket answers with an echo, a closed one raises.

    Without the ping, a socket that was never closed would block the read forever.
    """
    socket.send_text("ping")
    return str(socket.receive_text())


def test_logout_all_closes_the_users_open_socket(db_session, revocation, client, owner):
    token = access_token(owner)
    with client.websocket_connect("/ws", headers=cookie_header(token)) as socket:
        assert _established(socket)

        revocation.revoke_all_user_tokens_in_transaction(
            db_session, int(owner.id), user_uuid=str(owner.uuid)
        )

        with pytest.raises(WebSocketDisconnect) as closed:
            _next_frame(socket)

    assert closed.value.code == ws_module.SESSION_REVOKED_CLOSE


def test_another_users_socket_stays_open(db_session, revocation, client, owner, bystander):
    """Control: the close is per user, not every socket in the process."""
    with (
        client.websocket_connect("/ws", headers=cookie_header(access_token(owner))) as revoked,
        client.websocket_connect("/ws", headers=cookie_header(access_token(bystander))) as other,
    ):
        assert _established(revoked) and _established(other)

        revocation.revoke_all_user_tokens_in_transaction(
            db_session, int(owner.id), user_uuid=str(owner.uuid)
        )
        with pytest.raises(WebSocketDisconnect):
            _next_frame(revoked)

        other.send_text("still here")
        echoed = json.loads(other.receive_text())

    assert echoed == {"type": "echo", "data": "still here"}


def test_the_closed_socket_cannot_come_back_with_the_same_token(
    db_session, revocation, client, owner
):
    """After the close, a reconnect with the revoked token is refused at the handshake.

    Issued a few seconds earlier: a token minted in the epoch's own second passes the
    epoch check by design (``iat < epoch``), so a session reissued by the same request
    keeps working.
    """
    token = access_token(owner, issued_at=int(time.time()) - 5)
    with client.websocket_connect("/ws", headers=cookie_header(token)) as socket:
        assert _established(socket)
        revocation.revoke_all_user_tokens_in_transaction(
            db_session, int(owner.id), user_uuid=str(owner.uuid)
        )
        with pytest.raises(WebSocketDisconnect):
            _next_frame(socket)

    with client.websocket_connect("/ws", headers=cookie_header(token)) as retry:
        retry.send_text(authenticate_frame(token))
        with pytest.raises(WebSocketDisconnect) as refused:
            retry.receive_text()

    assert refused.value.code == INVALID_TOKEN_CLOSE


@pytest.mark.parametrize("remove", ["delete", "erase"])
def test_deleting_the_account_closes_its_open_socket(
    db_session, revocation, client, owner, monkeypatch, remove
):
    """Deletion and GDPR erasure remove the row; the socket must not outlive it."""
    monkeypatch.setattr(user_deletion_service, "token_service", revocation)
    monkeypatch.setattr(gdpr_erasure_service, "token_service", revocation)
    with client.websocket_connect("/ws", headers=cookie_header(access_token(owner))) as socket:
        assert _established(socket)

        if remove == "delete":
            user_deletion_service.delete_user_account(db_session, owner)
        else:
            summary = gdpr_erasure_service.erase_user(db_session, int(owner.id))
            assert summary["users_deleted"] == 1

        with pytest.raises(WebSocketDisconnect) as closed:
            _next_frame(socket)

    assert closed.value.code == ws_module.SESSION_REVOKED_CLOSE


def test_single_session_logout_closes_only_that_tokens_socket(
    db_session, revocation, client, owner, monkeypatch
):
    """`/logout` blacklists the presented access token; the socket it opened goes with it,
    while a second tab's socket on another token stays open."""
    monkeypatch.setattr(sessions, "token_service", revocation)
    logged_out, other_tab = access_token(owner, jti="logout-a"), access_token(owner)
    with (
        client.websocket_connect("/ws", headers=cookie_header(logged_out)) as closing,
        client.websocket_connect("/ws", headers=cookie_header(other_tab)) as staying,
    ):
        assert _established(closing) and _established(staying)

        claims = {"sub": str(owner.uuid), "jti": "logout-a", "exp": int(time.time()) + 600}
        request = Request({"type": "http", "headers": []})
        assert sessions._revoke_presented_tokens(db_session, request, claims) == []

        with pytest.raises(WebSocketDisconnect) as closed:
            _next_frame(closing)
        staying.send_text("still here")
        echoed = json.loads(staying.receive_text())

    assert closed.value.code == ws_module.SESSION_REVOKED_CLOSE
    assert echoed == {"type": "echo", "data": "still here"}


def test_a_socket_does_not_outlive_its_access_token(db_session, revocation, client, owner):
    """Nothing revoked it, but its token expired: the socket closes so the SPA re-checks
    the session and reconnects with a fresh token, rather than staying open forever."""
    with client.websocket_connect(
        "/ws", headers=cookie_header(access_token(owner, expires_in=2))
    ) as socket:
        assert _established(socket)

        deadline = time.monotonic() + 15
        closed: WebSocketDisconnect | None = None
        while time.monotonic() < deadline:
            try:
                _next_frame(socket)
            except WebSocketDisconnect as disconnect:
                closed = disconnect
                break
            time.sleep(0.2)

    assert closed is not None, "the socket outlived its access token"
    assert closed.code == ws_module.SESSION_REVOKED_CLOSE
    assert closed.reason == "Session expired"


@pytest.fixture
def http(db_session) -> Iterator[TestClient]:
    """The real application, for the HTTP routes that revoke (not the socket client above)."""
    from app.db.base import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("trigger", ["password_change", "admin_disable"])
def test_a_credential_or_status_change_closes_the_open_socket(
    db_session, revocation, client, http, owner, monkeypatch, trigger
):
    """The two triggers #1362 names besides logout-all, driven through their real routes.

    Both reach the socket only through ``account_security_service.revoke_all_sessions``;
    this proves each route actually calls it, not just that the shared core closes sockets.
    """
    from app.services import account_security_service

    monkeypatch.setattr(account_security_service, "token_service", revocation)
    with client.websocket_connect("/ws", headers=cookie_header(access_token(owner))) as socket:
        assert _established(socket)

        if trigger == "password_change":
            resp = http.put(
                "/api/users/me",
                headers={"Authorization": f"Bearer {access_token(owner)}"},
                json={
                    "password": "Fresh-Quartz-Passphrase-2026!",
                    "current_password": "irrelevant-Passphrase99!",
                },
            )
        else:
            admin = make_user(db_session, role="super_admin", is_superuser=True)
            resp = http.put(
                f"/api/users/{owner.uuid}",
                headers={"Authorization": f"Bearer {access_token(admin)}"},
                json={"is_active": False},
            )
        assert resp.status_code == 200, resp.text

        with pytest.raises(WebSocketDisconnect) as closed:
            _next_frame(socket)

    assert closed.value.code == ws_module.SESSION_REVOKED_CLOSE
