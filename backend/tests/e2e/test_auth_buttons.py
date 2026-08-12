# mypy: disable-error-code="operator,union-attr"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
"""
E2E Tests for Login Page Auth Buttons and Login Flows (Playwright)

Tests verify in a real browser:
- Login page displays correct auth buttons based on enabled auth methods
- Local login with email/password works end-to-end
- LDAP login with username/password works end-to-end
- The OIDC/SSO button appears and redirects correctly
- PKI certificate button appears when enabled
- Login form validation (empty fields, invalid email)
- Thumbnails/gallery load after successful login
- Logout works and returns to login page

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173
- Backend at localhost:5174
- LDAP container running (for LDAP tests)
- Keycloak container running (for Keycloak tests)

Run:
    pytest backend/tests/e2e/test_auth_buttons.py -v
    DISPLAY=:13 pytest backend/tests/e2e/test_auth_buttons.py -v --headed
"""

import os
from typing import cast

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

# This module used to define its own ``FRONTEND_URL``/``BACKEND_URL`` constants here.
# A module constant is evaluated at import time, so it could not see ``--base-url`` and
# this file always drove whatever was on the default ports — even when the run was aimed
# at an isolated stack (issue #431). Every test below takes conftest's ``base_url``
# fixture instead. No ``backend_url`` counterpart is needed: the API calls here are made
# by the browser as same-origin ``fetch``, so they follow ``base_url`` automatically (the
# old ``BACKEND_URL`` constant was never read).

# Test credentials
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "password"
LDAP_USERNAME = "ldap-admin"
LDAP_PASSWORD = "admin_password"

# Identifiers that resolve to NO account, local or LDAP. Failing logins must target
# these: account lockout is per-account and progressive (app/auth/lockout.py keys the
# counter on the resolved account's email via canonical_identifier), so a wrong password
# aimed at a real account poisons every later test that logs in as it.
NO_SUCH_LOCAL_ACCOUNT = "nosuchuser-e2e@example.com"
NO_SUCH_LDAP_ACCOUNT = "ldap-nosuchuser-e2e"


def _login_local(page: Page, email: str, password: str):
    """Helper: log in with email/password via the local auth form."""
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("button[type=submit]")


def _wait_for_gallery(page: Page, timeout: int = 15000):
    """Helper: wait for the gallery/main page to load after login."""
    # Wait for the SPA NAVIGATION itself — the login form / navbar state can
    # update a beat before goto("/") completes, so element-based waits race
    # any later assertion on page.url.
    page.wait_for_url(lambda url: "/login" not in url, timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)


def _logout(page: Page):
    """Helper: log out by clicking the user menu dropdown."""
    page.click(".user-button")
    page.wait_for_selector(".dropdown-item", state="visible", timeout=5000)
    logout_item = page.locator(
        ".dropdown-item:has-text('Logout'), .dropdown-item:has-text('Sign Out')"
    )
    if logout_item.count() > 0:
        logout_item.first.click()
    page.wait_for_url("**/login**", timeout=10000)


# ===== Auth Methods Discovery Tests =====


