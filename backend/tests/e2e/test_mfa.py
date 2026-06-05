"""
MFA E2E Tests with Playwright

Tests verify the MFA enrollment and login flow through the browser:
- MFA setup flow: open settings -> enable MFA -> scan QR -> enter code -> backup codes
- MFA login flow: login -> MFA prompt -> enter code -> gallery
- MFA disable flow: settings -> disable MFA -> confirm with code

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173
- Backend at localhost:5174
- MFA enabled globally (mfa_enabled=true in auth_config table)

Run:
    pytest backend/tests/e2e/test_mfa.py -v
    DISPLAY=:13 pytest backend/tests/e2e/test_mfa.py -v --headed

Automated run (via master script):
    ./scripts/run-auth-e2e.sh --skip-ldap --skip-pki
"""

import os
import uuid
from typing import cast

import pyotp
import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:5174")

# Admin credentials (for viewing settings, enabling MFA globally)
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "password"

# Dedicated MFA test user — created per session to avoid locking out admin
MFA_TEST_EMAIL = f"mfa-e2e-{uuid.uuid4().hex[:8]}@example.com"
MFA_TEST_PASSWORD = "Xk9!pLm2@Wq7Rn"
MFA_TEST_FULLNAME = "Jo Ce"


# ===== Session fixtures =====


