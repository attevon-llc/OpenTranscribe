"""Call contracts of the auth dependencies that no assertion pinned (mutation survivors).

Each test pins an argument or an effect that a caller cannot see from the return value:
the session and user handed to ``end_read_transaction`` (a dropped one leaves the request
holding its pooled connection), the role and JWT id read out of the token for the
role-mismatch and revoked-token warnings, ``exc_info`` on the optional-auth warnings, the
``or``/``and`` of the optional-auth admission, the arguments the admission gate receives,
and the overload re-raise on every broad ``except``.

Log assertions here check the VALUES a record carries (the rejected token type, the JWT-id
prefix, the exception text, whether a traceback is attached), never the wording around them.
"""

# mypy: disable-error-code="arg-type,comparison-overlap,index,attr-defined"
from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api.endpoints.auth import dependencies as deps_module
from app.api.endpoints.auth import external_token as external_token_module
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.dependencies import get_optional_current_user
from app.auth.direct_auth import create_access_token
from app.auth.provider_registry import ExternalIdentity
from app.auth.provider_registry import register_verifier
from app.core.config import settings
from tests.jwt_compat import jwt
from tests.unit.auth_dependencies._helpers import PROVIDER
from tests.unit.auth_dependencies._helpers import _persist_user
from tests.unit.auth_dependencies._helpers import _real_request
from tests.unit.auth_dependencies._helpers import _request
from tests.unit.auth_dependencies._helpers import _user

DEPS_LOGGER = "app.api.endpoints.auth.dependencies"


def _bearer(token: str):
    return _real_request(headers=[(b"authorization", f"Bearer {token}".encode())])


def _identity() -> ExternalIdentity:
    return ExternalIdentity(
        provider=PROVIDER,
        external_id=f"ext_{uuid4().hex[:10]}",
        email=f"call-contract-{uuid4().hex[:8]}@example.com",
        full_name="Call Contract",
        org_id=None,
        email_verified=True,
    )


def _register_external(identity: ExternalIdentity) -> None:
    class _Verifier:
        def verify(self, token, request):
            return identity

    register_verifier(PROVIDER, _Verifier())


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == DEPS_LOGGER and r.levelno == level]


@pytest.fixture
def end_reads(monkeypatch) -> list[tuple]:
    """Every ``end_read_transaction`` call the dependencies make, as the args tuple."""
    calls: list[tuple] = []
    monkeypatch.setattr(deps_module, "end_read_transaction", lambda *a, **k: calls.append(a))
    return calls


class TestEndReadTransactionReceivesTheSessionAndTheUser:
    """Without both, the connection is never released or the user is expired by the commit."""

    def test_get_current_user_local_token(self, db_session, end_reads):
        user = _persist_user(db_session)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})

        get_current_user(request=_request(), token=token, db=db_session)

        assert end_reads == [(db_session, user)]

    def test_get_current_user_external_token(self, db_session, end_reads, monkeypatch):
        external = _user()
        monkeypatch.setattr(deps_module, "_authenticate_external_token", lambda *a: external)

        got = get_current_user(request=_request(), token="opaque", db=db_session)

        assert got is external
        assert end_reads == [(db_session, external)]

    def test_get_current_active_user(self, db_session, end_reads):
        user = _persist_user(db_session)

        got = get_current_active_user(request=_request(), current_user=user, db=db_session)

        assert got is user
        assert end_reads == [(db_session, user)]

    def test_get_optional_current_user_local_token(self, db_session, end_reads):
        user = _persist_user(db_session)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})

        got = get_optional_current_user(request=_bearer(token), db=db_session)

        assert got is not None
        assert end_reads == [(db_session, got)]

    def test_get_optional_current_user_external_token(self, db_session, end_reads):
        _register_external(_identity())

        got = get_optional_current_user(request=_bearer("opaque"), db=db_session)

        assert got is not None
        assert end_reads == [(db_session, got)]


