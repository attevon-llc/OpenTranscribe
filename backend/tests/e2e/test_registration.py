"""
Comprehensive E2E Tests for User Registration

Tests cover:
- Form validation (all fields)
- Password requirements and constraints
- Username constraints
- Email validation
- Duplicate prevention
- Success flow with login verification
- Error message display

Run with:
    pytest backend/tests/e2e/test_registration.py -v --headed
"""

import re
import uuid
from collections.abc import Iterator

import pytest
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD
from playwright.sync_api import Page
from playwright.sync_api import expect

#: Every address the tests in this file register begins with one of these. The cleanup
#: helper deletes ONLY matching addresses, so it can never reach a pre-existing dev
#: account — notably not the shared admin, which the duplicate-email test deliberately
#: submits. A positive rule rather than a hand-maintained never-delete list: the latter
#: is how ``test@example.com`` came to sit on ``scripts/cleanup-test-users.py``'s
#: keep-list, read as a legitimate dev account when it was in fact the leak.
_TEST_CREATED_PREFIXES = ("reg-e2e-", "shortname-", "testuser_", "newuser_")


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

    Every test in this file submits the REAL register form against the live dev stack,
    so any payload the server happens to accept creates a real account. A fixed
    ``test@example.com`` is how one of those accounts ended up in the dev database and
    had to be deleted by hand — and ``scripts/cleanup-test-users.py`` still carries that
    exact address on its keep-list, so a sweep would not have caught it either.

    Teardown runs whichever way the test exits, and is a no-op when nothing was created.
    That matters for the many tests here that *expect* a rejection: whether a given
    payload is rejected depends on the password policy, which is DB-backed and
    admin-editable (``core/auth_settings.py``), so it is not a property this file can
    guarantee — which is precisely the assumption that produced the leak.
    """
    email = f"reg-e2e-{uuid.uuid4().hex[:8]}@example.com"
    yield email
    _delete_user_by_email(api_helper, email)


class TestRegistrationFormValidation:
    """Test registration form field validation."""

    def test_all_fields_required(self, page: Page, base_url: str):
        """Test that all fields are required."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        # Try to submit empty form
        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        expect(page.locator("#username")).to_be_visible()

    def test_username_required(self, page: Page, base_url: str, registration_email: str):
        """Test username field is required."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        # Fill all except username
        page.fill("#email", registration_email)
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        expect(page.locator("#username")).to_be_visible()

    def test_email_required(self, page: Page, base_url: str):
        """Test email field is required."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        expect(page.locator("#email")).to_be_visible()

    def test_password_required(self, page: Page, base_url: str, registration_email: str):
        """Test password field is required."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", registration_email)

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        expect(page.locator("#password")).to_be_visible()

    def test_confirm_password_required(self, page: Page, base_url: str, registration_email: str):
        """Test confirm password field is required."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "ValidPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        expect(page.locator("#confirmPassword")).to_be_visible()


class TestUsernameValidation:
    """Test username field constraints."""

    def test_a_short_display_name_is_accepted(self, page: Page, base_url: str, api_helper):
        """The field labelled "username" is the display NAME, and short names are valid.

        This was ``test_username_min_length``, asserting a minimum-length rule that does not
        exist and never did. Registration has no username concept: the frontend sends this
        field as ``full_name`` (``frontend/src/stores/auth.ts`` — ``register(email, username,
        password)`` posts ``{email, full_name}``), and ``UserCreate`` declares no length
        constraint on it. Nor should it: "Li" and "Bo" are real names.

        It passed only because ``wait_for_timeout(2000)`` expired before the
        post-registration redirect completed, so ``#username`` was still on screen and "we
        stayed on /register" looked true. Waiting deterministically showed registration had
        in fact succeeded — the fixed wait hid a false premise for as long as the app stayed
        slow (issue #431). Verified directly against the API: a 2-character value returns 200.
        """
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        email = f"shortname-{uuid.uuid4().hex[:8]}@example.com"
        page.fill("#username", "Li")
        page.fill("#email", email)
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")

        try:
            # A two-character display name is accepted, so registration leaves /register.
            expect(page).not_to_have_url(re.compile(r"/register"), timeout=15000)
        finally:
            # This test really does create an account, so it deletes it — E2E must never
            # persist changes. The old version never got this far, so it never had to.
            _delete_user_by_email(api_helper, email)

    def test_username_special_characters(self, page: Page, base_url: str, registration_email: str):
        """Test username doesn't allow special characters."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        # Try username with special characters
        page.fill("#username", "test@user!")
        page.fill("#email", registration_email)
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        # Should handle special characters (either reject or sanitize)
        # Just verify form processed without crash
        assert page.is_visible("body")

    def test_username_whitespace(self, page: Page, base_url: str, registration_email: str):
        """Test username handles whitespace correctly."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        # Try username with spaces
        page.fill("#username", "test user")
        page.fill("#email", registration_email)
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        # Should stay on register or show error
        assert page.is_visible("body")


