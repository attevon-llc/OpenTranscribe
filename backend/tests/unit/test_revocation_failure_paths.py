"""Revocation failures end in the documented outcome, not in a follow-on database error.

Covers the rotation endpoint's 503, the successor-withdrawal path when the database
that failed is also what the next read needs, the audit read after a revocation
commit, and the SAML single-logout audit outcome.
"""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from starlette.requests import Request
from starlette.responses import Response

from app.api.endpoints.auth import saml as saml_module
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.cookies import REFRESH_COOKIE
from app.auth.saml.config import SAMLConfig
from app.auth.token_service import RevocationError
from app.auth.token_service import TokenService
from app.auth.token_service import token_service
from app.models.refresh_token import RefreshToken


class _WorkingStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key, *_a, **_k):
        return self.data.get(key)

    def set(self, key, value, *_a, **_k):
        self.data[key] = value


@pytest.fixture
def service() -> TokenService:
    svc = TokenService()
    svc._store = _WorkingStore()
    return svc


@pytest.fixture
def reads_fail(db_session):
    """Switch that makes every ORM statement on ``db_session`` raise, like a dead database."""
    state = {"down": False}

    def _maybe_fail(_orm_execute_state):
        if state["down"]:
            raise OperationalError("SELECT", {}, Exception("database unavailable"))

    event.listen(db_session, "do_orm_execute", _maybe_fail)
    try:
        yield state
    finally:
        event.remove(db_session, "do_orm_execute", _maybe_fail)


def _issue(service: TokenService, db_session, user):
    return service.create_refresh_token(db_session, user.id, str(user.uuid), "user")


@pytest.fixture
def audit_records(monkeypatch) -> list[dict]:
    records: list[dict] = []
    original = audit_logger.log

    def _capture(*args, **kwargs):
        records.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit_logger, "log", _capture)
    return records


@pytest.mark.unit
class TestRotationWhenTheDatabaseFailsAfterTheSuccessorExists:
    def test_the_failure_stays_a_revocation_error(
        self, service, db_session, normal_user, monkeypatch, reads_fail
    ):
        """A lazy reload of the successor must not replace the typed error."""
        old_token, old_row = _issue(service, db_session, normal_user)
        calls: list[str] = []

        def _revoke(db, jti, expires_at=None):
            calls.append(jti)
            # What a failed revocation leaves behind: a rolled-back session whose
            # objects are expired, over a database that no longer answers.
            db.expire_all()
            reads_fail["down"] = True
            raise RevocationError("could not record revocation")

        monkeypatch.setattr(service, "revoke_token", _revoke)

        with pytest.raises(RevocationError):
            service.rotate_refresh_token(
                db=db_session,
                old_token=old_token,
                old_token_record=old_row,
                user_id=normal_user.id,
                user_uuid=str(normal_user.uuid),
                role="user",
            )

        assert len(calls) == 2, "the successor withdrawal was not attempted"


@pytest.mark.unit
class TestTheAuditAfterARevocationCommit:
    def test_an_owner_lookup_after_the_commit_is_not_needed(
        self, service, db_session, normal_user, reads_fail, monkeypatch, audit_records
    ):
        """The commit expires the row; the audit must not reload it."""
        _token, row = _issue(service, db_session, normal_user)
        expires_at = row.expires_at
        jti = str(row.jti)
        owner = normal_user.id
        real_commit = db_session.commit

        def _commit_then_database_goes_away():
            real_commit()
            reads_fail["down"] = True

        monkeypatch.setattr(db_session, "commit", _commit_then_database_goes_away)

        service.revoke_token(db_session, jti, expires_at)

        revokes = [
            r for r in audit_records if r.get("event_type") == AuditEventType.AUTH_TOKEN_REVOKE
        ]
        assert len(revokes) == 1
        assert revokes[0]["user_id"] == owner
        reads_fail["down"] = False
        db_session.expire_all()
        stored = db_session.query(RefreshToken).filter(RefreshToken.jti == jti).one()
        assert stored.revoked_at is not None


def _refresh(client, refresh_token: str):
    csrf = client.cookies.get("csrf_token")
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return client.post(
        "/api/auth/token/refresh", json={"refresh_token": refresh_token}, headers=headers
    )