class TestLoginPageAuthButtons:
    """Test that the login page shows correct auth buttons based on backend config."""

    def test_login_page_loads(self, page: Page, base_url: str):
        """Login page loads with email and password fields."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        assert page.locator("#email").is_visible()
        assert page.locator("#password").is_visible()
        assert page.locator("button[type=submit]").is_visible()

    def test_auth_methods_api_returns(self, page: Page, base_url: str):
        """Backend /api/auth/methods returns valid response."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        # Call the API directly from the browser context
        result = page.evaluate(
            """
            async () => {
                const response = await fetch('/api/auth/methods');
                return await response.json();
            }
        """
        )

        assert "methods" in result
        assert "local" in result["methods"]
        assert isinstance(result["oidc_enabled"], bool)
        assert isinstance(result["pki_enabled"], bool)
        assert isinstance(result["ldap_enabled"], bool)

    def test_oidc_button_visible_when_enabled(self, page: Page, base_url: str):
        """The SSO button appears when oidc_enabled is true."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        # Check if Keycloak is enabled via API
        methods = page.evaluate(
            """
            async () => {
                const r = await fetch('/api/auth/methods');
                return await r.json();
            }
        """
        )

        oidc_button = page.locator("button.oidc-button")

        if methods.get("oidc_enabled"):
            expect(oidc_button).to_be_visible()
            assert (
                "SSO" in oidc_button.text_content() or "sso" in oidc_button.text_content().lower()
            )
        else:
            expect(oidc_button).to_have_count(0)

    def test_pki_button_visible_when_enabled(self, page: Page, base_url: str):
        """PKI/Certificate button appears when pki_enabled is true."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        methods = page.evaluate(
            """
            async () => {
                const r = await fetch('/api/auth/methods');
                return await r.json();
            }
        """
        )

        pki_button = page.locator("button.pki-button")

        if methods.get("pki_enabled"):
            expect(pki_button).to_be_visible()
            text = pki_button.text_content().lower()
            assert "certificate" in text
        else:
            expect(pki_button).to_have_count(0)

    def test_external_auth_divider_visible(self, page: Page, base_url: str):
        """'Or continue with' divider appears when external auth is enabled."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        methods = page.evaluate(
            """
            async () => {
                const r = await fetch('/api/auth/methods');
                return await r.json();
            }
        """
        )

        divider = page.locator(".auth-divider")

        if methods.get("oidc_enabled") or methods.get("pki_enabled"):
            expect(divider).to_be_visible()
        else:
            expect(divider).to_have_count(0)


# ===== Local Auth Login Tests =====


class TestLocalLogin:
    """Test local email/password login flow through the browser."""

    def test_local_login_success(self, page: Page, base_url: str):
        """Admin login with email/password works and redirects to gallery."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        _login_local(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        _wait_for_gallery(page)

        # Should NOT be on login page
        assert "/login" not in page.url

    def test_local_login_shows_gallery(self, page: Page, base_url: str):
        """After login, gallery page displays correctly with content."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        _login_local(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        _wait_for_gallery(page)

        # Wait for gallery content to load (gallery container or file cards)
        page.wait_for_load_state("networkidle", timeout=10000)

        # Verify we have a main content area
        body_text = page.text_content("body")
        assert body_text is not None

    def test_local_login_invalid_password(self, page: Page, base_url: str):
        """Bad local credentials are rejected.

        Targets a NONEXISTENT account, not ``admin@example.com``. Both take the identical
        401 path, but a failure against the real admin account increments its progressive
        per-account lockout counter (threshold 5 with prod values), which would break every
        later e2e test that logs in as admin — the hazard
        ``backend/tests/CLAUDE.md`` calls out by name, and which this test violated.

        The wrong-password-for-a-real-account branch is covered where it can be, under
        savepoint rollback:
        ``tests/api/endpoints/test_auth_comprehensive.py::test_login_wrong_password``.
        """
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        _login_local(page, NO_SUCH_LOCAL_ACCOUNT, "wrong_password")

        # Should stay on login page with an error
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        assert "/login" in page.url or page.locator("#email").is_visible()

    def test_local_login_empty_fields(self, page: Page, base_url: str):
        """Submitting with empty fields shows validation."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        # Click submit without filling fields
        page.click("button[type=submit]")

        # Should remain on login page
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        expect(page.locator("#email")).to_be_visible()

    def test_logout_returns_to_login(self, page: Page, base_url: str):
        """Logging out returns to the login page."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        _login_local(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        _wait_for_gallery(page)

        _logout(page)

        # Should be back on login page
        page.wait_for_selector("#email", timeout=10000)
        assert page.locator("#email").is_visible()


# ===== LDAP Login Tests =====


class TestLDAPLogin:
    """Test LDAP login flow through the browser.

    Requires LDAP container running and LDAP enabled in backend config.
    """

    _ldap_e2e = os.environ.get("RUN_AUTH_E2E", "").lower() == "true"

    def _is_ldap_enabled(self, page: Page) -> bool:
        """Check if LDAP is enabled via API."""
        methods = cast(
            dict,
            page.evaluate(
                """
            async () => {
                const r = await fetch('/api/auth/methods');
                return await r.json();
            }
        """
            ),
        )
        return cast(bool, methods.get("ldap_enabled", False))

    def test_ldap_login_success(self, page: Page, base_url: str):
        """LDAP user can log in with username/password."""
        if not self._ldap_e2e:
            pytest.skip("LDAP login tests require RUN_AUTH_E2E=true and LLDAP container")
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        if not self._is_ldap_enabled(page):
            pytest.skip("LDAP is not enabled")

        # LDAP login uses the same form — the email field accepts usernames
        _login_local(page, LDAP_USERNAME, LDAP_PASSWORD)
        _wait_for_gallery(page)

        assert "/login" not in page.url

    def test_ldap_login_shows_gallery(self, page: Page, base_url: str):
        """After LDAP login, gallery loads with thumbnails."""
        if not self._ldap_e2e:
            pytest.skip("LDAP login tests require RUN_AUTH_E2E=true and LLDAP container")
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        if not self._is_ldap_enabled(page):
            pytest.skip("LDAP is not enabled")

        _login_local(page, LDAP_USERNAME, LDAP_PASSWORD)
        _wait_for_gallery(page)

        # Wait for content
        page.wait_for_load_state("networkidle", timeout=10000)

        # The networkidle above already settled the page, so the old fixed 3 s wait was
        # redundant (issue #431).
        # Page should load without errors (may have zero images if no files uploaded)
        console_errors = []
        page.on(
            "console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None
        )

        # No JS errors should occur
        body_text = page.text_content("body")
        assert body_text is not None

    def test_ldap_invalid_password(self, page: Page, base_url: str):
        """Bad LDAP credentials are rejected.

        Targets an LDAP uid that exists nowhere. Aiming this at ``ldap-admin`` — which
        ``test_ldap_login_success`` above provisions as a real local account on its first
        successful login — put failed attempts into that account's lockout bucket
        (``canonical_identifier`` resolves an ``ldap_uid`` to the account's email), so a
        run of this class could lock out the account the class itself logs in with.

        The real-user-wrong-password bind rejection is covered by
        ``test_ldap_oidc.py::test_ldap_wrong_password_rejected``, which owns the LLDAP
        fixture and can use an account reserved for exactly that.
        """
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        if not self._is_ldap_enabled(page):
            pytest.skip("LDAP is not enabled")

        _login_local(page, NO_SUCH_LDAP_ACCOUNT, "wrong_ldap_password")

        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        assert "/login" in page.url or page.locator("#email").is_visible()


# ===== OIDC Login Tests =====


class TestOIDCLogin:
    """Test Keycloak/OIDC login button and redirect flow.

    Requires Keycloak container running and Keycloak enabled in backend config.
    """

    def _is_oidc_enabled(self, page: Page) -> bool:
        """Check if Keycloak is enabled via API."""
        methods = cast(
            dict,
            page.evaluate(
                """
            async () => {
                const r = await fetch('/api/auth/methods');
                return await r.json();
            }
        """
            ),
        )
        return cast(bool, methods.get("oidc_enabled", False))

    def test_oidc_button_click_redirects(self, page: Page, base_url: str):
        """Clicking Keycloak button initiates OIDC redirect."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        if not self._is_oidc_enabled(page):
            pytest.skip("Keycloak is not enabled")

        oidc_button = page.locator("button.oidc-button")
        expect(oidc_button).to_be_visible()

        oidc_button.click()

        # Should redirect to Keycloak login page (different domain/path)
        page.wait_for_url("**/realms/**", timeout=15000)
        assert "realms" in page.url

    def test_oidc_redirect_shows_login_form(self, page: Page, base_url: str):
        """Keycloak redirect page shows a login form."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        if not self._is_oidc_enabled(page):
            pytest.skip("Keycloak is not enabled")

        page.locator("button.oidc-button").click()
        page.wait_for_url("**/realms/**", timeout=15000)

        # Keycloak login page should have username/password fields
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        username_field = page.locator("#username, input[name=username]")
        password_field = page.locator("#password, input[name=password]")

        assert username_field.count() > 0, "Keycloak login page should have username field"
        assert password_field.count() > 0, "Keycloak login page should have password field"


# ===== PKI Certificate Button Tests =====


class TestPKIButton:
    """Test PKI certificate button appearance and behavior.

    Tests that run without the PKI overlay — just verifies button visibility
    and API behavior. Full PKI E2E tests with real certs are in test_pki.py.
    """

    def _is_pki_enabled(self, page: Page) -> bool:
        """Check if PKI is enabled via API."""
        methods = cast(
            dict,
            page.evaluate(
                """
            async () => {
                const r = await fetch('/api/auth/methods');
                return await r.json();
            }
        """
            ),
        )
        return cast(bool, methods.get("pki_enabled", False))

    def test_pki_button_visible(self, page: Page, base_url: str):
        """PKI button is visible when enabled."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        if not self._is_pki_enabled(page):
            pytest.skip("PKI is not enabled")

        pki_button = page.locator("button.pki-button")
        expect(pki_button).to_be_visible()
        text = pki_button.text_content()
        assert "Certificate" in text

    def test_pki_button_click_attempts_auth(self, page: Page, base_url: str):
        """Clicking PKI button sends auth request to backend."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        if not self._is_pki_enabled(page):
            pytest.skip("PKI is not enabled")

        pki_button = page.locator("button.pki-button")

        # Listen for the PKI API call
        with page.expect_response("**/api/auth/pki/authenticate") as response_info:
            pki_button.click()

        # Without a real client cert, this will fail with 401
        # But the API call should happen
        response = response_info.value
        assert response.status in (200, 401), f"Expected 200 or 401, got {response.status}"

    def test_pki_api_responds(self, page: Page, base_url: str):
        """PKI API endpoint is reachable from the browser."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        if not self._is_pki_enabled(page):
            pytest.skip("PKI is not enabled")

        # Make API call directly
        result = page.evaluate(
            """
            async () => {
                try {
                    const response = await fetch('/api/auth/pki/authenticate', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    return { status: response.status, ok: response.ok };
                } catch (e) {
                    return { error: e.message };
                }
            }
        """
        )

        # Without a cert, should get 401 (not 500 or network error)
        assert "error" not in result, f"API call failed: {result.get('error')}"
        assert result["status"] in (401, 400), f"Expected 401 or 400, got {result['status']}"


# ===== Post-Login Gallery/Thumbnail Tests =====


class TestPostLoginGallery:
    """Test that the gallery and thumbnails load properly after login."""

    def test_gallery_no_console_errors(self, page: Page, base_url: str):
        """After login, no JavaScript console errors on gallery page."""
        console_errors = []
        page.on(
            "console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None
        )

        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        _login_local(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        _wait_for_gallery(page)

        # Settle deterministically instead of guessing 5 s: console errors are asserted by
        # their ABSENCE, so no locator can wait for them (issue #431).
        page.wait_for_load_state("networkidle")

        # Filter out common benign errors
        real_errors = [e for e in console_errors if "favicon" not in e.lower()]

        # No real JS errors should have occurred
        assert len(real_errors) == 0, f"Console errors found: {real_errors}"

    def test_thumbnails_load_200(self, page: Page, base_url: str):
        """Thumbnail images return 200 status codes (no broken images)."""
        failed_requests = []

        def on_response(response):
            url = response.url
            # Check image/media responses
            if any(
                ext in url for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", "/s3/", "/minio/"]
            ):
                if response.status >= 400:
                    failed_requests.append(f"{response.status} {url}")

        page.on("response", on_response)

        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        _login_local(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        _wait_for_gallery(page)

        # "Images finished loading" is exactly what networkidle means — a deterministic
        # replacement for the old guessed 5 s (issue #431).
        page.wait_for_load_state("networkidle")

        # No image requests should have 4xx/5xx errors
        assert len(failed_requests) == 0, f"Failed image requests: {failed_requests}"

    def test_navigation_works_after_login(self, page: Page, base_url: str):
        """Can navigate to file detail page after login."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("#email", timeout=10000)

        _login_local(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        _wait_for_gallery(page)

        # Check if there are any file cards to click
        file_cards = page.locator(".file-card, .gallery-item, a[href*='/files/']")
        if file_cards.count() > 0:
            file_cards.first.click()
            # Deterministic settle rather than a guessed duration (issue #431).
            page.wait_for_load_state("networkidle")
            # Should navigate to a file detail page
            assert "/files/" in page.url, f"Expected /files/ in URL, got {page.url}"