class TestOptionalAuthAdmission:
    """An inactive account is anonymous even when the lifecycle gate would admit it, and
    the gate is asked about the real request and session."""

    def test_an_inactive_local_user_is_anonymous_even_if_admitted(self, db_session, monkeypatch):
        user = _persist_user(db_session, is_active=False)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})
        monkeypatch.setattr(deps_module, "admitted_on_optional_auth", lambda *a: True)

        assert get_optional_current_user(request=_bearer(token), db=db_session) is None

    def test_an_inactive_external_user_is_anonymous_even_if_admitted(self, db_session, monkeypatch):
        from app.auth import external_sync

        _register_external(_identity())
        monkeypatch.setattr(
            external_sync, "sync_external_user_to_db", lambda db, ident: _user(is_active=False)
        )
        monkeypatch.setattr(deps_module, "admitted_on_optional_auth", lambda *a: True)

        assert get_optional_current_user(request=_bearer("opaque"), db=db_session) is None

    def test_the_local_gate_receives_the_user_request_and_session(self, db_session, monkeypatch):
        user = _persist_user(db_session)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})
        asked: list[tuple] = []

        def _admit(*args):
            asked.append(args)
            return True

        monkeypatch.setattr(deps_module, "admitted_on_optional_auth", _admit)
        request = _bearer(token)

        get_optional_current_user(request=request, db=db_session)

        assert asked == [(user, request, db_session)]

    def test_the_external_gate_receives_the_user_request_and_session(self, db_session, monkeypatch):
        from app.auth import external_sync

        _register_external(_identity())
        synced = _user()
        monkeypatch.setattr(external_sync, "sync_external_user_to_db", lambda db, ident: synced)
        asked: list[tuple] = []

        def _admit(*args):
            asked.append(args)
            return True

        monkeypatch.setattr(deps_module, "admitted_on_optional_auth", _admit)
        request = _bearer("opaque")

        get_optional_current_user(request=request, db=db_session)

        assert asked == [(synced, request, db_session)]


class TestTokenClaimsReachTheRoleMismatchAndRevocationWarnings:
    def test_a_stale_role_claim_warns_with_both_roles(self, db_session, caplog):
        user = _persist_user(db_session, role="user")
        token = create_access_token({"sub": str(user.uuid), "role": "admin"})

        with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
            get_current_user(request=_request(), token=token, db=db_session)

        (record,) = _records(caplog, logging.WARNING)
        message = record.getMessage()
        assert str(user.id) in message
        assert "'admin'" in message
        assert "'user'" in message

    def test_a_matching_role_claim_does_not_warn(self, db_session, caplog):
        user = _persist_user(db_session, role="user")
        token = create_access_token({"sub": str(user.uuid), "role": "user"})

        with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
            get_current_user(request=_request(), token=token, db=db_session)

        assert _records(caplog, logging.WARNING) == []

    def test_a_token_without_a_role_claim_does_not_warn(self, db_session, caplog):
        user = _persist_user(db_session, role="user")
        token = create_access_token({"sub": str(user.uuid)})

        with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
            get_current_user(request=_request(), token=token, db=db_session)

        assert _records(caplog, logging.WARNING) == []

    def test_a_revoked_token_is_logged_by_its_jti_prefix(self, db_session, caplog, monkeypatch):
        from fastapi import HTTPException

        user = _persist_user(db_session)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})
        jti = deps_module.jwt.decode(
            token,
            deps_module.OctKey.import_key(settings.JWT_SECRET_KEY),
            algorithms=deps_module.accepted_algorithms(),
        ).claims["jti"]
        monkeypatch.setattr(deps_module.token_service, "access_token_revoked", lambda p, d: True)

        with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER), pytest.raises(HTTPException):
            get_current_user(request=_request(), token=token, db=db_session)

        (record,) = _records(caplog, logging.WARNING)
        message = record.getMessage()
        assert jti[:8] in message
        assert jti[:9] not in message

    def test_a_foreign_token_type_is_logged_with_the_type(self, db_session, caplog):
        from fastapi import HTTPException

        now = datetime.now(UTC)
        token = jwt.encode(
            {"sub": str(uuid4()), "type": "mfa", "iat": now, "exp": now + timedelta(minutes=10)},
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER), pytest.raises(HTTPException):
            get_current_user(request=_request(), token=token, db=db_session)

        (record,) = _records(caplog, logging.WARNING)
        assert record.getMessage().startswith("Rejected token")
        assert "type='mfa'" in record.getMessage()


class TestUserLookupFailureLogs:
    def _boom_db(self):
        class _Q:
            def filter(self, *a, **k):
                return self

            def first(self):
                raise RuntimeError("db unavailable")

        class _DB:
            def query(self, *a, **k):
                return _Q()

        return _DB()

    def test_the_error_and_the_fabricated_user_are_logged_with_their_values(
        self, caplog, monkeypatch
    ):
        monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", False)
        subject = str(uuid4())
        token = create_access_token({"sub": subject, "role": "user"})

        with caplog.at_level(logging.INFO, logger=DEPS_LOGGER):
            get_current_user(request=_request(), token=token, db=self._boom_db())

        (error,) = _records(caplog, logging.ERROR)
        (info,) = _records(caplog, logging.INFO)
        assert "db unavailable" in error.getMessage()
        assert subject in info.getMessage()