@pytest.mark.unit
class TestRefreshEndpointWhenRotationCannotBeRecorded:
    def test_it_answers_503_and_hands_out_no_new_refresh_cookie(
        self, client, normal_user, monkeypatch
    ):
        login = client.post(
            "/api/auth/token",
            data={"username": normal_user.email, "password": "password123"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert login.status_code == 200
        refresh = login.json()["refresh_token"]

        def _fails(*_a, **_k):
            raise RevocationError("could not record revocation")

        monkeypatch.setattr(token_service, "rotate_refresh_token", _fails)

        response = _refresh(client, refresh)

        assert response.status_code == 503
        assert response.json() == {"detail": {"code": "token_rotation_failed"}}
        set_cookies = " ".join(response.headers.get_list("set-cookie")).lower()
        assert "refresh_token=" not in set_cookies


class _StubAuth:
    """python3-saml stand-in: ``process_slo`` runs the callback like the library does."""

    def __init__(self, errors: list[str] | None = None) -> None:
        self._errors = errors or []

    def process_slo(self, delete_session_cb=None, **_k):
        if delete_session_cb is not None:
            delete_session_cb()
        return "https://idp.example.com/after-logout"

    def get_errors(self) -> list[str]:
        return self._errors


def _request_with_cookies(cookies: dict[str, str]) -> Request:
    header = "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()
    return Request({"type": "http", "method": "GET", "headers": [(b"cookie", header)]})


def _run_sls(db, request) -> Response:
    return saml_module._complete_saml_sls(
        request,
        db,
        cast("SAMLConfig", object()),
        {},
        "203.0.113.9",
        "pytest",
    )


def _logout_records(audit_records):
    return [r for r in audit_records if r.get("event_type") == AuditEventType.AUTH_LOGOUT]


@pytest.mark.unit
class TestSamlSingleLogoutOutcome:
    @pytest.fixture
    def session_cookies(self, db_session, normal_user, monkeypatch):
        monkeypatch.setattr(saml_module, "_build_auth", lambda *_a, **_k: _StubAuth())
        token, row = token_service.create_refresh_token(
            db_session, normal_user.id, str(normal_user.uuid), "user"
        )
        return token, row

    def test_a_recorded_revocation_is_audited_as_success(
        self, db_session, session_cookies, audit_records
    ):
        token, row = session_cookies
        jti = str(row.jti)

        response = _run_sls(db_session, _request_with_cookies({REFRESH_COOKIE: token}))

        assert response.status_code == 302
        (record,) = _logout_records(audit_records)
        assert record["outcome"] == AuditOutcome.SUCCESS
        assert record["details"]["revocation_failed"] is False
        db_session.expire_all()
        stored = db_session.query(RefreshToken).filter(RefreshToken.jti == jti).one()
        assert stored.revoked_at is not None

    def test_a_failed_revocation_is_audited_as_failure_but_the_redirect_completes(
        self, db_session, session_cookies, audit_records, monkeypatch
    ):
        token, _row = session_cookies

        def _fails(*_a, **_k):
            raise RevocationError("could not record revocation")

        monkeypatch.setattr(token_service, "revoke_token", _fails)

        response = _run_sls(db_session, _request_with_cookies({REFRESH_COOKIE: token}))

        assert response.status_code == 302
        assert "refresh_token=" in " ".join(response.headers.getlist("set-cookie"))
        (record,) = _logout_records(audit_records)
        assert record["outcome"] == AuditOutcome.FAILURE
        assert record["details"]["revocation_failed"] is True

    def test_an_unexpected_error_while_revoking_is_never_audited_as_success(
        self, db_session, session_cookies, audit_records, monkeypatch
    ):
        token, _row = session_cookies

        def _database_error(*_a, **_k):
            raise OperationalError("SELECT", {}, Exception("database unavailable"))

        monkeypatch.setattr(token_service, "verify_refresh_token", _database_error)

        response = _run_sls(db_session, _request_with_cookies({REFRESH_COOKIE: token}))

        assert response.status_code == 302
        (record,) = _logout_records(audit_records)
        assert record["outcome"] == AuditOutcome.FAILURE
        assert record["details"]["processing_failed"] is True


@pytest.mark.unit
class TestBulkRevocationAuditRecordsTheEpoch:
    def test_epoch_stamped_is_true_when_the_epoch_was_written(
        self, service, db_session, normal_user, audit_records
    ):
        _issue(service, db_session, normal_user)

        service.revoke_all_user_tokens_in_transaction(db_session, normal_user.id)

        (record,) = [
            r
            for r in audit_records
            if r.get("event_type") == AuditEventType.AUTH_TOKEN_REVOKE
            and r["details"].get("scope") == "all_user_tokens"
        ]
        assert record["details"]["epoch_stamped"] is True

    def test_epoch_stamped_is_false_when_the_epoch_write_failed(
        self, db_session, normal_user, audit_records
    ):
        from redis.exceptions import ConnectionError as RedisConnectionError

        class _Down:
            def get(self, *_a, **_k):
                raise RedisConnectionError("down")

            def set(self, *_a, **_k):
                raise RedisConnectionError("down")

        svc = TokenService()
        svc._store = _Down()
        _issue(svc, db_session, normal_user)

        svc.revoke_all_user_tokens_in_transaction(db_session, normal_user.id)

        (record,) = [
            r
            for r in audit_records
            if r.get("event_type") == AuditEventType.AUTH_TOKEN_REVOKE
            and r["details"].get("scope") == "all_user_tokens"
        ]
        assert record["details"]["epoch_stamped"] is False
