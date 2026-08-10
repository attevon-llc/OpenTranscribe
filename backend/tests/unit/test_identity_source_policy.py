"""The deployment-level identity-source model (issue #354).

Three settings decide who may authenticate how:

* ``local_enabled`` — may accounts holding a local password sign in at all?
* ``allow_registration`` — may anyone create their own account?
* per-user ``auth_type`` / ``allow_local_fallback`` — which method for this user.

Before this, the first two were admin-UI toggles wired to nothing and
``/token`` had no local-auth check whatsoever, so a deployment fronted by LDAP
still accepted local passwords AND still let anyone self-register. The reporter
running LDAP saw exactly that.

The break-glass rule is load-bearing: auth configuration is super_admin-gated, so
if disabling local auth also locked out the super_admin there would be no way to
undo a misconfigured IdP.
"""

# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.endpoints.auth import authenticators
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER


def _user(role=ROLE_USER, is_active=True):
    return SimpleNamespace(role=role, is_active=is_active, auth_type="local")


@pytest.fixture
def local_enabled(monkeypatch):
    """Control the DB-backed ``local_enabled`` setting."""

    def _set(value: bool):
        monkeypatch.setattr(
            authenticators,
            "get_auth_settings",
            lambda db: SimpleNamespace(local_enabled=value),
        )

    return _set


class TestLocalAuthPermitted:
    def test_permitted_when_local_auth_is_on(self, local_enabled):
        local_enabled(True)
        assert authenticators._local_auth_permitted(None, _user()) is True

    def test_refused_for_ordinary_user_when_off(self, local_enabled):
        local_enabled(False)
        assert authenticators._local_auth_permitted(None, _user()) is False

    def test_refused_for_plain_admin_when_off(self, local_enabled):
        """`admin` is not the break-glass tier — only `super_admin` is."""
        local_enabled(False)
        assert authenticators._local_auth_permitted(None, _user(role=ROLE_ADMIN)) is False

    def test_super_admin_keeps_break_glass_access(self, local_enabled):
        local_enabled(False)
        assert authenticators._local_auth_permitted(None, _user(role=ROLE_SUPER_ADMIN)) is True

    def test_inactive_super_admin_gets_no_exemption(self, local_enabled):
        local_enabled(False)
        user = _user(role=ROLE_SUPER_ADMIN, is_active=False)
        assert authenticators._local_auth_permitted(None, user) is False

    def test_unknown_identifier_is_refused_when_off(self, local_enabled):
        local_enabled(False)
        assert authenticators._local_auth_permitted(None, None) is False


class TestAuthMethodsContract:
    """`/auth/methods` drives what the login page renders."""

    def test_response_model_carries_the_new_flags(self):
        from app.schemas.user import AuthMethodsResponse

        fields = AuthMethodsResponse.model_fields
        assert "local_enabled" in fields
        assert "allow_registration" in fields

    def test_local_is_no_longer_hardcoded_into_methods(self):
        import inspect

        from app.api.endpoints.auth import methods

        source = inspect.getsource(methods.get_auth_methods)
        # Strip comments — the fix documents the old line in one, and that is
        # deliberate history, not the behaviour under test.
        code = "\n".join(line.split("#")[0] for line in source.splitlines())
        assert 'methods = ["local"]' not in code, (
            "'local' must be conditional on local_enabled, not always advertised"
        )
        assert "local_enabled" in code

    def test_banner_copy_comes_from_the_same_place_as_the_flag(self):
        """Enabling the banner in the admin UI used to render an EMPTY banner."""
        import inspect

        from app.api.endpoints.auth import methods

        for fn in (methods.get_auth_methods, methods.get_login_banner):
            source = inspect.getsource(fn)
            assert "settings.LOGIN_BANNER_TEXT" not in source
            assert "settings.LOGIN_BANNER_CLASSIFICATION" not in source
