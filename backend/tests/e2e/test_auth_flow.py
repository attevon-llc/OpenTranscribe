"""
E2E Tests for Authentication Flows

Tests login, logout, and registration through the actual frontend UI.
These tests verify that frontend and backend work together correctly.

Run with:
    pytest backend/tests/e2e/test_auth_flow.py -v

Run with visible browser:
    pytest backend/tests/e2e/test_auth_flow.py -v --headed
"""

import uuid
from collections.abc import Iterator

import pytest
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD
from playwright.sync_api import Page
from playwright.sync_api import expect

#: Every address the tests in this file register begins with one of these; the cleanup
#: helper deletes ONLY matching addresses. Positively scoped for the same reason as the
#: identical list in ``test_registration.py``: the duplicate-email test deliberately
#: submits the shared admin address, which must survive its own teardown.
_TEST_CREATED_PREFIXES = ("reg-e2e-", "testuser_")


def _delete_user_by_email(api_helper, email: str) -> None:
    """Best-effort cleanup of a user this test registered (dev data hygiene)."""
    if not email.strip().lower().startswith(_TEST_CREATED_PREFIXES):
        return
    try:
        api_helper.login(TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD)
        users = api_helper.get("/api/admin/users")
        items = users if isinstance(users, list) else users.get("items", [])
        for user in items:
            if user.get("email") == email:
                api_helper.delete(f"/api/admin/users/{user['uuid']}")
                return
    except Exception:
        pass  # cleanup is best-effort; scripts/cleanup-test-users.py catches strays


@pytest.fixture
def registration_email(api_helper) -> Iterator[str]:
    """A run-unique address for a registration attempt, removed on the way out.

    Mirrors ``test_registration.py``'s fixture of the same name, for the same reason: a
    fixed address submitted to the live register endpoint is how a real account ended up
    in the dev database. Teardown always runs and is a no-op when nothing was created,
    so it is correct even on the tests here that expect the submission to be rejected.
    """
    email = f"reg-e2e-{uuid.uuid4().hex[:8]}@example.com"
    yield email
    _delete_user_by_email(api_helper, email)


class TestLoginFlow:
    """Test login functionality through the UI."""

    def test_login_page_loads(self, login_page: Page):
        """Verify login page loads with all required elements."""
        # Check page title contains OpenTranscribe
        expect(login_page).to_have_title("OpenTranscribe")

        # Check form elements exist
        expect(login_page.locator("#email")).to_be_visible()
        expect(login_page.locator("#password")).to_be_visible()
        expect(login_page.locator("button[type=submit]")).to_be_visible()

    def test_login_success(self, login_page: Page, base_url: str):
        """Test successful login with valid credentials."""
        login_page.fill("#email", "admin@example.com")
        login_page.fill("#password", "password")
        login_page.click("button[type=submit]")

        # Should redirect to gallery/dashboard (a bare base_url/** pattern
        # matches /login itself — wait for the navigation off the login page)
        login_page.wait_for_url(lambda url: "/login" not in url, timeout=15000)

        # Verify we're logged in - check for user menu or gallery content
        expect(
            login_page.locator("text=Gallery")
            .or_(login_page.locator("[data-testid=user-menu]"))
            .first
        ).to_be_visible(timeout=10000)

    def test_login_failure_invalid_password(self, login_page: Page):
        """Test login fails with invalid password.

        Nonexistent account (same 401 path) — failing against the real admin
        account trips the progressive per-account lockout (threshold 5) and
        poisons every later test in the suite.
        """
        login_page.fill("#email", "nosuchuser-e2e@example.com")
        login_page.fill("#password", "wrongpassword")
        login_page.click("button[type=submit]")

        # `[role=alert]` is the app's error surface (one node, "Incorrect username
        # or password"), and `expect` AUTO-WAITS for it.
        #
        # The previous chain of synchronous `.is_visible()` calls was a SNAPSHOT
        # taken the instant `networkidle` settled. When the alert had not painted
        # yet the chain fell through to `text=Invalid` / `text=incorrect` — bare
        # text selectors that can match several nodes, and a multi-match locator
        # makes Playwright's strict mode RAISE rather than return False. So the
        # test ERRORED under parallel load instead of failing, non-deterministically.
        # Its final `or login_page.url.endswith("/login")` could not fail either:
        # a rejected login stays on /login by definition.
        expect(login_page.get_by_role("alert")).to_contain_text(
            "Incorrect username or password", timeout=15000
        )
        assert login_page.url.endswith("/login"), "a rejected login must not navigate away"

    def test_login_failure_nonexistent_user(self, login_page: Page):
        """Test login fails for non-existent user."""
        login_page.fill("#email", "nonexistent@example.com")
        login_page.fill("#password", "anypassword")
        login_page.click("button[type=submit]")

        # Deterministic settle rather than a guessed duration (issue #431).
        login_page.wait_for_load_state("networkidle")
        # Should stay on login page
        assert "/login" in login_page.url or login_page.locator("#email").is_visible()

    def test_login_empty_fields_validation(self, login_page: Page):
        """Test form validation for empty fields."""
        # Click submit without filling fields
        login_page.click("button[type=submit]")

        # Should show validation or stay on page
        # Deterministic settle rather than a guessed duration (issue #431).
        login_page.wait_for_load_state("networkidle")
        expect(login_page.locator("#email")).to_be_visible()

    def test_password_visibility_toggle(self, login_page: Page):
        """Test password visibility toggle if available."""
        password_input = login_page.locator("#password")
        toggle_button = login_page.locator("[data-testid=toggle-password], button:near(#password)")

        if toggle_button.count() > 0:
            # Initial state should be password (hidden)
            expect(password_input).to_have_attribute("type", "password")

            # Click toggle
            toggle_button.first.click()

            # Should now be text (visible)
            expect(password_input).to_have_attribute("type", "text")