class TestOverloadIsNeverSwallowed:
    """A pool timeout is answered 503 upstream; each broad ``except`` re-raises it."""

    def test_the_token_user_lookup(self, db_session, monkeypatch):
        user = _persist_user(db_session)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})

        def _overloaded(db, uuid):
            raise PoolTimeoutError("pool exhausted")

        monkeypatch.setattr(deps_module, "get_user_by_uuid", _overloaded)

        with pytest.raises(PoolTimeoutError):
            get_current_user(request=_request(), token=token, db=db_session)

    def test_the_optional_external_sync(self, db_session, monkeypatch):
        from app.auth import external_sync

        _register_external(_identity())

        def _overloaded(db, ident):
            raise PoolTimeoutError("pool exhausted")

        monkeypatch.setattr(external_sync, "sync_external_user_to_db", _overloaded)

        with pytest.raises(PoolTimeoutError):
            get_optional_current_user(request=_bearer("opaque"), db=db_session)

    def test_the_optional_local_lookup(self, db_session, monkeypatch):
        user = _persist_user(db_session)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})

        def _overloaded(db, uuid):
            raise PoolTimeoutError("pool exhausted")

        monkeypatch.setattr(deps_module, "get_user_by_uuid", _overloaded)

        with pytest.raises(PoolTimeoutError):
            get_optional_current_user(request=_bearer(token), db=db_session)

    def test_the_external_token_sync(self, db_session, monkeypatch):
        from app.auth import external_sync

        _register_external(_identity())

        def _overloaded(db, ident):
            raise PoolTimeoutError("pool exhausted")

        monkeypatch.setattr(external_sync, "sync_external_user_to_db", _overloaded)

        with pytest.raises(PoolTimeoutError):
            external_token_module._authenticate_external_token(
                _real_request(), "opaque", db_session
            )


class TestExternalSyncFailureIsLogged:
    """The sync failure is rejected as a 401; the log is the operator's only sign that
    provisioning is broken, so it must be an error-level record with a traceback."""

    def test_the_failure_is_logged_with_a_traceback(self, db_session, caplog, monkeypatch):
        from fastapi import HTTPException

        from app.auth import external_sync

        _register_external(_identity())

        def _broken(db, ident):
            raise RuntimeError("sync exploded")

        monkeypatch.setattr(external_sync, "sync_external_user_to_db", _broken)

        with caplog.at_level(logging.ERROR, logger=external_token_module.logger.name):
            with pytest.raises(HTTPException):
                external_token_module._authenticate_external_token(
                    _real_request(), "opaque", db_session
                )

        records = [r for r in caplog.records if r.name == external_token_module.logger.name]
        (record,) = records
        assert record.levelno == logging.ERROR
        assert record.getMessage().startswith("External JIT sync failed")
        assert record.exc_info is not None and record.exc_info[0] is RuntimeError


class TestOptionalAuthWarningsCarryATraceback:
    """A swallowed database error must leave a traceback for the operator."""

    def test_the_external_sync_failure(self, db_session, caplog, monkeypatch):
        from app.auth import external_sync

        _register_external(_identity())

        def _broken(db, ident):
            raise SQLAlchemyError("sync broke")

        monkeypatch.setattr(external_sync, "sync_external_user_to_db", _broken)

        with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
            result = get_optional_current_user(request=_bearer("opaque"), db=db_session)

        assert result is None
        (record,) = _records(caplog, logging.WARNING)
        assert "sync broke" in record.getMessage()
        assert record.exc_info is not None and record.exc_info[0] is SQLAlchemyError

    def test_the_local_lookup_failure(self, db_session, caplog, monkeypatch):
        user = _persist_user(db_session)
        token = create_access_token({"sub": str(user.uuid), "role": "user"})

        def _broken(db, uuid):
            raise SQLAlchemyError("lookup broke")

        monkeypatch.setattr(deps_module, "get_user_by_uuid", _broken)

        with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
            result = get_optional_current_user(request=_bearer(token), db=db_session)

        assert result is None
        (record,) = _records(caplog, logging.WARNING)
        assert "lookup broke" in record.getMessage()
        assert record.exc_info is not None and record.exc_info[0] is SQLAlchemyError
