"""Pins the exact audit record and 401 of the external absolute-timeout refusal, and the
per-user revocation epoch on the OPTIONAL auth path.

Mutation testing found both unpinned: ``_enforce_external_absolute_timeout`` was only
checked for "some 401", so its audit record, ``detail`` body and ``WWW-Authenticate``
header could change freely (39 survivors); ``get_optional_current_user`` stopped passing
``iat`` into the revocation check, which silently exempts every optional-auth route
(public/share-link reads) from logout-all and password-reset revocation.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.endpoints.auth import dependencies as deps_module
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.external_session import ERROR_CODE_SESSION_EXPIRED
from app.auth.provider_registry import ExternalIdentity
from app.auth.session import InMemoryStore
from app.auth.token_service import TokenService
from app.core.config import settings
from tests.jwt_compat import jwt


def _request(user_agent: str = "mutant-agent/1.0") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("backend", 8080),
            "root_path": "",
            "path": "/api/files/x",
            "query_string": b"",
            "headers": [(b"user-agent", user_agent.encode())],
            "client": ("10.0.0.7", 40000),
        }
    )


def _identity() -> ExternalIdentity:
    return ExternalIdentity(provider="acme-idp", external_id="ext-1", email="who@acme.example")


class TestExpiredExternalSessionRefusal:
    @pytest.fixture
    def audited(self, monkeypatch) -> list[dict[str, Any]]:
        import app.auth.external_session as external_session

        monkeypatch.setattr(external_session, "external_session_expired", lambda _identity: True)
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(
            deps_module.audit_logger, "log", lambda **kwargs: records.append(kwargs)
        )
        return records

    def test_the_audit_record_names_the_provider_actor_and_reason(self, audited):
        with pytest.raises(HTTPException):
            deps_module._enforce_external_absolute_timeout(_request(), _identity())

        assert len(audited) == 1
        record = audited[0]
        assert record["event_type"] == AuditEventType.AUTH_SESSION_EXPIRED
        assert record["outcome"] == AuditOutcome.FAILURE
        assert record["username"] == "who@acme.example"
        assert record["details"] == {"provider": "acme-idp", "reason": "absolute_timeout"}

    def test_the_audit_record_carries_the_callers_address_and_agent(self, audited):
        with pytest.raises(HTTPException):
            deps_module._enforce_external_absolute_timeout(
                _request("mutant-agent/1.0"), _identity()
            )

        assert audited[0]["user_agent"] == "mutant-agent/1.0"
        assert audited[0]["source_ip"] == "10.0.0.7"

    def test_the_401_has_the_exact_machine_readable_body_and_challenge(self, audited):
        with pytest.raises(HTTPException) as exc:
            deps_module._enforce_external_absolute_timeout(_request(), _identity())

        assert exc.value.status_code == 401
        detail: object = exc.value.detail
        assert detail == {
            "code": ERROR_CODE_SESSION_EXPIRED,
            "message": "Your session has expired. Sign in again.",
        }
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_a_live_session_writes_no_audit_record_and_does_not_raise(self, monkeypatch):
        import app.auth.external_session as external_session

        monkeypatch.setattr(external_session, "external_session_expired", lambda _identity: False)
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(
            deps_module.audit_logger, "log", lambda **kwargs: records.append(kwargs)
        )

        deps_module._enforce_external_absolute_timeout(_request(), _identity())

        assert records == []


class TestOptionalAuthHonoursTheRevocationEpoch:
    """``get_optional_current_user`` must apply the same logout-all epoch as
    ``get_current_user`` - otherwise a revoked session still reads as signed in on every
    optional-auth route."""

    USER_UUID = "019ec90a-1b2c-7def-8000-0000000000ee"

    def _token(self, issued_at: int) -> str:
        return jwt.encode(
            {
                "sub": self.USER_UUID,
                "type": TOKEN_TYPE_ACCESS,
                "jti": "optional-epoch-jti",
                "iat": issued_at,
                "exp": int(time.time()) + 600,
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

    @pytest.fixture
    def revocation(self, monkeypatch) -> TokenService:
        service = TokenService()
        service._store = InMemoryStore()
        monkeypatch.setattr(deps_module, "token_service", service)
        monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
        return service

    @pytest.fixture
    def seen_issued_at(self, revocation, monkeypatch) -> list[Any]:
        seen: list[Any] = []
        real = revocation.is_token_revoked

        def spy(*args: Any, **kwargs: Any) -> bool:
            seen.append(kwargs.get("issued_at"))
            return bool(real(*args, **kwargs))

        monkeypatch.setattr(revocation, "is_token_revoked", spy)
        return seen

    def _call(self, token: str) -> Any:
        request = SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"},
            cookies={},
            state=SimpleNamespace(),
            client=SimpleNamespace(host="10.0.0.1"),
            url=SimpleNamespace(path="/api/x"),
            scope={},
        )
        return deps_module.get_optional_current_user(cast(Any, request), cast(Any, None))

    def test_the_tokens_iat_claim_reaches_the_revocation_check(self, seen_issued_at):
        issued = int(time.time()) - 30

        self._call(self._token(issued))

        assert seen_issued_at == [issued]


class TestRequiredAuthPassesItsRequestToTheTimeoutRefusal:
    """``_authenticate_external_token`` must hand the real request on, so the audit
    record names the caller's address and agent rather than "unknown"."""

    def test_the_expired_session_audit_names_the_caller(self, db_session, monkeypatch):
        from app.auth import external_session
        from app.auth.provider_registry import register_verifier
        from app.auth.provider_registry import unregister_verifier

        class _Verifier:
            def verify(self, token: str, request: Request) -> ExternalIdentity | None:
                return ExternalIdentity(
                    provider="mutant-idp",
                    external_id="ext-mutant",
                    email="who@acme.example",
                    auth_time=int(time.time()) - 7200,
                )

        monkeypatch.setattr(settings, "EXTERNAL_SESSION_ABSOLUTE_TIMEOUT_ENFORCED", True)
        monkeypatch.setattr(external_session, "_absolute_timeout_minutes", lambda: 60)
        records: list[dict[str, Any]] = []
        monkeypatch.setattr(
            deps_module.audit_logger, "log", lambda **kwargs: records.append(kwargs)
        )
        register_verifier("mutant-idp", _Verifier())
        try:
            with pytest.raises(HTTPException):
                deps_module._authenticate_external_token(
                    _request("mutant-agent/2.0"), "opaque", db_session
                )
        finally:
            unregister_verifier("mutant-idp")

        assert len(records) == 1
        assert records[0]["user_agent"] == "mutant-agent/2.0"
        assert records[0]["source_ip"] == "10.0.0.7"
