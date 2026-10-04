"""Absolute session timeout for tokens issued by an external identity provider (#1106).

Built-in sessions get ``SESSION_ABSOLUTE_TIMEOUT_MINUTES`` from the refresh-token
row (``token_service._session_within_lifetime``). An external identity provider
owns its own browser session and keeps minting fresh short-lived tokens for as
long as that session lives, so without a check here the core limit never applied
to those users at all.

The backstop keys on ``auth_time`` — when the person actually authenticated —
never on ``iat``: a silently refreshed token is re-issued every minute or so, so
its ``iat`` is always recent and would make the control read as satisfied forever.

What this suite pins, through the real registry and the three real call sites:

* an ``auth_time`` older than the limit is refused on the required-auth path as a
  401 carrying ``detail.code == "session_expired"`` (the SPA branches on it to end
  the provider session, not just the local one);
* the refusal happens BEFORE JIT provisioning writes anything;
* a fresh ``auth_time`` is unaffected;
* a verifier that supplies no ``auth_time`` degrades to "not enforced" (with a
  warning) rather than locking every user out;
* ``auth_time`` may arrive on the dataclass field or in ``raw_claims``;
* the setting and a limit of 0 both switch it off;
* the optional-auth and WebSocket paths refuse the same token (anonymous / closed).
"""

# mypy: disable-error-code="arg-type"
from __future__ import annotations

import logging
import time
import uuid as uuid_pkg
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import websockets as ws_module
from app.api.endpoints.auth import dependencies as deps_module
from app.auth import external_session
from app.auth.provider_registry import ExternalIdentity
from app.auth.provider_registry import register_verifier
from app.auth.provider_registry import unregister_verifier
from app.core.config import settings
from app.models.user import User

PROVIDER = "oidc"
TOKEN = "an-external-token"  # noqa: S105 # nosec B105 — an opaque test string
LIMIT_MINUTES = 60


@pytest.fixture(autouse=True)
def _registry_and_limits(monkeypatch):
    unregister_verifier(PROVIDER)
    monkeypatch.setattr(settings, "EXTERNAL_SESSION_ABSOLUTE_TIMEOUT_ENFORCED", True)
    monkeypatch.setattr(
        external_session, "_absolute_timeout_minutes", lambda: LIMIT_MINUTES, raising=True
    )
    external_session._warned_providers.clear()
    yield
    unregister_verifier(PROVIDER)


class _Verifier:
    def __init__(self, identity: ExternalIdentity):
        self._identity = identity

    def verify(self, token: str, request: Request) -> ExternalIdentity | None:
        return self._identity if token == TOKEN else None


def _identity(**overrides: Any) -> ExternalIdentity:
    unique = uuid_pkg.uuid4().hex[:10]
    defaults: dict[str, Any] = {
        "provider": PROVIDER,
        "external_id": f"ext_{unique}",
        "email": f"external-{unique}@example.com",
        "email_verified": True,
    }
    defaults.update(overrides)
    return ExternalIdentity(**defaults)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("backend", 8080),
            "root_path": "",
            "path": "/api/files",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
            "client": ("10.0.0.7", 40000),
        }
    )


def _register(**overrides: Any) -> ExternalIdentity:
    identity = _identity(**overrides)
    register_verifier(PROVIDER, _Verifier(identity))
    return identity


def _required(db_session) -> str | None:
    """External id of the user the required-auth path resolved, or None."""
    user = deps_module._authenticate_external_token(_request(), TOKEN, db_session)
    return None if user is None else str(user.external_id)


def _stale() -> int:
    return int(time.time()) - (LIMIT_MINUTES * 60) - 5


def _fresh() -> int:
    return int(time.time()) - 60


