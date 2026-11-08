"""One refresh token can be spent once, and a spent token coming back ends its session.

Rotation used to verify the token with a plain read, mint the successor, then revoke the
old row unconditionally, so two refreshes presenting the same token at the same moment
both succeeded and the session forked into two live chains. The claim is now one
conditional ``UPDATE ... WHERE revoked_at IS NULL RETURNING``; the loser matches no row,
is answered 401, and the session's remaining tokens are revoked (RFC 9700 §4.14.2).

Real PostgreSQL connections through the real endpoint: each request opens its own
session from ``get_db``, and a barrier holds both requests until each has verified the
token, so both reach the claim with a token that was valid when they read it.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

from app.auth.audit import audit_logger
from app.auth.token_service import token_service
from app.db.base import SessionLocal
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User

REUSE_EVENT = "auth.token.reuse"


@pytest.fixture
def committed_user():
    """A user committed for real (the racers use their own connections), removed after."""
    with SessionLocal() as db:
        user = User(
            email=f"rotation-race-{uuid.uuid4().hex[:12]}@example.com",
            full_name="Rotation Race",
            hashed_password="not-used",
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db.add(user)
        db.commit()
        ids = (int(user.id), str(user.uuid))
    yield ids
    with SessionLocal() as db:
        db.query(RefreshToken).filter(RefreshToken.user_id == ids[0]).delete()
        db.query(User).filter(User.id == ids[0]).delete()
        db.commit()


@pytest.fixture
def audit_events(monkeypatch) -> list[str]:
    events: list[str] = []
    original = audit_logger.log

    def _capture(*args, **kwargs):
        event = kwargs.get("event_type")
        events.append(str(getattr(event, "value", event)))
        return original(*args, **kwargs)

    monkeypatch.setattr(audit_logger, "log", _capture)
    return events


def _issue(user_id: int, user_uuid: str) -> str:
    with SessionLocal() as db:
        token, _row = token_service.create_refresh_token(db, user_id, user_uuid, "user")
    return token


def _live_sessions(user_id: int) -> int:
    with SessionLocal() as db:
        return (
            db.query(RefreshToken)
            .filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .count()
        )


def _refresh(token: str) -> int:
    # No ``with``: entering the client would run the app's lifespan (startup tasks).
    client = TestClient(app)
    return client.post("/api/auth/token/refresh", json={"refresh_token": token}).status_code


def _race(token: str, monkeypatch) -> list[int]:
    """Both requests verify the token, then meet at the barrier, then claim it."""
    barrier = threading.Barrier(2)
    real_verify = token_service.verify_refresh_token

    def _verify_then_wait(db, presented):
        result = real_verify(db, presented)
        barrier.wait(timeout=30)
        return result

    monkeypatch.setattr(token_service, "verify_refresh_token", _verify_then_wait)
    statuses: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        code = _refresh(token)
        with lock:
            statuses.append(code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return sorted(statuses)


def test_two_concurrent_refreshes_of_one_token_yield_one_success(
    committed_user, audit_events, monkeypatch
):
    user_id, user_uuid = committed_user
    token = _issue(user_id, user_uuid)

    statuses = _race(token, monkeypatch)

    assert statuses == [200, 401]
    # The loser's refusal is reuse: the winner's fresh successor is revoked too.
    assert _live_sessions(user_id) == 0
    assert audit_events.count(REUSE_EVENT) == 1


def test_a_rotated_token_presented_later_ends_the_session(committed_user, audit_events):
    user_id, user_uuid = committed_user
    token = _issue(user_id, user_uuid)
    assert _refresh(token) == 200
    assert _live_sessions(user_id) == 1

    assert _refresh(token) == 401

    assert _live_sessions(user_id) == 0
    assert audit_events.count(REUSE_EVENT) == 1


def test_reuse_revokes_only_the_reused_session(committed_user):
    """Another device's session is a different family and survives."""
    user_id, user_uuid = committed_user
    other_device = _issue(user_id, user_uuid)
    token = _issue(user_id, user_uuid)
    assert _refresh(token) == 200

    assert _refresh(token) == 401

    assert _live_sessions(user_id) == 1
    assert _refresh(other_device) == 200


def test_a_logged_out_token_is_refused_without_a_reuse_event(committed_user, audit_events):
    user_id, user_uuid = committed_user
    token = _issue(user_id, user_uuid)
    with SessionLocal() as db:
        jti = db.query(RefreshToken.jti).filter(RefreshToken.user_id == user_id).scalar()
        token_service.revoke_token(db, str(jti))

    assert _refresh(token) == 401

    assert REUSE_EVENT not in audit_events


def test_a_failed_rotation_leaves_the_presented_token_usable(committed_user, monkeypatch):
    """The claim and the successor commit together, so a failure undoes the claim."""
    user_id, user_uuid = committed_user
    token = _issue(user_id, user_uuid)
    real_create = token_service.create_refresh_token

    def _mint_fails(*_a, **_k):
        raise OperationalError("INSERT", {}, Exception("database unavailable"))

    monkeypatch.setattr(token_service, "create_refresh_token", _mint_fails)
    assert _refresh(token) == 503
    monkeypatch.setattr(token_service, "create_refresh_token", real_create)

    assert _live_sessions(user_id) == 1
    assert _refresh(token) == 200
