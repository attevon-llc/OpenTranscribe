"""Issue #632: ``max_concurrent_sessions`` must be a real ceiling, not a target rate.

The old mechanism revoked exactly one session and minted exactly one on every login
above the cap — a conservation law once the active count already sits at or above
the limit, so it could never bring the count back down, and with no gap lock two
concurrent logins could both mint with no eviction at all, netting the count UP.
Five other minting paths (OIDC, SAML, PKI, proxy, MFA enrollment,
``account_security_service.reissue_current_session``) enforced nothing whatsoever.

The fix is a shared post-condition, ``token_service.enforce_session_ceiling``: "keep
the newest N active sessions, revoke the rest" — expressed as one
``UPDATE ... WHERE id IN (SELECT ... OFFSET :limit)`` statement, applied both at
login (``login.py``) and inside the universal choke point
(``token_service.create_refresh_token``, called by every minting path).

Everything here runs against a real Postgres ``db_session`` — the defect is a race
and a conservation-law argument about SQL semantics, not something a mock can stand
in for.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.auth.token_service import TokenService
from app.auth.token_service import enforce_session_ceiling
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("auth_config_behaviour")


def _save_session_config(db: Session, user: User, **config) -> None:
    """Write the ``session`` auth-config category the way the admin endpoint does."""
    AuthConfigService.bulk_update_category(
        db=db, category="session", config_dict=config, user_id=user.id
    )


def _seed_session(db: Session, user: User, **overrides) -> RefreshToken:
    """Insert one active ``refresh_token`` row directly, bypassing token minting.

    Used where the test cares about the SET of rows the SQL statement operates
    over, not about producing a real signed JWT.
    """
    now = datetime.now(UTC)
    unique = uuid.uuid4().hex
    attrs = {
        "user_id": user.id,
        "token_hash": f"hash-{unique}",
        "jti": str(uuid.uuid4()),
        "expires_at": now + timedelta(days=7),
        "revoked_at": None,
        "created_at": now,
        "last_activity_at": now,
        "absolute_expires_at": now + timedelta(hours=8),
        "user_agent": "pytest",
        "ip_address": "127.0.0.1",
    }
    attrs.update(overrides)
    row = RefreshToken(**attrs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _active_count(db: Session, user: User) -> int:
    return (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .count()
    )


def _active_jtis(db: Session, user: User) -> set[str]:
    rows = (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .all()
    )
    return {str(r.jti) for r in rows}


# ── 1. the headline convergence test ──────────────────────────────────────────────


def test_login_converges_an_existing_backlog_to_the_cap(client, db_session, normal_user):
    """20 pre-existing active sessions + a cap of 5 must leave exactly 5 after login.

    This is RED against the pre-#632 mechanism: "revoke one, mint one" leaves 20
    active sessions before the call and 20 after (one of the original 20 gone, the
    freshly minted one added).
    """
    _save_session_config(
        db_session,
        normal_user,
        max_concurrent_sessions=5,
        concurrent_session_policy="terminate_oldest",
    )
    for i in range(20):
        _seed_session(db_session, normal_user, created_at=datetime.now(UTC) - timedelta(minutes=i))

    response = client.post(
        "/api/auth/token",
        data={"username": normal_user.email, "password": "password123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.text

    assert _active_count(db_session, normal_user) == 5


# ── 2. idempotent / absolute ───────────────────────────────────────────────────────


def test_enforce_session_ceiling_is_idempotent(db_session, normal_user):
    for _ in range(7):
        _seed_session(db_session, normal_user)

    revoked_first = enforce_session_ceiling(db_session, normal_user.id, 5)
    db_session.commit()
    assert len(revoked_first) == 2
    assert _active_count(db_session, normal_user) == 5

    revoked_second = enforce_session_ceiling(db_session, normal_user.id, 5)
    db_session.commit()
    assert revoked_second == []
    assert _active_count(db_session, normal_user) == 5


# ── 3. the newest row is never the one evicted ─────────────────────────────────────


def test_the_newest_row_survives_identical_created_at(db_session, normal_user):
    """The real production case: rows minted in the same transaction tie on
    ``created_at`` (``server_default=func.now()``). Without the ``id DESC``
    tiebreaker, the row just inserted could be the one evicted.
    """
    same_instant = datetime.now(UTC)
    rows = [_seed_session(db_session, normal_user, created_at=same_instant) for _ in range(5)]
    # ids are assigned in insertion order by the serial PK.
    newest = max(rows, key=lambda r: r.id)

    enforce_session_ceiling(db_session, normal_user.id, 1)
    db_session.commit()

    live = _active_jtis(db_session, normal_user)
    assert live == {str(newest.jti)}, f"expected only the highest-id row to survive, got {live}"


# ── 4. batch bound ──────────────────────────────────────────────────────────────────


def test_batch_limit_bounds_a_single_call(db_session, normal_user):
    for _ in range(105):
        _seed_session(db_session, normal_user)

    revoked_first = enforce_session_ceiling(db_session, normal_user.id, 5, batch_limit=50)
    db_session.commit()
    assert len(revoked_first) == 50
    assert _active_count(db_session, normal_user) == 55

    revoked_second = enforce_session_ceiling(db_session, normal_user.id, 5, batch_limit=50)
    db_session.commit()
    assert len(revoked_second) == 50
    assert _active_count(db_session, normal_user) == 5


# ── 5. rotation must not evict other sessions ──────────────────────────────────────


def test_rotation_does_not_evict_other_sessions(db_session, normal_user, monkeypatch):
    service = TokenService()
    monkeypatch.setattr(service, "revoke_token", lambda db, jti, expires_at=None: True)

    _save_session_config(db_session, normal_user, max_concurrent_sessions=3)

    rows = [
        service.create_refresh_token(
            db=db_session, user_id=normal_user.id, user_uuid=str(normal_user.uuid), role="user"
        )[1]
        for _ in range(3)
    ]
    other_jtis = {str(rows[0].jti), str(rows[1].jti)}
    rotated_row = rows[2]

    service.rotate_refresh_token(
        db=db_session,
        old_token="irrelevant-old-token",
        old_token_record=rotated_row,
        user_id=normal_user.id,
        user_uuid=str(normal_user.uuid),
        role="user",
    )

    live = _active_jtis(db_session, normal_user)
    assert other_jtis <= live, f"rotation evicted an unrelated session: {other_jtis - live}"


# ── 6. every minting path is capped, not just login ────────────────────────────────


def test_create_refresh_token_enforces_the_cap_directly(db_session, normal_user):
    """The universal backstop (issue #632): calling ``create_refresh_token`` through
    ANY caller — not just ``login.py`` — must converge to the cap. Must be RED
    against the pre-#632 code, which enforced nothing at all outside ``login.py``.
    """
    service = TokenService()
    _save_session_config(db_session, normal_user, max_concurrent_sessions=4)

    for _ in range(4 + 3):
        service.create_refresh_token(
            db=db_session, user_id=normal_user.id, user_uuid=str(normal_user.uuid), role="user"
        )

    assert _active_count(db_session, normal_user) == 4


# ── 7. reject policy still rejects, exactly ────────────────────────────────────────


def test_reject_policy_still_refuses_with_429(client, db_session, normal_user):
    _save_session_config(
        db_session, normal_user, max_concurrent_sessions=2, concurrent_session_policy="reject"
    )
    for _ in range(2):
        _seed_session(db_session, normal_user)

    response = client.post(
        "/api/auth/token",
        data={"username": normal_user.email, "password": "password123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 429, response.text
