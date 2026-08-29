"""Regression tests for the SAML ACS RelayState open-redirect guard.

``relay_state.startswith("/")`` alone is not a same-origin check: browsers resolve
``"//evil.com"`` as a protocol-relative absolute URL and ``"/\\evil.com"`` the same
way (backslash-as-slash normalization), so either sails through a bare prefix check
and would carry the browser off-site right after the ACS handler sets the auth
cookies. ``_sanitize_relay_state`` is the pure function the handler delegates to;
these tests pin its behaviour without needing a live IdP or DB.
"""

from __future__ import annotations

import pytest

from app.api.endpoints.auth.saml import _POST_LOGIN_REDIRECT
from app.api.endpoints.auth.saml import _sanitize_relay_state


class TestSanitizeRelayState:
    @pytest.mark.parametrize(
        "malicious",
        [
            "//evil.com",
            "//evil.com/path",
            "https://evil.com",
            "http://evil.com",
            "/\\evil.com",
            "/\\/evil.com",
        ],
    )
    def test_off_origin_forms_are_rejected(self, malicious: str) -> None:
        assert _sanitize_relay_state(malicious) == _POST_LOGIN_REDIRECT

    @pytest.mark.parametrize(
        "safe",
        [
            "/dashboard",
            "/files/123",
            "/",
            "/a/b/c?x=1",
        ],
    )
    def test_same_origin_paths_pass_through(self, safe: str) -> None:
        assert _sanitize_relay_state(safe) == safe

    def test_none_and_empty_fall_back_to_default(self) -> None:
        assert _sanitize_relay_state(None) == _POST_LOGIN_REDIRECT
        assert _sanitize_relay_state("") == _POST_LOGIN_REDIRECT

    def test_relative_path_without_leading_slash_is_rejected(self) -> None:
        # Not itself a redirect vector, but also not the documented convention —
        # the handler's contract is "same-origin absolute path or the default".
        assert _sanitize_relay_state("dashboard") == _POST_LOGIN_REDIRECT
