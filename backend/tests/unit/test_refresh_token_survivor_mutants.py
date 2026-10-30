"""Refresh-token lifecycle behaviour no test asserted (repo cleanup b9, the token_service split).

Written from the mutants of ``auth/refresh_tokens.py`` that survived the whole token test set
at HEAD (before the split moved the code here); each test below was watched failing against
its mutant. The evidence lives outside the repo (``~/ot-cleanup-work/b9/token``).

Every test runs the real ``TokenService`` against the real database session; only the
clock (``refresh_tokens.datetime``), the session config (``get_auth_settings``) and the
audit sink are substituted.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey
from sqlalchemy.orm import Session

import app.core.auth_settings as auth_settings_module
from app.auth import refresh_tokens as refresh_tokens_module
from app.auth import token_service as token_service_module
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.session import InMemoryStore
from app.auth.token_service import TokenService
from app.core.config import settings
from app.core.constants import SESSION_CAP_EVICTION_BATCH_LIMIT
from app.models.refresh_token import RefreshToken
from app.models.user import User

T0 = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)


class _FrozenDatetime(datetime):
    """``refresh_tokens.datetime`` with a ``now`` the test moves."""

    frozen = T0

    @classmethod
    def now(cls, tz=None):
        return cls.frozen


@pytest.fixture
def clock(monkeypatch) -> type[_FrozenDatetime]:
    _FrozenDatetime.frozen = T0
    monkeypatch.setattr(refresh_tokens_module, "datetime", _FrozenDatetime)
    return _FrozenDatetime


def _session_config(monkeypatch, *, idle: int, absolute: int, cap: int) -> None:
    monkeypatch.setattr(
        auth_settings_module,
        "get_auth_settings",
        lambda db: SimpleNamespace(
            session_idle_timeout_minutes=idle,
            session_absolute_timeout_minutes=absolute,
            max_concurrent_sessions=cap,
        ),
    )


@pytest.fixture
def service() -> TokenService:
    svc = TokenService()
    svc._store = InMemoryStore()
    return svc


@pytest.fixture
def audit_records(monkeypatch) -> list[dict]:
    records: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: records.append(kw))
    return records


def _active(db: Session, user: User) -> list[RefreshToken]:
    db.expire_all()
    return (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .all()
    )


def _mint(service: TokenService, db: Session, user: User) -> tuple[str, RefreshToken]:
    return service.create_refresh_token(db, user.id, str(user.uuid), "user")


# ── create_refresh_token ──────────────────────────────────────────────────────────


def test_a_cap_of_one_keeps_only_the_newest_session(db_session, normal_user, service, monkeypatch):
    _session_config(monkeypatch, idle=30, absolute=480, cap=1)

    _first_token, first = _mint(service, db_session, normal_user)
    _second_token, second = _mint(service, db_session, normal_user)

    assert {str(r.jti) for r in _active(db_session, normal_user)} == {str(second.jti)}
    assert str(first.jti) != str(second.jti)


def test_one_mint_evicts_at_most_one_batch_of_a_backlog(
    db_session, normal_user, service, monkeypatch
):
    _session_config(monkeypatch, idle=30, absolute=480, cap=1)
    now = datetime.now(UTC)
    backlog = SESSION_CAP_EVICTION_BATCH_LIMIT + 10
    for i in range(backlog):
        db_session.add(
            RefreshToken(
                user_id=normal_user.id,
                token_hash=f"hash-{uuid.uuid4().hex}",
                jti=str(uuid.uuid4()),
                expires_at=now + timedelta(days=7),
                created_at=now - timedelta(minutes=i + 1),
            )
        )
    db_session.commit()

    _mint(service, db_session, normal_user)

    # backlog + the new row, minus exactly one batch: the rest waits for the sweep.
    assert len(_active(db_session, normal_user)) == backlog + 1 - SESSION_CAP_EVICTION_BATCH_LIMIT


def test_a_new_session_lives_exactly_the_configured_refresh_days(
    db_session, normal_user, service, clock, monkeypatch
):
    _session_config(monkeypatch, idle=30, absolute=480, cap=0)
    monkeypatch.setattr(settings, "JWT_REFRESH_TOKEN_EXPIRE_DAYS", 3)

    token, row = _mint(service, db_session, normal_user)

    expected = T0 + timedelta(days=3)
    assert row.expires_at == expected
    claims = jwt.decode(
        token, OctKey.import_key(settings.JWT_SECRET_KEY), algorithms=[settings.JWT_ALGORITHM]
    ).claims
    assert claims["exp"] == int(expected.timestamp())
    assert claims["iat"] == int(T0.timestamp())


def test_minting_a_session_audits_it_as_created(db_session, normal_user, audit_records):
    service = TokenService()
    service._store = InMemoryStore()

    _token, row = service.create_refresh_token(
        db_session,
        normal_user.id,
        str(normal_user.uuid),
        "user",
        user_agent="pytest-agent",
        ip_address="192.0.2.7",
    )

    assert audit_records == [
        {
            "event_type": AuditEventType.AUTH_SESSION_CREATED,
            "outcome": AuditOutcome.SUCCESS,
            "user_id": normal_user.id,
            "source_ip": "192.0.2.7",
            "user_agent": "pytest-agent",
            "details": {"jti": str(row.jti)},
        }
    ]


# ── verify_refresh_token ──────────────────────────────────────────────────────────


def test_an_expired_row_is_refused_and_audited_even_with_a_live_jwt(
    db_session, normal_user, service, audit_records, monkeypatch
):
    _session_config(monkeypatch, idle=0, absolute=0, cap=0)
    token, row = _mint(service, db_session, normal_user)
    row.absolute_expires_at = None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    audit_records.clear()

    assert service.verify_refresh_token(db_session, token) == (None, None)
    assert audit_records == [
        {
            "event_type": AuditEventType.AUTH_SESSION_EXPIRED,
            "outcome": AuditOutcome.FAILURE,
            "user_id": normal_user.id,
            "details": {"jti": str(row.jti)},
        }
    ]


def test_a_degraded_store_answers_from_the_database_not_by_denying(
    db_session, normal_user, monkeypatch
):
    """With Redis gone, a live session must still refresh: the DB row is the record.

    Dropping ``db=db`` from the blacklist check makes the degraded path fail closed for
    every refresh, logging every user out while Redis is down.
    """
    _session_config(monkeypatch, idle=30, absolute=480, cap=0)
    monkeypatch.setattr(token_service_module, "get_redis_client", lambda: None)
    service = TokenService()
    token, row = _mint(service, db_session, normal_user)

    payload, verified = service.verify_refresh_token(db_session, token)

    assert service.degraded is True
    assert verified is not None and str(verified.jti) == str(row.jti)
    assert payload is not None and payload["jti"] == str(row.jti)


def test_a_blacklisted_jti_is_refused_even_while_its_row_is_live(
    db_session, normal_user, service, monkeypatch
):
    """The bulk revoker writes the blacklist BEFORE its caller commits the rows.

    A failed commit leaves the key set and the row live; the cache entry alone must
    still refuse (fail closed), so the blacklist check is not redundant with the row.
    """
    _session_config(monkeypatch, idle=30, absolute=480, cap=0)
    token, row = _mint(service, db_session, normal_user)
    service.store.set(f"revoked:jti:{row.jti}", "revoked", ex=600)

    assert service.verify_refresh_token(db_session, token) == (None, None)


def test_a_row_revoked_only_in_the_database_is_refused(
    db_session, normal_user, service, monkeypatch
):
    """The row is the record; an empty cache (flushed, restarted, missed write) must not win."""
    _session_config(monkeypatch, idle=30, absolute=480, cap=0)
    token, row = _mint(service, db_session, normal_user)
    row.revoked_at = datetime.now(UTC)
    db_session.commit()

    assert service.store.get(f"revoked:jti:{row.jti}") is None
    assert service.verify_refresh_token(db_session, token) == (None, None)


# ── _session_within_lifetime boundaries ──────────────────────────────────────────


def _row(**columns) -> RefreshToken:
    return RefreshToken(jti=str(uuid.uuid4()), **columns)


def test_the_absolute_ceiling_instant_itself_is_still_inside(db_session, clock, monkeypatch):
    _session_config(monkeypatch, idle=0, absolute=480, cap=0)
    row = _row(absolute_expires_at=T0 + timedelta(minutes=10), last_activity_at=T0)

    clock.frozen = T0 + timedelta(minutes=10)
    assert TokenService._session_within_lifetime(db_session, row) is True
    clock.frozen = T0 + timedelta(minutes=10, microseconds=1)
    assert TokenService._session_within_lifetime(db_session, row) is False


def test_the_idle_limit_instant_itself_is_still_inside(db_session, clock, monkeypatch):
    _session_config(monkeypatch, idle=15, absolute=480, cap=0)
    row = _row(absolute_expires_at=T0 + timedelta(hours=8), last_activity_at=T0)

    clock.frozen = T0 + timedelta(minutes=15)
    assert TokenService._session_within_lifetime(db_session, row) is True
    clock.frozen = T0 + timedelta(minutes=15, microseconds=1)
    assert TokenService._session_within_lifetime(db_session, row) is False


# ── rotate_refresh_token ──────────────────────────────────────────────────────────


def test_rotation_revokes_the_predecessor_for_its_remaining_lifetime(
    db_session, normal_user, service, monkeypatch
):
    """The real revoke path, not a stub: the old JTI is blacklisted and its row revoked."""
    _session_config(monkeypatch, idle=30, absolute=480, cap=0)
    old_token, old_row = _mint(service, db_session, normal_user)

    _new_token, new_row = service.rotate_refresh_token(
        db_session, old_token, old_row, normal_user.id, str(normal_user.uuid), "user"
    )

    assert service.store.get(f"revoked:jti:{old_row.jti}") == "revoked"
    assert service.verify_refresh_token(db_session, old_token) == (None, None)
    assert {str(r.jti) for r in _active(db_session, normal_user)} == {str(new_row.jti)}


def test_rotating_a_legacy_row_at_the_cap_evicts_no_other_device(
    db_session, normal_user, service, monkeypatch
):
    """A pre-v377 row carries no ceiling, so its rotation re-reads the session config.

    That is the one rotation where the cap value is in hand; ``enforce_concurrent_limit=False``
    is all that stops this refresh from evicting the user's other sessions.
    """
    _session_config(monkeypatch, idle=30, absolute=480, cap=3)
    _t1, other_a = _mint(service, db_session, normal_user)
    _t2, other_b = _mint(service, db_session, normal_user)
    legacy_token, legacy = _mint(service, db_session, normal_user)
    legacy.absolute_expires_at = None
    db_session.commit()

    service.rotate_refresh_token(
        db_session, legacy_token, legacy, normal_user.id, str(normal_user.uuid), "user"
    )

    live = {str(r.jti) for r in _active(db_session, normal_user)}
    assert {str(other_a.jti), str(other_b.jti)} <= live
