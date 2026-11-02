"""The WebSocket handshake applies the account approval and expiry gates.

HTTP refuses a rejected, pending (while approval is required) or expired account on every
authenticated route, and optional-auth routes treat one as anonymous
(``lifecycle_gates.admitted_on_optional_auth``). The handshake checked only ``is_active``,
so such an account with a still-valid token opened a socket and received its live events.

The banner and forced-password-change holds stay admitted, as on optional-auth routes:
both have a self-service remedy whose screens run while the hold is in force.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.models.user import User
from tests.fixtures.ws_handshake import INVALID_TOKEN_CLOSE
from tests.fixtures.ws_handshake import access_token
from tests.fixtures.ws_handshake import authenticate_frame
from tests.fixtures.ws_handshake import cookie_header
from tests.fixtures.ws_handshake import isolated_revocation
from tests.fixtures.ws_handshake import make_user
from tests.fixtures.ws_handshake import ws_test_client


@pytest.fixture(autouse=True)
def _revocation(monkeypatch):
    isolated_revocation(monkeypatch)


@pytest.fixture
def client(db_session, monkeypatch) -> Iterator[TestClient]:
    with ws_test_client(db_session, monkeypatch) as test_client:
        yield test_client


def _refusal_code(client: TestClient, user: User) -> int:
    token = access_token(user)
    with client.websocket_connect("/ws", headers=cookie_header(token)) as socket:
        socket.send_text(authenticate_frame(token))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
    return closed.value.code


def _connects(client: TestClient, user: User) -> bool:
    with client.websocket_connect("/ws", headers=cookie_header(access_token(user))) as socket:
        return bool(json.loads(socket.receive_text())["type"] == "connection_established")


def test_a_rejected_account_is_refused(db_session, client):
    rejected = make_user(db_session, approval_status="rejected")

    assert _refusal_code(client, rejected) == INVALID_TOKEN_CLOSE


def test_an_expired_account_is_refused(db_session, client):
    expired = make_user(db_session, account_expires_at=datetime.now(UTC) - timedelta(minutes=1))

    assert _refusal_code(client, expired) == INVALID_TOKEN_CLOSE


def test_a_pending_account_is_refused_while_approval_is_required(db_session, client, monkeypatch):
    from app.api.endpoints.auth import approval_gate

    monkeypatch.setattr(approval_gate, "approval_enforced", lambda _db: True)
    pending = make_user(db_session, approval_status="pending")

    assert _refusal_code(client, pending) == INVALID_TOKEN_CLOSE


def test_an_approved_account_connects(db_session, client):
    """Control: an ordinary account is unaffected."""
    assert _connects(client, make_user(db_session, approval_status="approved")) is True


def test_an_account_expiring_later_connects(db_session, client):
    """Control: the expiry gate bites only once the date has passed."""
    later = make_user(db_session, account_expires_at=datetime.now(UTC) + timedelta(days=1))

    assert _connects(client, later) is True


def test_an_account_owing_a_password_change_connects(db_session, client):
    """Control: a remedy-screen hold is not an admission refusal."""
    assert _connects(client, make_user(db_session, must_change_password=True)) is True


def test_a_rejected_account_is_refused_on_the_first_message_path(db_session, client):
    """No cookie: the token arrives in the authenticate frame, the other handshake branch."""
    rejected = make_user(db_session, approval_status="rejected")

    with client.websocket_connect("/ws") as socket:
        socket.send_text(authenticate_frame(access_token(rejected)))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == INVALID_TOKEN_CLOSE


def test_an_approved_account_connects_on_the_first_message_path(db_session, client):
    """Control for the frame branch."""
    approved = make_user(db_session, approval_status="approved")

    with client.websocket_connect("/ws") as socket:
        socket.send_text(authenticate_frame(access_token(approved)))
        assert json.loads(socket.receive_text())["type"] == "connection_established"