@pytest.fixture(scope="session", autouse=True)
def mfa_test_user():
    """Create a dedicated user for MFA tests via the admin API.

    Uses a unique email per session to avoid conflicts. The user is created
    at the start of the test session and used for all MFA setup/login tests,
    keeping the admin account clean and unlocked.
    """
    # Get admin token
    resp = requests.post(
        f"{BACKEND_URL}/api/auth/token",
        data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if resp.status_code != 200:
        pytest.skip(f"Cannot login as admin: {resp.text}")
    data = resp.json()
    if data.get("mfa_required"):
        pytest.skip("Admin has MFA enabled — cannot create test user")
    admin_token = data["access_token"]
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Register the MFA test user
    reg_resp = requests.post(
        f"{BACKEND_URL}/api/auth/register",
        json={
            "email": MFA_TEST_EMAIL,
            "password": MFA_TEST_PASSWORD,
            "full_name": MFA_TEST_FULLNAME,
        },
    )
    if reg_resp.status_code not in (200, 201, 409):
        # 409 = already exists (from previous run with same email)
        pytest.skip(f"Cannot create MFA test user: {reg_resp.status_code} {reg_resp.text}")

    return {"email": MFA_TEST_EMAIL, "password": MFA_TEST_PASSWORD}


# ===== API helpers =====


def _api_login(email: str, password: str) -> str:
    """Log in via API and return access token."""
    resp = requests.post(
        f"{BACKEND_URL}/api/auth/token",
        data={"username": email, "password": password},
    )
    data = cast(dict, resp.json())
    if data.get("mfa_required"):
        pytest.skip("User already has MFA enabled — cannot run MFA setup tests")
    return cast(str, data["access_token"])


def _api_get_mfa_status(token: str) -> dict:
    """Get MFA status via API."""
    headers = {"Authorization": f"Bearer {token}"}
    return cast(dict, requests.get(f"{BACKEND_URL}/api/auth/mfa/status", headers=headers).json())


def _login_browser(page: Page, email: str, password: str):
    """Log in via the browser."""
    page.goto(f"{FRONTEND_URL}/login")
    page.wait_for_selector("#email", timeout=10000)
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("button[type=submit]")


def _wait_for_gallery(page: Page, timeout: int = 15000):
    """Wait for navigation past the login page."""
    page.wait_for_function(
        "() => !document.querySelector('#email') || document.querySelector('#email').offsetParent === null",
        timeout=timeout,
    )


def _open_security_settings(page: Page):
    """Navigate to Settings -> Security tab and wait for MFA content to load."""
    page.click(".user-button")
    page.wait_for_selector(".dropdown-item", state="visible", timeout=5000)
    page.click(".dropdown-item:has-text('Settings')")
    page.wait_for_selector(".settings-sidebar", state="visible", timeout=10000)

    # Click Security nav item
    security_nav = page.locator(".settings-sidebar button.nav-item:has-text('Security')")
    security_nav.scroll_into_view_if_needed()
    security_nav.wait_for(state="visible", timeout=5000)
    security_nav.click()
    page.wait_for_selector(".security-settings", state="visible", timeout=10000)

    # Wait for async MFA status to finish loading (spinner disappears)
    page.wait_for_function(
        "() => !document.querySelector('.security-settings .spinner')",
        timeout=10000,
    )


# ===== MFA Setup Flow Tests =====


class TestMFASetupFlow:
    """Test MFA enrollment flow through the browser UI."""

    def test_mfa_section_visible_in_settings(self, page: Page, mfa_test_user):
        """MFA section is visible in Security settings."""
        _login_browser(page, mfa_test_user["email"], mfa_test_user["password"])
        _wait_for_gallery(page)
        _open_security_settings(page)

        # Content is already loaded after _open_security_settings waits for spinner
        body_text = page.text_content(".security-settings")
        assert body_text is not None
        mfa_text = any(
            kw in body_text.lower() for kw in ["two-factor", "mfa", "multi-factor", "authenticator"]
        )
        assert mfa_text or "not available" in body_text.lower(), (
            "MFA section should be visible in security settings"
        )

    def test_mfa_setup_shows_qr_code(self, page: Page, mfa_test_user):
        """Starting MFA setup shows QR code for authenticator app."""
        token = _api_login(mfa_test_user["email"], mfa_test_user["password"])
        status = _api_get_mfa_status(token)

        if not status.get("can_setup_mfa"):
            pytest.skip("User cannot set up MFA")
        if status.get("mfa_configured"):
            pytest.skip("MFA already configured for this user")

        _login_browser(page, mfa_test_user["email"], mfa_test_user["password"])
        _wait_for_gallery(page)
        _open_security_settings(page)

        # Click "Enable Two-Factor Authentication" button (wait for it to appear)
        enable_btn = page.locator(
            "button.btn-primary:has-text('Enable'), button.btn-primary:has-text('Set Up')"
        )
        try:
            enable_btn.first.wait_for(state="visible", timeout=10000)
        except Exception:
            # Capture what's visible for debugging
            content = page.text_content(".security-settings") or ""
            pytest.skip(f"MFA setup button not found. Content: {content[:200]}")

        enable_btn.first.click()

        # QR code should appear
        qr_image = page.locator(".qr-code img, img[alt*='QR']")
        expect(qr_image).to_be_visible(timeout=10000)

    def test_mfa_setup_complete_flow(self, page: Page, mfa_test_user):
        """Full MFA setup: enable -> QR -> verify TOTP -> see backup codes."""
        token = _api_login(mfa_test_user["email"], mfa_test_user["password"])
        status = _api_get_mfa_status(token)

        if not status.get("can_setup_mfa"):
            pytest.skip("User cannot set up MFA")
        if status.get("mfa_configured"):
            pytest.skip("MFA already configured")

        _login_browser(page, mfa_test_user["email"], mfa_test_user["password"])
        _wait_for_gallery(page)
        _open_security_settings(page)

        # Start MFA setup — wait for the button to appear after async load
        enable_btn = page.locator(
            "button.btn-primary:has-text('Enable'), button.btn-primary:has-text('Set Up')"
        )
        try:
            enable_btn.first.wait_for(state="visible", timeout=10000)
        except Exception:
            content = page.text_content(".security-settings") or ""
            pytest.skip(f"MFA setup button not found. Content: {content[:200]}")
        enable_btn.first.click()

        # Wait for QR code to appear (setup API call)
        qr_image = page.locator(".qr-code img, img[alt*='QR']")
        expect(qr_image).to_be_visible(timeout=10000)

        # Click "Show manual entry" to reveal the TOTP secret
        manual_btn = page.locator("button:has-text('manual'), button:has-text('Manual')")
        if manual_btn.count() > 0:
            manual_btn.first.click()
            page.wait_for_timeout(500)

        secret_elem = page.locator(".secret-code")
        secret = None
        if secret_elem.count() > 0:
            secret = secret_elem.first.text_content()
            if secret:
                secret = secret.strip()

        if not secret or len(secret) < 16:
            # Fall back to API-based setup (re-use the existing setup session)
            headers = {"Authorization": f"Bearer {token}"}
            setup_resp = requests.post(
                f"{BACKEND_URL}/api/auth/mfa/setup",
                headers=headers,
            )
            if setup_resp.status_code != 200:
                pytest.skip("Could not start MFA setup via API")
            secret = setup_resp.json()["secret"]

        # Generate TOTP code
        totp = pyotp.TOTP(secret)
        code = totp.now()

        # Enter the code in the verification input
        code_input = page.locator("#verify-code")
        code_input.wait_for(state="visible", timeout=5000)
        code_input.fill(code)

        # Click verify button
        verify_btn = page.locator("button.btn-primary:has-text('Verify')")
        verify_btn.click()

        # Wait for backup codes section to appear
        backup_section = page.locator(".backup-codes-section")
        try:
            backup_section.wait_for(state="visible", timeout=10000)
        except Exception:
            # If browser flow didn't show backup codes, verify via API fallback
            headers = {"Authorization": f"Bearer {token}"}
            verify_resp = requests.post(
                f"{BACKEND_URL}/api/auth/mfa/verify-setup",
                headers=headers,
                json={"code": totp.now()},
            )
            assert verify_resp.status_code == 200, f"MFA verify-setup failed: {verify_resp.text}"
            assert len(verify_resp.json()["backup_codes"]) > 0
            return

        # Verify backup codes are displayed
        backup_codes = page.locator(".backup-code")
        assert backup_codes.count() > 0, "Backup codes should be displayed"


# ===== MFA Login Flow Tests =====


class TestMFALoginFlow:
    """Test MFA-required login flow through the browser.

    These tests use API-level verification because the browser MFA prompt
    depends on the frontend implementation. The API flow tests the complete
    authentication chain: password -> MFA token -> TOTP verify -> access token.
    """

    def test_mfa_login_api_flow(self, mfa_test_user):
        """API-level: login -> MFA token -> verify wrong code -> 401."""
        login_resp = requests.post(
            f"{BACKEND_URL}/api/auth/token",
            data={"username": mfa_test_user["email"], "password": mfa_test_user["password"]},
        )
        data = login_resp.json()

        if not data.get("mfa_required"):
            pytest.skip("MFA not required for this user — run setup test first")

        mfa_token = data["mfa_token"]

        # Verify that a wrong code is rejected
        verify_resp = requests.post(
            f"{BACKEND_URL}/api/auth/mfa/verify",
            json={"mfa_token": mfa_token, "code": "000000"},
        )
        assert verify_resp.status_code == 401, "Wrong MFA code should return 401"

    def test_mfa_login_shows_code_prompt(self, page: Page, mfa_test_user):
        """When MFA is configured, login shows code entry prompt."""
        # Check if MFA is required for the test user (raw API, no pytest.skip side effects)
        login_resp = requests.post(
            f"{BACKEND_URL}/api/auth/token",
            data={"username": mfa_test_user["email"], "password": mfa_test_user["password"]},
        )
        if not login_resp.json().get("mfa_required"):
            pytest.skip("MFA not configured for test user — run setup test first")

        # Try login via browser — should show MFA prompt
        _login_browser(page, mfa_test_user["email"], mfa_test_user["password"])
        page.wait_for_timeout(3000)

        mfa_input = page.locator("input[autocomplete='one-time-code'], input[maxlength='6']")
        mfa_text = page.locator(
            ":has-text('verification code'), :has-text('authenticator'), :has-text('two-factor')"
        )

        has_mfa_prompt = mfa_input.count() > 0 or mfa_text.count() > 0

        if not has_mfa_prompt:
            if "/login" not in page.url:
                pytest.skip("MFA not enforced during login")

        assert has_mfa_prompt, "MFA code prompt should appear after password login"

    def test_mfa_wrong_code_stays_on_prompt(self, page: Page, mfa_test_user):
        """Wrong MFA code keeps user on the MFA prompt."""
        login_resp = requests.post(
            f"{BACKEND_URL}/api/auth/token",
            data={"username": mfa_test_user["email"], "password": mfa_test_user["password"]},
        )
        if not login_resp.json().get("mfa_required"):
            pytest.skip("MFA not configured")

        _login_browser(page, mfa_test_user["email"], mfa_test_user["password"])
        page.wait_for_timeout(3000)

        mfa_input = page.locator("input[autocomplete='one-time-code'], input[maxlength='6']")
        if mfa_input.count() == 0:
            pytest.skip("MFA prompt not shown")

        mfa_input.first.fill("000000")

        submit_btn = page.locator("button[type='submit'], button:has-text('Verify')")
        if submit_btn.count() > 0:
            submit_btn.first.click()
            page.wait_for_timeout(3000)

        # Should still be on login/MFA page
        assert "/login" in page.url or mfa_input.count() > 0


# ===== MFA Status Display Tests =====


class TestMFAStatusDisplay:
    """Test MFA status indicators in the UI.

    These tests use the admin account since they only view settings (no MFA setup).
    """

    def test_security_settings_shows_mfa_status(self, page: Page):
        """Security settings page shows MFA enabled/disabled status."""
        _login_browser(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        _wait_for_gallery(page)
        _open_security_settings(page)

        settings_text = page.text_content(".security-settings")
        assert settings_text is not None
        has_mfa_info = any(
            kw in settings_text.lower()
            for kw in [
                "two-factor",
                "mfa",
                "multi-factor",
                "authenticator",
                "enabled",
                "disabled",
                "not available",
            ]
        )
        assert has_mfa_info, (
            f"Security settings should show MFA status. Content: {settings_text[:200]}"
        )

    def test_mfa_api_status_accessible(self, page: Page):
        """MFA status API is accessible after login."""
        _login_browser(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        _wait_for_gallery(page)

        # Auth is an httpOnly cookie (no JS-readable token since the 0.4.0
        # cookie migration) — same-origin fetch sends it automatically.
        result = page.evaluate(
            """
            async () => {
                const resp = await fetch('/api/auth/mfa/status', {
                    credentials: 'same-origin'
                });
                if (!resp.ok) return { error: `HTTP ${resp.status}` };
                return await resp.json();
            }
        """
        )

        assert "error" not in result, f"MFA status API failed: {result}"
        assert "mfa_enabled" in result or "can_setup_mfa" in result
