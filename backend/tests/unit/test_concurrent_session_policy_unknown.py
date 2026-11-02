"""An unrecognised ``concurrent_session_policy`` is enforced as ``reject``.

The admin API validates the value (``schemas/auth_config.py``), but a stored row
written another way or a mistyped ``CONCURRENT_SESSION_POLICY`` in ``.env`` reached
``_enforce_concurrent_session_limit`` unchecked, matched neither branch and issued
the session with nothing logged. The stricter of the two policies is applied and a
warning names the value so the operator can correct it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import get_args

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.auth.login import CONCURRENT_SESSION_POLICIES
from app.api.endpoints.auth.login import _enforce_concurrent_session_limit
from app.models.auth_config import AuthConfig
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth_config import SessionConfig

pytestmark = pytest.mark.xdist_group("auth_config_behaviour")

LOGIN_LOGGER = "app.api.endpoints.auth.login"


def _seed_sessions(db: Session, user: User, count: int) -> None:
    now = datetime.now(UTC)
    for i in range(count):
        db.add(
            RefreshToken(
                user_id=user.id,
                token_hash=f"hash-{uuid.uuid4().hex}",
                jti=str(uuid.uuid4()),
                expires_at=now + timedelta(days=7),
                created_at=now - timedelta(minutes=i),
                last_activity_at=now,
                absolute_expires_at=now + timedelta(hours=8),
                user_agent="pytest",
                ip_address="127.0.0.1",
            )
        )
    db.commit()


def _active_count(db: Session, user: User) -> int:
    return (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .count()
    )


def _store(db: Session, key: str, value: str) -> None:
    """Upsert a ``session`` row directly, the way a non-API write would land."""
    row = db.query(AuthConfig).filter(AuthConfig.config_key == key).first()
    if row is None:
        row = AuthConfig(config_key=key, category="session")
        db.add(row)
    row.config_value = value
    db.commit()


def test_the_recognised_set_matches_what_the_admin_api_admits():
    annotation = SessionConfig.model_fields["concurrent_session_policy"].annotation

    assert set(get_args(annotation)) == CONCURRENT_SESSION_POLICIES


class TestTheEnforcer:
    def test_an_unknown_policy_at_the_cap_is_refused_with_429(
        self, db_session, normal_user, caplog
    ):
        _seed_sessions(db_session, normal_user, 2)

        with caplog.at_level(logging.WARNING, logger=LOGIN_LOGGER):
            with pytest.raises(HTTPException) as caught:
                _enforce_concurrent_session_limit(
                    db_session, normal_user, 2, "rejekt", "127.0.0.1", "pytest"
                )

        assert caught.value.status_code == 429
        assert _active_count(db_session, normal_user) == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("rejekt" in r.getMessage() for r in warnings), caplog.text

    def test_an_unknown_policy_below_the_cap_issues_the_session(self, db_session, normal_user):
        """Control: below the cap the policy is never consulted."""
        _seed_sessions(db_session, normal_user, 1)

        _enforce_concurrent_session_limit(
            db_session, normal_user, 2, "rejekt", "127.0.0.1", "pytest"
        )

        assert _active_count(db_session, normal_user) == 1

    def test_terminate_oldest_at_the_cap_still_evicts(self, db_session, normal_user):
        """Control: a recognised policy keeps its own behaviour."""
        _seed_sessions(db_session, normal_user, 2)

        _enforce_concurrent_session_limit(
            db_session, normal_user, 2, "terminate_oldest", "127.0.0.1", "pytest"
        )

        assert _active_count(db_session, normal_user) == 1


class TestTheLoginRoute:
    def test_a_stored_unknown_policy_refuses_a_login_at_the_cap(
        self, client, db_session, normal_user
    ):
        _store(db_session, "max_concurrent_sessions", "2")
        _store(db_session, "concurrent_session_policy", "rejekt")
        _seed_sessions(db_session, normal_user, 2)

        response = client.post(
            "/api/auth/token",
            data={"username": normal_user.email, "password": "password123"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        assert response.status_code == 429, response.text
        assert _active_count(db_session, normal_user) == 2
