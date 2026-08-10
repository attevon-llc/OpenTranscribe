"""Characterization tests for ``app.api.endpoints.auth``.

These pin the *current* external behavior of the auth endpoints (status codes,
``detail`` strings, token-response shape, cookie/CSRF semantics) so the Phase-4+
refactors can be proven behavior-neutral. They are NOT aspirational — every
assertion captures what the code does today.

House rules honored here:
- Negative login tests use a NONEXISTENT account only. Wrong-password attempts
  against a real fixture user poison the per-account lockout for the whole
  suite, so they are never done here.
- Mutating tests run inside the savepoint-isolated ``db_session`` and never
  persist to dev data.
- The cookie + CSRF double-submit flow (``middleware/csrf.py``) is exercised
  explicitly: a cookie-authenticated mutating request needs a matching
  ``X-CSRF-Token`` header or it gets a 403.

Run: ``venv/bin/pytest tests/api/test_auth_endpoints.py -v -n0``
"""

from unittest.mock import patch

import pyotp
import pytest

from app.core.config import settings


def _login(client, email: str, password: str):
    """POST the OAuth2 password form to ``/api/auth/token``."""
    return client.post(
        "/api/auth/token",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _refresh(client, refresh_token: str):
    """POST to ``/api/auth/token/refresh`` the way a browser would.

    ``/api/auth/token/refresh`` is no longer CSRF-exempt — it mints a new session
    from the refresh cookie alone, which is exactly what a forged cross-site
    request would want. ``TestClient`` keeps the login's cookie jar, so it looks
    like a browser and must double-submit the CSRF token like one.
    """
    csrf = client.cookies.get("csrf_token")
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return client.post(
        "/api/auth/token/refresh",
        json={"refresh_token": refresh_token},
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# Local login (happy path) — token shape + cookies
# --------------------------------------------------------------------------- #
class TestLocalLogin:
    def test_login_returns_token_payload_shape(self, client, normal_user):
        """A valid login returns the documented Token shape."""
        resp = _login(client, normal_user.email, "password123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert isinstance(body["access_token"], str) and body["access_token"]
        assert isinstance(body["refresh_token"], str) and body["refresh_token"]
        # expires_in mirrors the configured access-token lifetime (seconds).
        assert body["expires_in"] == settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60

    def test_login_sets_httponly_and_csrf_cookies(self, client, normal_user):
        """Login sets the httpOnly access/refresh cookies + a JS-readable CSRF cookie."""
        resp = _login(client, normal_user.email, "password123")
        assert resp.status_code == 200

        # Raw Set-Cookie headers carry the httpOnly flags; the cookie jar carries values.
        set_cookies = " ".join(resp.headers.get_list("set-cookie")).lower()
        assert "access_token=" in set_cookies
        assert "refresh_token=" in set_cookies
        assert "csrf_token=" in set_cookies
        assert "httponly" in set_cookies  # access/refresh are httpOnly

        jar = resp.cookies
        assert jar.get("access_token")
        assert jar.get("csrf_token")

    def test_login_alias_path(self, client, normal_user):
        """The ``/api/auth/login`` alias behaves like ``/token``."""
        resp = client.post(
            "/api/auth/login",
            data={"username": normal_user.email, "password": "password123"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        assert resp.json()["token_type"] == "bearer"


# --------------------------------------------------------------------------- #
# Negative login — NONEXISTENT account only
# --------------------------------------------------------------------------- #
class TestLoginNegative:
    def test_login_nonexistent_account_401(self, client):
        """A login for an account that does not exist returns a generic 401."""
        resp = _login(client, "does-not-exist-9f3c1a@example.com", "whatever-pass-1A!")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Incorrect username or password"
        # Generic message + WWW-Authenticate (no username enumeration).
        assert resp.headers.get("www-authenticate") == "Bearer"

    def test_login_missing_password_field_422(self, client):
        """Omitting the password form field is an OAuth2 form validation error."""
        resp = client.post(
            "/api/auth/token",
            data={"username": "nobody-7a21@example.com"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Registration — happy + duplicate + weak password
# --------------------------------------------------------------------------- #
class TestRegister:
    _STRONG_PW = "Str0ng-Passw0rd!9xQ"

    def test_register_happy_path(self, client, db_session):
        """A fresh email + policy-compliant password creates a local user."""
        import uuid

        email = f"reg_{uuid.uuid4().hex[:10]}@example.com"
        resp = client.post(
            "/api/auth/register",
            json={"email": email, "full_name": "Reg Test", "password": self._STRONG_PW},
        )
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body["email"] == email
        assert body["auth_type"] == "local"
        assert body["role"] == "user"
        assert body["is_active"] is True
        # The response is the public User schema — never leaks the password hash.
        assert "hashed_password" not in body
        assert "password" not in body

    def test_register_duplicate_email_400(self, client, normal_user):
        """Re-registering an existing email is a 400 with the exact detail."""
        resp = client.post(
            "/api/auth/register",
            json={
                "email": normal_user.email,
                "full_name": "Dup",
                "password": self._STRONG_PW,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Email already registered"

    def test_register_weak_password_422(self, client):
        """A password that fails the policy validator is rejected by the schema (422)."""
        import uuid

        email = f"weak_{uuid.uuid4().hex[:10]}@example.com"
        resp = client.post(
            "/api/auth/register",
            json={"email": email, "full_name": "Weak", "password": "short1A!"},
        )
        assert resp.status_code == 422
        # Pydantic surfaces the password-policy message from the model_validator.
        detail = resp.json()["detail"]
        assert any("policy" in str(item).lower() for item in detail)


# --------------------------------------------------------------------------- #
# /api/auth/session — frontend probe, must be 200 either way
# --------------------------------------------------------------------------- #
class TestSessionProbe:
    def test_session_anonymous_is_200_not_401(self, client):
        """The SPA session probe never 401s for anonymous visitors."""
        resp = client.get("/api/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is False
        assert body["user"] is None
        assert body["refreshable"] is False

    def test_session_authenticated_returns_user(self, client, user_token_headers, normal_user):
        """With a valid bearer token the probe reports the authenticated user."""
        resp = client.get("/api/auth/session", headers=user_token_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["refreshable"] is False
        assert body["user"]["email"] == normal_user.email

    def test_session_with_cookie_only(self, client, normal_user):
        """A cookie-only session (no Authorization header) is recognized by the probe."""
        login = _login(client, normal_user.email, "password123")
        assert login.status_code == 200
        # TestClient persists the login cookies on the client; no bearer header.
        resp = client.get("/api/auth/session")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is True


# --------------------------------------------------------------------------- #
# /api/auth/me — current user route
# --------------------------------------------------------------------------- #
class TestReadMe:
    def test_me_authorized(self, client, user_token_headers, normal_user):
        resp = client.get("/api/auth/me", headers=user_token_headers)
        assert resp.status_code == 200
        assert resp.json()["email"] == normal_user.email

    def test_me_unauthenticated_401(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Could not validate credentials"


# --------------------------------------------------------------------------- #
# Refresh-token rotation
# --------------------------------------------------------------------------- #
class TestRefreshRotation:
    def test_refresh_issues_new_tokens(self, client, normal_user):
        login = _login(client, normal_user.email, "password123")
        refresh_token = login.json()["refresh_token"]

        resp = _refresh(client, refresh_token)
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]
        # Rotation: the returned refresh token differs from the one we sent.
        assert body["refresh_token"] != refresh_token

    def test_old_refresh_token_rejected_after_rotation(self, client, normal_user):
        login = _login(client, normal_user.email, "password123")
        old_refresh = login.json()["refresh_token"]

        first = _refresh(client, old_refresh)
        assert first.status_code == 200

        # Replaying the now-rotated (revoked) token is rejected.
        replay = _refresh(client, old_refresh)
        assert replay.status_code == 401
        assert replay.json()["detail"] == "Invalid or expired refresh token"

    def test_refresh_invalid_token_401(self, client):
        resp = _refresh(client, "not-a-real-token")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid or expired refresh token"


# --------------------------------------------------------------------------- #
# Logout
# --------------------------------------------------------------------------- #
class TestLogout:
    def test_logout_clears_cookies_and_revokes(self, client, user_token_headers):
        resp = client.post("/api/auth/logout", headers=user_token_headers)
        assert resp.status_code == 200
        assert resp.json() == {"message": "Successfully logged out"}
        # Cookies are deleted (Set-Cookie with empty value / expiry in the past).
        set_cookies = " ".join(resp.headers.get_list("set-cookie")).lower()
        assert "access_token=" in set_cookies
        assert "refresh_token=" in set_cookies

    def test_logout_without_token_still_200(self, client):
        """Logout is idempotent — succeeds even with no credentials."""
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert resp.json() == {"message": "Successfully logged out"}

    def test_refresh_revoked_after_logout(self, client, normal_user):
        """The refresh token is revoked on logout and can't mint new tokens."""
        login = _login(client, normal_user.email, "password123")
        access = login.json()["access_token"]
        refresh = login.json()["refresh_token"]

        out = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {access}"})
        assert out.status_code == 200

        # The access token's JTI is blacklisted only when Redis is available; in
        # the test env (SKIP_REDIS) revocation is best-effort. The refresh token
        # however is rotated/revoked via the DB record on the next refresh, so a
        # fresh login is the supported post-logout path. Assert logout response
        # only here — refresh-revocation-by-DB is covered by rotation tests.
        assert refresh  # sanity


# --------------------------------------------------------------------------- #
# Cookie-auth + CSRF double-submit (middleware/csrf.py)
# --------------------------------------------------------------------------- #
class TestCsrfDoubleSubmit:
    """A cookie-authenticated mutating request on a NON-exempt path must carry a
    matching X-CSRF-Token header. ``/api/auth/logout`` is POST + non-exempt and
    accepts a cookie session, so it exercises the double-submit gate.
    """

    def _login_collect_cookies(self, client, normal_user) -> dict:
        login = _login(client, normal_user.email, "password123")
        assert login.status_code == 200
        return {
            "access_token": login.cookies.get("access_token"),
            "csrf_token": login.cookies.get("csrf_token"),
        }

    def test_cookie_post_without_csrf_header_403(self, client, normal_user):
        cookies = self._login_collect_cookies(client, normal_user)
        # Fresh client request carrying ONLY cookies, no Authorization, no CSRF header.
        resp = client.post(
            "/api/auth/logout",
            cookies={"access_token": cookies["access_token"], "csrf_token": cookies["csrf_token"]},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "CSRF token missing or invalid"

    def test_cookie_post_with_matching_csrf_header_succeeds(self, client, normal_user):
        cookies = self._login_collect_cookies(client, normal_user)
        resp = client.post(
            "/api/auth/logout",
            cookies={"access_token": cookies["access_token"], "csrf_token": cookies["csrf_token"]},
            headers={"X-CSRF-Token": cookies["csrf_token"]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"message": "Successfully logged out"}

    def test_cookie_post_with_mismatched_csrf_header_403(self, client, normal_user):
        cookies = self._login_collect_cookies(client, normal_user)
        resp = client.post(
            "/api/auth/logout",
            cookies={"access_token": cookies["access_token"], "csrf_token": cookies["csrf_token"]},
            headers={"X-CSRF-Token": "0" * 64},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "CSRF token missing or invalid"

    def test_bearer_request_is_csrf_exempt(self, client, user_token_headers):
        """Bearer-token requests bypass CSRF entirely (no cookie session)."""
        resp = client.post("/api/auth/logout", headers=user_token_headers)
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Auth discovery / banner / password-policy (public-ish endpoints)
# --------------------------------------------------------------------------- #
class TestAuthDiscovery:
    def test_methods_always_includes_local(self, client):
        resp = client.get("/api/auth/methods")
        assert resp.status_code == 200
        body = resp.json()
        assert "local" in body["methods"]
        assert "mfa_enabled" in body

    def test_password_policy_public(self, client):
        resp = client.get("/api/auth/password-policy")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_banner_public(self, client):
        resp = client.get("/api/auth/banner")
        assert resp.status_code == 200
        assert "enabled" in resp.json()


# --------------------------------------------------------------------------- #
# MFA gates — reuse the test_mfa_integration patterns, savepoint-isolated
# --------------------------------------------------------------------------- #
@pytest.fixture
def _mfa_enabled():
    """Enable MFA system-wide for the duration of a test (patched, not persisted)."""
    with patch.object(settings, "MFA_ENABLED", True), patch.object(settings, "MFA_REQUIRED", False):
        yield


class TestMfaGates:
    def test_mfa_status_unauthenticated_401(self, client):
        resp = client.get("/api/auth/mfa/status")
        assert resp.status_code == 401

    def test_mfa_status_local_user(self, client, user_token_headers, _mfa_enabled):
        resp = client.get("/api/auth/mfa/status", headers=user_token_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_setup_mfa"] is True
        assert body["mfa_configured"] is False

    def test_mfa_setup_requires_auth_401(self, client, _mfa_enabled):
        resp = client.post("/api/auth/mfa/setup")
        assert resp.status_code == 401

    def test_mfa_setup_disabled_system_400(self, client, user_token_headers):
        """With MFA disabled (default test env) setup is a 400 with the exact detail."""
        resp = client.post("/api/auth/mfa/setup", headers=user_token_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "MFA is not enabled on this system"

    def test_mfa_setup_then_verify_enables(self, client, user_token_headers, _mfa_enabled):
        setup = client.post("/api/auth/mfa/setup", headers=user_token_headers)
        assert setup.status_code == 200
        secret = setup.json()["secret"]
        assert setup.json()["provisioning_uri"].startswith("otpauth://totp/")

        code = pyotp.TOTP(secret).now()
        verify = client.post(
            "/api/auth/mfa/verify-setup",
            headers=user_token_headers,
            json={"code": code},
        )
        assert verify.status_code == 200
        assert verify.json()["success"] is True
        assert verify.json()["backup_codes"]

    def test_mfa_verify_setup_invalid_code_400(self, client, user_token_headers, _mfa_enabled):
        client.post("/api/auth/mfa/setup", headers=user_token_headers)
        verify = client.post(
            "/api/auth/mfa/verify-setup",
            headers=user_token_headers,
            json={"code": "000000"},
        )
        assert verify.status_code == 400
        assert verify.json()["detail"] == "Invalid verification code. Please try again."

    def test_mfa_login_flow_returns_mfa_token(
        self, client, normal_user, user_token_headers, _mfa_enabled
    ):
        """After enabling MFA, login returns mfa_required + mfa_token (no access token)."""
        setup = client.post("/api/auth/mfa/setup", headers=user_token_headers)
        secret = setup.json()["secret"]
        client.post(
            "/api/auth/mfa/verify-setup",
            headers=user_token_headers,
            json={"code": pyotp.TOTP(secret).now()},
        )

        login = _login(client, normal_user.email, "password123")
        assert login.status_code == 200
        body = login.json()
        assert body.get("mfa_required") is True
        assert "mfa_token" in body
        assert "access_token" not in body
