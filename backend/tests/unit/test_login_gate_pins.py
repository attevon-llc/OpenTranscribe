"""Pins for password-login gates that no other test made fail.

Each test names the branch it pins. All are pure unit tests: the collaborators the
handler reaches through ``login.py`` globals are replaced, so nothing touches a
database, Redis or the rate limiter.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi import Response

from app.api.endpoints.auth import login as login_module
from app.auth.roles import ROLE_SUPER_ADMIN
from app.models.user import User

pytestmark = pytest.mark.unit

USER_UUID = "11111111-2222-3333-4444-555555555555"


def _unwrap(func):
    """Strip the slowapi rate-limit wrapper so the endpoint can be called directly."""
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


def _account(role: str, allow_local_fallback: bool) -> User:
    return cast(User, SimpleNamespace(role=role, allow_local_fallback=allow_local_fallback))


class TestTestingShortcutNeverRunsHardened:
    """``_perform_authentication``: ``TESTING=true`` is ignored on a hardened deployment."""

    def test_hardened_deployment_uses_the_real_authenticator(self, monkeypatch):
        monkeypatch.setenv("TESTING", "true")
        monkeypatch.setattr(login_module.settings, "ENVIRONMENT", "production")
        assert login_module.settings.is_hardened is True

        def _shortcut(db, username, password):
            raise AssertionError("the TESTING shortcut ran on a hardened deployment")

        def _real(db, username, password):
            raise HTTPException(status_code=401, detail="Incorrect username or password")

        monkeypatch.setattr(login_module, "_authenticate_testing_user", _shortcut)
        monkeypatch.setattr(login_module, "_authenticate_production_user", _real)

        result = login_module._perform_authentication(cast(Any, None), "a@example.com", "x")

        assert result == (False, "", {}, "")


class TestLockoutExemptionNeedsBothConditions:
    """``_is_exempt_from_lockout``: super_admin AND allow_local_fallback, nothing less."""

    def test_super_admin_with_fallback_is_exempt(self):
        assert login_module._is_exempt_from_lockout(_account(ROLE_SUPER_ADMIN, True)) is True

    def test_super_admin_without_fallback_is_not_exempt(self):
        assert login_module._is_exempt_from_lockout(_account(ROLE_SUPER_ADMIN, False)) is False

    @pytest.mark.parametrize("role", ["admin", "user"])
    def test_other_roles_with_fallback_are_not_exempt(self, role):
        assert login_module._is_exempt_from_lockout(_account(role, True)) is False

    def test_unresolved_account_is_not_exempt(self):
        assert login_module._is_exempt_from_lockout(None) is False


class TestLockoutOverridesValidCredentials:
    """``login_for_access_token``: a locked account is refused even with the right password."""

    @pytest.fixture
    def handler_env(self, monkeypatch):
        reached: list[str] = []
        monkeypatch.setattr(
            login_module,
            "_perform_authentication",
            lambda db, u, p: (True, USER_UUID, {}, "local"),
        )
        monkeypatch.setattr(
            login_module, "_get_client_info", lambda request: ("10.0.0.1", "pytest")
        )
        monkeypatch.setattr(
            login_module,
            "get_auth_settings",
            lambda db: SimpleNamespace(
                account_lockout_threshold=5, account_lockout_duration_minutes=15
            ),
        )
        monkeypatch.setattr(login_module, "_resolve_lockout_account", lambda db, u, s: None)
        monkeypatch.setattr(
            login_module,
            "get_lockout_info",
            lambda ident: {"failed_attempts": 5, "lockout_count": 1},
        )
        monkeypatch.setattr(login_module.audit_logger, "log_account_lockout", lambda **kw: None)

        def _role(db, uuid_str, data) -> str:
            reached.append("role")
            return "user"

        monkeypatch.setattr(login_module, "_get_user_role", _role)
        return reached

    def _call(self):
        return _unwrap(login_module.login_for_access_token)(
            request=cast(Any, None),
            response=Response(),
            form_data=SimpleNamespace(username="a@example.com", password="right"),
            db=cast(Any, None),
        )

    def test_locked_account_with_valid_password_gets_the_uniform_401(
        self, monkeypatch, handler_env
    ):
        until = datetime.now(UTC) + timedelta(minutes=15)
        monkeypatch.setattr(login_module, "check_and_record_attempt", lambda *a, **k: (True, until))

        with pytest.raises(HTTPException) as exc:
            self._call()

        assert exc.value.status_code == 401
        assert exc.value.detail == "Incorrect username or password"
        assert handler_env == [], "a locked login went on to build a session"