class TestLogoutFlow:
    """Test logout functionality."""

    def test_logout_success(self, authenticated_page: Page, base_url: str):
        """Test successful logout."""
        # Click user menu button
        user_menu = authenticated_page.locator(".user-button").first
        expect(user_menu).to_be_visible(timeout=5000)
        user_menu.click()
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        # Click logout in dropdown
        logout_btn = authenticated_page.locator(
            "button:has-text('Logout'), button:has-text('Sign Out'), "
            "a:has-text('Logout'), .logout-btn, [data-action=logout]"
        ).first

        logout_btn.click()

        # Should redirect to login
        authenticated_page.wait_for_url("**/login**", timeout=10000)
        expect(authenticated_page.locator("#email")).to_be_visible()


class TestRegistrationFlow:
    """Test user registration functionality."""

    def test_registration_link_exists(self, login_page: Page):
        """Verify registration link exists on login page."""
        register_link = login_page.locator("a[href*=register]")
        expect(register_link.first).to_be_visible()

    def test_registration_page_loads(self, login_page: Page):
        """Test registration page loads correctly."""
        login_page.click("a[href*=register]")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        # Check for all registration form fields
        expect(login_page.locator("#username")).to_be_visible()
        expect(login_page.locator("#email")).to_be_visible()
        expect(login_page.locator("#password")).to_be_visible()
        expect(login_page.locator("#confirmPassword")).to_be_visible()
        expect(login_page.locator("button:has-text('Create Account')")).to_be_visible()

    def test_registration_success(self, page: Page, base_url: str, api_helper):
        """Test successful user registration with unique credentials."""
        unique_id = str(uuid.uuid4())[:8]
        username = f"testuser_{unique_id}"
        email = f"testuser_{unique_id}@example.com"
        password = "TestPassword123!"

        try:
            # Navigate to registration page
            page.goto(f"{base_url}/login")
            page.wait_for_selector("a[href*=register]")
            page.click("a[href*=register]")
            # The `page.fill("#username", ...)` below auto-waits for the register form to
            # mount, so the old fixed 1 s wait bought nothing (issue #431).

            # Fill registration form
            page.fill("#username", username)
            page.fill("#email", email)
            page.fill("#password", password)
            page.fill("#confirmPassword", password)

            # Submit
            page.click("button:has-text('Create Account')")

            # Successful registration auto-logs-in and lands on the gallery
            # (frontend/src/routes/register/+page.svelte: register -> login -> goto "/").
            # Wait for the navigation itself — the navbar button can render
            # a beat before goto("/") completes.
            page.wait_for_url(lambda url: "register" not in url.lower(), timeout=15000)
            page.wait_for_selector(".user-button", timeout=15000)
        finally:
            _delete_user_by_email(api_helper, email)

    def test_registration_password_mismatch(
        self, page: Page, base_url: str, registration_email: str
    ):
        """Test registration fails when passwords don't match."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("a[href*=register]")
        page.click("a[href*=register]")
        # The `page.fill("#username", ...)` below auto-waits for the register form to
        # mount, so the old fixed 1 s wait bought nothing (issue #431).

        # Fill form with mismatched passwords
        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "Password123!")
        page.fill("#confirmPassword", "DifferentPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        # Should show error or stay on registration page
        still_on_register = "register" in page.url or page.locator("#confirmPassword").is_visible()
        assert still_on_register, "Should not proceed with mismatched passwords"

    def test_registration_weak_password(self, page: Page, base_url: str, registration_email: str):
        """Test registration validates password strength."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("a[href*=register]")
        page.click("a[href*=register]")
        # The `page.fill("#username", ...)` below auto-waits for the register form to
        # mount, so the old fixed 1 s wait bought nothing (issue #431).

        # Fill form with weak password
        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "weak")
        page.fill("#confirmPassword", "weak")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        # Should show error or validation message
        still_on_register = "register" in page.url or page.locator("#password").is_visible()
        assert still_on_register, "Should validate password strength"

    def test_registration_duplicate_email_fails(self, page: Page, base_url: str):
        """Test registration fails for existing email."""
        page.goto(f"{base_url}/login")
        page.wait_for_selector("a[href*=register]")
        page.click("a[href*=register]")
        # The `page.fill("#username", ...)` below auto-waits for the register form to
        # mount, so the old fixed 1 s wait bought nothing (issue #431).

        # Try to register with existing admin email
        page.fill("#username", "newadmin")
        page.fill("#email", "admin@example.com")
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")

        # Same fix as test_login_failure_invalid_password, and this one was the
        # observed offender: `text=already` matches TWO nodes on this page — the
        # alert ("Email already registered") AND the "Already have an account?
        # Login" link — so every time the alert had not rendered by the time the
        # snapshot chain reached that term, strict mode raised and the test ERRORED.
        # That is the entirety of its parallel-load flakiness; standalone it always
        # short-circuited on `[role=alert]` first and looked stable.
        expect(page.get_by_role("alert")).to_contain_text("already registered", timeout=15000)
        assert "register" in page.url, "a duplicate email must not create an account"

    # NOTE: there is deliberately no "duplicate username" test — the register
    # form's "username" maps to User.full_name, which is NOT unique (only
    # email is: app/models/user.py). The duplicate-email test above covers
    # the real uniqueness constraint.