class TestRequiredAuthPath:
    def test_a_stale_auth_time_is_a_401_with_a_machine_readable_code(self, db_session):
        register_verifier(PROVIDER, _Verifier(_identity(auth_time=_stale())))

        with pytest.raises(HTTPException) as exc:
            deps_module._authenticate_external_token(_request(), TOKEN, db_session)

        assert exc.value.status_code == 401
        assert isinstance(exc.value.detail, dict)
        assert exc.value.detail["code"] == "session_expired"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_nothing_is_provisioned_for_an_expired_session(self, db_session):
        identity = _identity(auth_time=_stale())
        register_verifier(PROVIDER, _Verifier(identity))

        with pytest.raises(HTTPException):
            deps_module._authenticate_external_token(_request(), TOKEN, db_session)

        assert db_session.query(User).filter(User.external_id == identity.external_id).count() == 0

    def test_a_fresh_auth_time_authenticates(self, db_session):
        identity = _register(auth_time=_fresh())

        assert _required(db_session) == identity.external_id

    def test_auth_time_in_raw_claims_is_honoured(self, db_session):
        register_verifier(PROVIDER, _Verifier(_identity(raw_claims={"auth_time": _stale()})))

        with pytest.raises(HTTPException) as exc:
            deps_module._authenticate_external_token(_request(), TOKEN, db_session)

        assert exc.value.status_code == 401

    def test_iat_is_never_used_as_the_authentication_time(self, db_session):
        """A refreshed token's ``iat`` is always recent; an old one proves nothing
        either way. Only ``auth_time`` means "when the person signed in"."""
        identity = _register(raw_claims={"iat": _stale()})

        assert _required(db_session) == identity.external_id


class TestDegradesInsteadOfLockingEveryoneOut:
    def test_no_auth_time_is_not_enforced(self, db_session):
        identity = _register()

        assert _required(db_session) == identity.external_id

    def test_no_auth_time_logs_a_warning_once_per_provider(self, db_session, caplog):
        register_verifier(PROVIDER, _Verifier(_identity()))

        with caplog.at_level(logging.WARNING, logger=external_session.__name__):
            deps_module._authenticate_external_token(_request(), TOKEN, db_session)
            deps_module._authenticate_external_token(_request(), TOKEN, db_session)

        warnings = [r for r in caplog.records if "auth_time" in r.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.parametrize("bad", ["not-a-number", True, None, 1.5e30])
    def test_an_unusable_auth_time_is_not_enforced(self, db_session, bad):
        identity = _register(raw_claims={"auth_time": bad})

        assert _required(db_session) == identity.external_id

    def test_the_setting_switches_it_off(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "EXTERNAL_SESSION_ABSOLUTE_TIMEOUT_ENFORCED", False)
        identity = _register(auth_time=_stale())

        assert _required(db_session) == identity.external_id

    def test_a_zero_limit_switches_it_off(self, db_session, monkeypatch):
        monkeypatch.setattr(external_session, "_absolute_timeout_minutes", lambda: 0)
        identity = _register(auth_time=_stale())

        assert _required(db_session) == identity.external_id


class TestTheOtherTwoCallSites:
    def test_optional_auth_treats_an_expired_session_as_anonymous(self, db_session):
        register_verifier(PROVIDER, _Verifier(_identity(auth_time=_stale())))

        assert deps_module.get_optional_current_user(_request(), db_session) is None

    def test_optional_auth_still_accepts_a_fresh_session(self, db_session):
        identity = _register(auth_time=_fresh())

        user = deps_module.get_optional_current_user(_request(), db_session)

        assert user is not None and user.external_id == identity.external_id

    def test_the_websocket_refuses_an_expired_session(self, db_session):
        register_verifier(PROVIDER, _Verifier(_identity(auth_time=_stale())))

        assert ws_module._try_authenticate_token(TOKEN, db_session) is None

    def test_the_websocket_accepts_a_fresh_session(self, db_session):
        identity = _register(auth_time=_fresh())

        user = ws_module._try_authenticate_token(TOKEN, db_session)

        assert user is not None and user.external_id == identity.external_id


class TestTheLimitComesFromTheLayeredAuthConfig:
    def test_it_reads_session_absolute_timeout_minutes(self, monkeypatch):
        """DB (admin UI) > .env > default — the same value built-in sessions use."""
        monkeypatch.undo()

        class _Stub:
            session_absolute_timeout_minutes = 123

        monkeypatch.setattr("app.core.auth_settings.get_process_auth_settings", lambda: _Stub())

        assert external_session._absolute_timeout_minutes() == 123
