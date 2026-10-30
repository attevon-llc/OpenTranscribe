"""Session-ceiling behaviour no test asserted (repo cleanup b9, the token_service split).

Written from the mutants of ``auth/session_ceiling.py`` that survived the whole token test
set at HEAD (before the split moved the code here); each test below was watched failing
against its mutant. The evidence lives outside the repo (``~/ot-cleanup-work/b9/token``).

* ``"limit": max(limit, 0)`` -> ``limit``: a negative limit reached ``OFFSET`` and Postgres
  raised, so a caller computing ``max_concurrent_sessions - 1`` from a cap of 0 would take
  the login down instead of keeping no sessions.
* ``AND expires_at > now()`` dropped: an expired-but-unrevoked row counted toward the cap,
  so a user with stale rows lost a LIVE session to make room for a dead one.
* The ``.env`` fallback inside ``_session_lifetime_minutes``: every existing test reached
  ``get_auth_settings``' own internal fallback, never this one, so swapping the idle and
  absolute timeouts (or zeroing the cap) there went unnoticed.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

import app.core.auth_settings as auth_settings_module
from app.auth.session_ceiling import _session_lifetime_minutes
from app.auth.session_ceiling import enforce_session_ceiling
from app.core.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User


def _seed(db: Session, user: User, **overrides) -> RefreshToken:
    now = datetime.now(UTC)
    attrs = {
        "user_id": user.id,
        "token_hash": f"hash-{uuid.uuid4().hex}",
        "jti": str(uuid.uuid4()),
        "expires_at": now + timedelta(days=7),
        "revoked_at": None,
        "created_at": now,
        "last_activity_at": now,
        "absolute_expires_at": now + timedelta(hours=8),
    }
    attrs.update(overrides)
    row = RefreshToken(**attrs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _revoked_at(db: Session, row: RefreshToken) -> datetime | None:
    db.expire_all()
    fresh = db.query(RefreshToken).filter(RefreshToken.id == row.id).one()
    return fresh.revoked_at


def test_a_negative_limit_keeps_no_sessions_instead_of_raising(db_session, normal_user):
    rows = [_seed(db_session, normal_user) for _ in range(3)]

    revoked = enforce_session_ceiling(db_session, normal_user.id, -1)
    db_session.commit()

    assert sorted(revoked) == sorted(str(r.jti) for r in rows)
    assert all(_revoked_at(db_session, r) is not None for r in rows)


def test_an_expired_row_neither_counts_toward_the_cap_nor_is_revoked(db_session, normal_user):
    now = datetime.now(UTC)
    older_live = _seed(db_session, normal_user, created_at=now - timedelta(minutes=2))
    newer_live = _seed(db_session, normal_user, created_at=now - timedelta(minutes=1))
    expired = _seed(
        db_session,
        normal_user,
        created_at=now,
        expires_at=now - timedelta(minutes=5),
    )

    revoked = enforce_session_ceiling(db_session, normal_user.id, 2)
    db_session.commit()

    assert revoked == []
    assert _revoked_at(db_session, older_live) is None
    assert _revoked_at(db_session, newer_live) is None
    assert _revoked_at(db_session, expired) is None


def test_an_unreadable_auth_config_falls_back_to_the_env_values_in_order(db_session, monkeypatch):
    def _unavailable(db):
        raise RuntimeError("auth settings unavailable")

    monkeypatch.setattr(auth_settings_module, "get_auth_settings", _unavailable)
    monkeypatch.setattr(settings, "SESSION_IDLE_TIMEOUT_MINUTES", 17)
    monkeypatch.setattr(settings, "SESSION_ABSOLUTE_TIMEOUT_MINUTES", 431)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_SESSIONS", 6)

    assert _session_lifetime_minutes(db_session) == (17, 431, 6)