class TestAuthenticationPersistence:
    """Test that authentication state persists correctly."""

    def test_session_persists_on_refresh(self, authenticated_page: Page, base_url: str):
        """Test that user stays logged in after page refresh."""
        # We're already logged in via authenticated_page fixture
        # Refresh the page
        authenticated_page.reload()
        authenticated_page.wait_for_load_state("networkidle")

        # Should still be on the same page (not redirected to login)
        assert "/login" not in authenticated_page.url, "Should stay logged in after refresh"

    def test_protected_route_redirects_when_not_logged_in(self, page: Page, base_url: str):
        """Test that protected routes redirect to login."""
        # Try to access gallery directly without logging in
        page.goto(f"{base_url}/gallery")
        page.wait_for_load_state("networkidle")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        # Should be redirected to login
        assert "/login" in page.url or page.locator("#email").is_visible(), (
            "Should redirect to login when not authenticated"
        )


class TestAlternativeAuthMethods:
    """Test alternative authentication methods (Keycloak, PKI)."""

    def test_keycloak_button_visible(self, login_page: Page):
        """Check if Keycloak login option is available."""
        keycloak_btn = login_page.locator(
            "button:has-text('Keycloak'), button:has-text('SSO'), [data-testid=keycloak-login]"
        )

        # This is optional - just check if it exists
        if keycloak_btn.count() > 0:
            expect(keycloak_btn.first).to_be_visible()

    def test_certificate_login_visible(self, login_page: Page):
        """Check if certificate/PKI login option is available."""
        pki_btn = login_page.locator(
            "button:has-text('Certificate'), button:has-text('PKI'), "
            "button:has-text('CAC'), [data-testid=pki-login]"
        )

        # This is optional - just check if it exists
        if pki_btn.count() > 0:
            expect(pki_btn.first).to_be_visible()


class TestConsoleErrors:
    """Test that pages load without JavaScript errors."""

    def test_login_page_no_console_errors(self, login_page: Page, console_errors: list):
        """Login page should load without console errors."""
        login_page.wait_for_load_state("networkidle")
        # Deterministic settle rather than a guessed duration (issue #431).
        login_page.wait_for_load_state("networkidle")
        # Filter out non-critical errors
        critical_errors = [
            e for e in console_errors if "favicon" not in e.lower() and "404" not in e
        ]

        assert len(critical_errors) == 0, f"Page has console errors: {critical_errors}"

    def test_authenticated_page_no_console_errors(
        self, authenticated_page: Page, console_errors: list
    ):
        """Authenticated pages should load without console errors."""
        authenticated_page.wait_for_load_state("networkidle")
        # Deterministic settle rather than a guessed duration (issue #431).
        authenticated_page.wait_for_load_state("networkidle")
        critical_errors = [
            e for e in console_errors if "favicon" not in e.lower() and "404" not in e
        ]

        assert len(critical_errors) == 0, f"Page has console errors: {critical_errors}"
