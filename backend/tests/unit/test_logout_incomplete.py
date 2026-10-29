"""Logout and logout-all report honestly when revocation cannot be completed.

Runs through the real endpoints against the test database. The cache outage is a
store whose every call raises ``redis.exceptions.ConnectionError``; the database
outage is ``sqlalchemy.exc.OperationalError``.
"""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.token_service import token_service
from app.models.refresh_token import RefreshToken


class _UnreachableStore:
    def get(self, *_a, **_k):
        raise RedisConnectionError("cache unreachable")

    def set(self, *_a, **_k):
        raise RedisConnectionError("cache unreachable")


def _login(client, user):
    resp = client.post(
        "/api/auth/token",
        data={"username": user.email, "password": "password123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    return resp.json()


def _cache_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(token_service, "_store", _UnreachableStore())
    monkeypatch.setattr(token_service, "_degraded", False)


def _cleared_cookies(resp) -> str:
    return " ".join(resp.headers.get_list("set-cookie")).lower()


@pytest.fixture
def audit_records(monkeypatch) -> list[dict]:
    records: list[dict] = []
    original = audit_logger.log

    def _capture(*args, **kwargs):
        records.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit_logger, "log", _capture)
    return records


def _rows(db_session, user) -> list[RefreshToken]:
    db_session.expire_all()
    rows: list[RefreshToken] = (
        db_session.query(RefreshToken).filter(RefreshToken.user_id == user.id).all()
    )
    return rows


@pytest.mark.unit
class TestLogoutWhenTheCacheIsUnreachable:
    def test_logout_answers_503_and_still_clears_the_cookies(
        self, client, normal_user, monkeypatch, audit_records
    ):
        tokens = _login(client, normal_user)
        _cache_unreachable(monkeypatch)

        resp = client.post(
            "/api/auth/logout", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )

        assert resp.status_code == 503
        cookies = _cleared_cookies(resp)
        assert "access_token=" in cookies
        assert "refresh_token=" in cookies
        assert resp.json() == {"detail": {"code": "logout_incomplete"}}

    def test_the_refresh_token_is_revoked_in_the_database_regardless(
        self, client, normal_user, db_session, monkeypatch
    ):
        tokens = _login(client, normal_user)
        _cache_unreachable(monkeypatch)

        client.post(
            "/api/auth/logout", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )

        rows = _rows(db_session, normal_user)
        assert rows
        assert all(r.revoked_at is not None for r in rows)

    def test_an_incomplete_logout_is_recorded_as_a_failure(
        self, client, normal_user, monkeypatch, audit_records
    ):
        tokens = _login(client, normal_user)
        _cache_unreachable(monkeypatch)
        audit_records.clear()

        client.post(
            "/api/auth/logout", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )

        logouts = [r for r in audit_records if r.get("event_type") == AuditEventType.AUTH_LOGOUT]
        assert logouts
        assert all(r["outcome"] == AuditOutcome.FAILURE for r in logouts)
        assert logouts[-1]["details"]["revocation_failed"]


@pytest.mark.unit
class TestLogoutAllWhenSessionsCannotBeRevoked:
    def test_a_database_failure_answers_503_and_keeps_the_cookies(
        self, client, normal_user, monkeypatch, audit_records
    ):
        tokens = _login(client, normal_user)

        def _fails(*_a, **_k):
            raise OperationalError("UPDATE", {}, Exception("database unavailable"))

        monkeypatch.setattr(token_service, "revoke_all_user_tokens_pending_audit", _fails)
        audit_records.clear()

        resp = client.post(
            "/api/auth/logout/all", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )

        assert resp.status_code == 503
        assert resp.json() == {"detail": {"code": "logout_all_failed"}}
        assert "access_token=" not in _cleared_cookies(resp)
        failures = [r for r in audit_records if r.get("outcome") == AuditOutcome.FAILURE]
        assert failures

    def test_an_unreachable_cache_does_not_prevent_the_database_revocation(
        self, client, normal_user, db_session, monkeypatch
    ):
        tokens = _login(client, normal_user)
        _cache_unreachable(monkeypatch)

        resp = client.post(
            "/api/auth/logout/all", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )

        assert resp.status_code == 200
        rows = _rows(db_session, normal_user)
        assert rows
        assert all(r.revoked_at is not None for r in rows)
        assert token_service.is_token_revoked(str(rows[0].jti), db=db_session) is True

    def test_no_bulk_success_record_is_written_when_the_commit_fails(
        self, client, normal_user, db_session, monkeypatch, audit_records
    ):
        tokens = _login(client, normal_user)

        def _commit_fails():
            raise OperationalError("COMMIT", {}, Exception("database unavailable"))

        monkeypatch.setattr(db_session, "commit", _commit_fails)
        audit_records.clear()

        resp = client.post(
            "/api/auth/logout/all", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )

        assert resp.status_code == 503
        bulk_success = [
            r
            for r in audit_records
            if r.get("event_type") == AuditEventType.AUTH_TOKEN_REVOKE
            and r.get("outcome") == AuditOutcome.SUCCESS
        ]
        assert bulk_success == []