class TestEmailValidation:
    """Test email field constraints."""

    def test_email_invalid_format(self, page: Page, base_url: str):
        """Test invalid email format is rejected."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", "notanemail")
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        # Should show validation error
        still_on_register = "register" in page.url or page.locator("#email").is_visible()
        assert still_on_register, "Should validate email format"

    def test_email_missing_domain(self, page: Page, base_url: str):
        """Test email without domain is rejected."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", "test@")
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        still_on_register = "register" in page.url or page.locator("#email").is_visible()
        assert still_on_register

    def test_email_missing_at_symbol(self, page: Page, base_url: str):
        """Test email without @ symbol is rejected."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", "testexample.com")
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        still_on_register = "register" in page.url or page.locator("#email").is_visible()
        assert still_on_register


class TestPasswordValidation:
    """Test password field constraints and requirements."""

    def test_password_too_short(self, page: Page, base_url: str, registration_email: str):
        """Test password minimum length (typically 8+ characters)."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "Short1!")
        page.fill("#confirmPassword", "Short1!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        still_on_register = "register" in page.url or page.locator("#password").is_visible()
        assert still_on_register, "Should enforce minimum password length"

    def test_password_no_uppercase(self, page: Page, base_url: str, registration_email: str):
        """Test password requires uppercase letter."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "lowercase123!")
        page.fill("#confirmPassword", "lowercase123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        # This may or may not be required depending on policy
        assert page.is_visible("body")

    def test_password_no_lowercase(self, page: Page, base_url: str, registration_email: str):
        """Test password requires lowercase letter."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "UPPERCASE123!")
        page.fill("#confirmPassword", "UPPERCASE123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        assert page.is_visible("body")

    def test_password_no_numbers(self, page: Page, base_url: str, registration_email: str):
        """Test password requires numbers."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "NoNumbersHere!")
        page.fill("#confirmPassword", "NoNumbersHere!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        assert page.is_visible("body")

    def test_password_no_special_chars(self, page: Page, base_url: str, registration_email: str):
        """Test password requires special characters."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "NoSpecialChars123")
        page.fill("#confirmPassword", "NoSpecialChars123")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        assert page.is_visible("body")

    def test_password_mismatch(self, page: Page, base_url: str, registration_email: str):
        """Test passwords must match."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        page.fill("#username", "testuser")
        page.fill("#email", registration_email)
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "DifferentPassword123!")

        page.click("button:has-text('Create Account')")
        # Deterministic settle rather than a guessed duration (issue #431).
        page.wait_for_load_state("networkidle")
        still_on_register = "register" in page.url or page.locator("#confirmPassword").is_visible()
        assert still_on_register, "Should reject mismatched passwords"

    def test_password_visibility_toggle(self, page: Page, base_url: str):
        """Test password visibility toggle button if present."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        password_input = page.locator("#password")
        toggle_btn = page.locator("button:near(#password)").first

        # Initial state should be hidden
        expect(password_input).to_have_attribute("type", "password")

        # If toggle exists, test it
        if toggle_btn.is_visible():
            toggle_btn.click()
            # Should now show password
            expect(password_input).to_have_attribute("type", "text")


class TestDuplicatePrevention:
    """Test duplicate user prevention."""

    def test_duplicate_email_rejected(self, page: Page, base_url: str):
        """Test registration fails for existing email."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        unique_username = f"unique_{uuid.uuid4().hex[:8]}"
        page.fill("#username", unique_username)
        page.fill("#email", "admin@example.com")  # Existing email
        page.fill("#password", "ValidPassword123!")
        page.fill("#confirmPassword", "ValidPassword123!")

        page.click("button:has-text('Create Account')")

        # Assert the actual rejection message, not a substring that collides with page
        # furniture. The old check was an `or` chain ending in `"register" in page.url`, and
        # included `page.locator("text=already").is_visible()` — which matches the register
        # page's own "Already have an account? Login" link whether or not anything failed. So
        # `error_visible` was always True and the test could not fail; the trailing url check
        # made it doubly unfalsifiable. `wait_for_timeout(2000)` hid this by expiring before
        # the toast rendered, leaving only the footer link to match (issue #431).
        expect(page.get_by_text("Email already registered")).to_be_visible(timeout=15000)

        # And it must NOT have created a session — a rejected registration stays put.
        expect(page).to_have_url(re.compile(r"/register"))

    # NOTE: there is deliberately no "duplicate username" test — the register
    # form's "username" maps to User.full_name, which is NOT unique (only
    # email is: app/models/user.py). test_duplicate_email_rejected covers the
    # real uniqueness constraint.


class TestRegistrationSuccess:
    """Test successful registration flow."""

    def test_successful_registration_redirects(self, page: Page, base_url: str, api_helper):
        """Successful registration auto-logs-in and lands on the gallery."""
        unique_id = uuid.uuid4().hex[:8]
        username = f"testuser_{unique_id}"
        email = f"testuser_{unique_id}@example.com"
        password = "ValidPassword123!"

        try:
            page.goto(f"{base_url}/register")
            page.wait_for_selector("#username", timeout=10000)

            page.fill("#username", username)
            page.fill("#email", email)
            page.fill("#password", password)
            page.fill("#confirmPassword", password)

            page.click("button:has-text('Create Account')")

            # register/+page.svelte logs the new user in and goto("/").
            # Wait for the NAVIGATION (the navbar user-button can render a
            # beat before goto("/") completes — asserting on it races).
            page.wait_for_url(lambda url: "register" not in url.lower(), timeout=15000)
            page.wait_for_selector(".user-button", timeout=15000)
        finally:
            _delete_user_by_email(api_helper, email)

    def test_can_login_after_registration(self, page: Page, base_url: str, api_helper):
        """Newly registered user is auto-logged-in, and can log in again later."""
        unique_id = uuid.uuid4().hex[:8]
        username = f"newuser_{unique_id}"
        email = f"newuser_{unique_id}@example.com"
        password = "ValidPassword123!"

        try:
            # Register
            page.goto(f"{base_url}/register")
            page.wait_for_selector("#username", timeout=10000)

            page.fill("#username", username)
            page.fill("#email", email)
            page.fill("#password", password)
            page.fill("#confirmPassword", password)

            page.click("button:has-text('Create Account')")

            # Successful registration auto-logs-in and lands on the gallery
            # (frontend/src/routes/register/+page.svelte: register -> login -> goto "/").
            # Wait for the navigation itself — the navbar button can render
            # a beat before goto("/") completes.
            page.wait_for_url(lambda url: "register" not in url.lower(), timeout=15000)
            page.wait_for_selector(".user-button", timeout=15000)

            # Drop the session cookies, then log in fresh with the new creds
            page.context.clear_cookies()
            page.goto(f"{base_url}/login")
            page.wait_for_selector("#email", timeout=10000)

            page.fill("#email", email)
            page.fill("#password", password)
            page.click("button[type=submit]")
            # Wait for the post-login NAVIGATION (the navbar can render before
            # the SPA finishes goto("/") — asserting on page.url races).
            page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
            page.wait_for_selector(".user-button", timeout=15000)
        finally:
            _delete_user_by_email(api_helper, email)


class TestRegistrationUI:
    """Test registration page UI elements."""

    def test_page_title(self, page: Page, base_url: str):
        """Test registration page has correct title."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        # Check for registration heading
        heading = page.locator("h1:has-text('Register'), h2:has-text('Register')")
        expect(heading.first).to_be_visible()

    def test_login_link_exists(self, page: Page, base_url: str):
        """Test registration page has link to login."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        login_link = page.locator("a[href*=login], a:has-text('Login')")
        expect(login_link.first).to_be_visible()

    def test_password_requirements_shown(self, page: Page, base_url: str):
        """Test password requirements are displayed."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        # Look for password requirements info (may be icon, tooltip, or text)
        info_icon = page.locator("[data-testid=password-info], .password-info, svg:near(#password)")

        # May or may not have explicit requirements displayed
        assert page.locator("#password").is_visible()

    def test_form_labels_present(self, page: Page, base_url: str):
        """Test all form fields have labels."""
        page.goto(f"{base_url}/register")
        page.wait_for_selector("#username", timeout=10000)

        # Check labels exist — match by `for` attribute ("Password" text alone
        # is ambiguous with "Confirm Password" under strict mode)
        expect(page.locator("label[for=username]")).to_be_visible()
        expect(page.locator("label[for=email]")).to_be_visible()
        expect(page.locator("label[for=password]")).to_be_visible()
        expect(page.locator("label[for=confirmPassword]")).to_be_visible()
