"""One definition of "may this account use a local password?".

The rule used to exist twice — in ``direct_auth._validate_user_can_authenticate``
(raw SQL path) and in ``core.security.authenticate_user`` (ORM path) — and the
two disagreed. Only the former hard-blocked LDAP. ``_authenticate_local_user``
tries the raw path first and then *falls through* to the ORM path, so an LDAP
account with ``allow_local_fallback`` set authenticated against a local bcrypt
hash, breaking the "LDAP users never have a local password" invariant.

Both call sites now delegate to ``auth.utils.local_password_allowed``.
"""

from __future__ import annotations

import inspect

import pytest

from app.auth.constants import AUTH_TYPE_KEYCLOAK
from app.auth.constants import AUTH_TYPE_LDAP
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import AUTH_TYPE_PKI
from app.auth.utils import local_fallback_permitted_for
from app.auth.utils import local_password_allowed


class TestLocalPasswordAllowed:
    def test_local_always_allowed(self):
        assert local_password_allowed(AUTH_TYPE_LOCAL, False)[0] is True

    @pytest.mark.parametrize("flag", [False, True])
    def test_ldap_never_allowed_even_with_the_flag(self, flag):
        """The regression: the flag must not override the LDAP hard-block."""
        allowed, reason = local_password_allowed(AUTH_TYPE_LDAP, flag)
        assert allowed is False
        assert "never has a local password" in reason

    @pytest.mark.parametrize("auth_type", [AUTH_TYPE_PKI, AUTH_TYPE_KEYCLOAK])
    def test_external_requires_opt_in(self, auth_type):
        assert local_password_allowed(auth_type, False)[0] is False
        assert local_password_allowed(auth_type, True)[0] is True

    @pytest.mark.parametrize("auth_type", ["bogus", "", None])
    def test_unknown_auth_type_fails_closed(self, auth_type):
        allowed, reason = local_password_allowed(auth_type, True)
        assert allowed is False
        assert "unrecognised" in reason


class TestLocalFallbackPermittedFor:
    """Write-side guard: the flag is only meaningful for pki/keycloak."""

    @pytest.mark.parametrize("auth_type", [AUTH_TYPE_PKI, AUTH_TYPE_KEYCLOAK])
    def test_permitted(self, auth_type):
        assert local_fallback_permitted_for(auth_type) is True

    @pytest.mark.parametrize("auth_type", [AUTH_TYPE_LOCAL, AUTH_TYPE_LDAP, "bogus", None])
    def test_refused(self, auth_type):
        assert local_fallback_permitted_for(auth_type) is False


class TestBothCallSitesDelegate:
    """Pin the de-duplication so the two paths cannot drift apart again."""

    def test_direct_auth_delegates(self):
        from app.auth import direct_auth

        source = inspect.getsource(direct_auth._validate_user_can_authenticate)
        assert "local_password_allowed" in source
        # The old inline literals must be gone.
        assert '"ldap"' not in source
        assert '("pki", "keycloak")' not in source

    def test_orm_path_delegates(self):
        from app.core import security

        source = inspect.getsource(security.authenticate_user)
        assert "local_password_allowed" in source
        assert 'user.auth_type != "local"' not in source
