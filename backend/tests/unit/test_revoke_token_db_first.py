"""Revoking a refresh token is recorded in the database even when Redis is unreachable.

``refresh_token.revoked_at`` is the durable record; the Redis key is a cache of it.
While Redis is unreachable the readers consult the database, so a revocation that
only reached Redis (or reached neither) would be invisible to them. These tests run
against the real test database with a cache store whose every call raises
``redis.exceptions.ConnectionError``.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.auth import token_service as token_service_module
from app.auth.token_service import TokenService
from app.models.refresh_token import RefreshToken

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000cc"


class _UnreachableStore:
    """A cache store whose server cannot be reached."""

    def get(self, *_a, **_k):
        raise RedisConnectionError("cache unreachable")

    def set(self, *_a, **_k):
        raise RedisConnectionError("cache unreachable")


@pytest.fixture
def service() -> TokenService:
    svc = TokenService()
    svc._store = _UnreachableStore()
    return svc


def _issue(service: TokenService, db_session, user) -> tuple[str, RefreshToken]:
    return service.create_refresh_token(db_session, user.id, str(user.uuid), "user")


@pytest.mark.unit
class TestRevokeTokenRecordsInTheDatabase:
    def test_the_database_row_is_revoked_when_the_cache_is_unreachable(
        self, service, db_session, normal_user
    ):
        _token, row = _issue(service, db_session, normal_user)
        before = datetime.now(UTC)

        service.revoke_token(db_session, str(row.jti), row.expires_at)

        db_session.expire_all()
        stored = db_session.query(RefreshToken).filter(RefreshToken.jti == row.jti).one()
        assert stored.revoked_at >= before

    def test_a_revoked_token_is_refused_through_the_database_fallback(
        self, service, db_session, normal_user
    ):
        token, row = _issue(service, db_session, normal_user)

        service.revoke_token(db_session, str(row.jti), row.expires_at)

        payload, record = service.verify_refresh_token(db_session, token)
        assert (payload, record) == (None, None)

    def test_a_database_failure_is_reported_to_the_caller(
        self, service, db_session, normal_user, monkeypatch
    ):
        _token, row = _issue(service, db_session, normal_user)
        service._store = token_service_module.InMemoryStore()

        def _commit_fails() -> None:
            raise OperationalError("COMMIT", {}, Exception("database unavailable"))

        monkeypatch.setattr(db_session, "commit", _commit_fails)

        with pytest.raises(token_service_module.RevocationError):
            service.revoke_token(db_session, str(row.jti), row.expires_at)

    def test_an_access_token_with_no_durable_record_reports_a_cache_failure(
        self, service, db_session
    ):
        """An access JTI has no row, so the cache write is its only record."""
        with pytest.raises(token_service_module.RevocationError):
            service.revoke_token(
                db_session, "access-jti-without-a-row", datetime.now(UTC) + timedelta(minutes=5)
            )


@pytest.mark.unit
class TestRotationWhenTheOldTokenCannotBeRetired:
    def test_the_old_token_is_unusable_after_rotation_with_the_cache_unreachable(
        self, service, db_session, normal_user
    ):
        old_token, old_row = _issue(service, db_session, normal_user)

        service.rotate_refresh_token(
            db=db_session,
            old_token=old_token,
            old_token_record=old_row,
            user_id=normal_user.id,
            user_uuid=str(normal_user.uuid),
            role="user",
        )

        assert service.verify_refresh_token(db_session, old_token) == (None, None)

    def test_the_new_token_is_withdrawn_when_the_old_one_cannot_be_revoked(
        self, service, db_session, normal_user, monkeypatch
    ):
        old_token, old_row = _issue(service, db_session, normal_user)
        original = service.revoke_token
        calls: list[str] = []

        def _revoke(db, jti, expires_at=None):
            calls.append(jti)
            if len(calls) == 1:
                raise token_service_module.RevocationError("could not record revocation")
            return original(db, jti, expires_at)

        monkeypatch.setattr(service, "revoke_token", _revoke)

        with pytest.raises(token_service_module.RevocationError):
            service.rotate_refresh_token(
                db=db_session,
                old_token=old_token,
                old_token_record=old_row,
                user_id=normal_user.id,
                user_uuid=str(normal_user.uuid),
                role="user",
            )

        db_session.expire_all()
        rows = db_session.query(RefreshToken).filter(RefreshToken.user_id == normal_user.id).all()
        successors = [r for r in rows if r.jti != old_row.jti]
        assert len(successors) == 1
        assert successors[0].revoked_at is not None
