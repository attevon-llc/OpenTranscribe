"""
PKI Certificate E2E Tests with Playwright

Tests verify PKI/X.509 certificate-based authentication through the browser:
- Admin certificate login flow
- Regular user certificate login flow
- No-certificate fallback to login page
- Role-based UI differences
- Certificate info display in settings

Requirements:
- PKI overlay running (or PKI container started by run-auth-e2e.sh)
- Frontend at https://localhost:5182 (PKI HTTPS port)
- Test certs at scripts/pki/test-certs/clients/
- Set RUN_PKI_E2E=true to enable these tests

Run:
    RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py -v --headed
"""

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page

# Skip entire module unless PKI E2E is explicitly enabled
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PKI_E2E", "").lower() != "true",
    reason="PKI E2E tests require RUN_PKI_E2E=true and PKI overlay running",
)

# PKI HTTPS URL (default port from docker-compose.pki.yml)
PKI_URL = os.environ.get("PKI_E2E_URL", "https://localhost:5182")

# Test certificate paths
_project_root = Path(__file__).resolve().parents[3]
CERTS_DIR = _project_root / "scripts" / "pki" / "test-certs"
CA_CERT = CERTS_DIR / "ca" / "ca.crt"
ADMIN_PFX = CERTS_DIR / "clients" / "admin.p12"
TESTUSER_PFX = CERTS_DIR / "clients" / "testuser.p12"
PFX_PASSPHRASE = "changeit"

# Sidebar nav item that owns the security/certificate panel
# (settings.profile.title, English UI).
PROFILE_NAV_LABEL = "Profile & Security"


def _make_client_cert_config(pfx_path: Path, origin: str):
    """Build Playwright client_certificates config for a .p12 file."""
    return [
        {
            "origin": origin,
            "pfxPath": str(pfx_path),
            "passphrase": PFX_PASSPHRASE,
        }
    ]


def _pki_login(page: Page, pki_origin: str):
    """Navigate to PKI frontend, click certificate button, wait for gallery redirect."""
    page.goto(pki_origin)
    page.wait_for_load_state("networkidle")

    pki_button = page.locator("button:has-text('Certificate'), a:has-text('Certificate')")
    if pki_button.count() > 0:
        pki_button.first.click()

    # Wait for navigation away from login page
    page.wait_for_function(
        "() => !window.location.pathname.includes('/login')",
        timeout=15000,
    )
    page.wait_for_load_state("networkidle")


def _open_settings(page: Page):
    """Open settings modal via the user dropdown menu."""
    page.click(".user-button")
    page.wait_for_selector(".dropdown-item", state="visible", timeout=5000)
    page.click(".dropdown-item:has-text('Settings')")
    page.wait_for_selector(".settings-sidebar", state="visible", timeout=10000)


def _open_security_settings(page: Page):
    """Open Settings -> Profile & Security (where CertificateInfo renders).

    There is no "Security" nav item: SecuritySettings.svelte — which renders
    CertificateInfo for PKI users — is a child of UserProfileSettings.svelte,
    reachable only through the Profile nav item.
    """
    _open_settings(page)
    profile_nav = page.locator(".settings-sidebar .nav-item", has_text=PROFILE_NAV_LABEL).first
    profile_nav.wait_for(state="visible", timeout=5000)
    profile_nav.click()
    page.wait_for_selector(".security-settings", state="visible", timeout=10000)


@pytest.fixture
def pki_origin():
    """Return the PKI origin (scheme + host + port) for cert matching."""
    return PKI_URL


@pytest.fixture
def admin_cert_context(browser, pki_origin):
    """Browser context with admin client certificate."""
    context = browser.new_context(
        ignore_https_errors=True,
        client_certificates=_make_client_cert_config(ADMIN_PFX, pki_origin),
        viewport={"width": 1920, "height": 1080},
    )
    yield context
    context.close()


@pytest.fixture
def testuser_cert_context(browser, pki_origin):
    """Browser context with testuser client certificate."""
    context = browser.new_context(
        ignore_https_errors=True,
        client_certificates=_make_client_cert_config(TESTUSER_PFX, pki_origin),
        viewport={"width": 1920, "height": 1080},
    )
    yield context
    context.close()


@pytest.fixture
def no_cert_context(browser):
    """Browser context without any client certificate."""
    context = browser.new_context(
        ignore_https_errors=True,
        viewport={"width": 1920, "height": 1080},
    )
    yield context
    context.close()


@pytest.mark.pki
class TestPKILoginFlow:
    """Test PKI certificate login flows."""

    def test_admin_cert_login(self, admin_cert_context, pki_origin):
        """Admin certificate should authenticate and redirect to gallery."""
        page = admin_cert_context.new_page()
        _pki_login(page, pki_origin)
        assert "/login" not in page.url, f"Expected redirect from login, still at {page.url}"
        page.close()

    def test_testuser_cert_login(self, testuser_cert_context, pki_origin):
        """Regular user certificate should authenticate and redirect to gallery."""
        page = testuser_cert_context.new_page()
        _pki_login(page, pki_origin)
        assert "/login" not in page.url, f"Expected redirect from login, still at {page.url}"
        page.close()

    def test_no_cert_shows_login_page(self, no_cert_context, pki_origin):
        """Without a certificate, user should see the login page."""
        page = no_cert_context.new_page()
        page.goto(pki_origin)
        page.wait_for_load_state("networkidle")

        login_visible = (
            page.locator("#email").is_visible()
            or page.locator("#password").is_visible()
            or "/login" in page.url
        )
        assert login_visible, f"Expected login page elements, URL is {page.url}"
        page.close()


@pytest.mark.pki
class TestPKIUserRoles:
    """Test that certificate roles map to correct UI permissions."""

    def test_admin_cert_sees_admin_ui(self, admin_cert_context, pki_origin):
        """Admin certificate user should see admin-specific UI elements."""
        page = admin_cert_context.new_page()
        _pki_login(page, pki_origin)
        _open_settings(page)

        # Admin users should see admin-only nav items (User Management, Authentication)
        # Note: System Statistics is visible to all users
        admin_nav = page.locator(".settings-sidebar button.nav-item:has-text('User Management')")
        assert admin_nav.count() > 0, "Admin user should see User Management in settings"
        page.close()

    def test_regular_cert_no_admin_ui(self, testuser_cert_context, pki_origin):
        """Regular certificate user should NOT see admin UI elements."""
        page = testuser_cert_context.new_page()
        _pki_login(page, pki_origin)
        _open_settings(page)

        # Regular users should NOT see admin-only sections
        admin_nav = page.locator(".settings-sidebar button.nav-item:has-text('User Management')")
        assert admin_nav.count() == 0, "Regular user should not see User Management"
        page.close()


@pytest.mark.pki
class TestPKICertificateDisplay:
    """Test that certificate information is displayed in the settings panel."""

    def test_cert_info_visible_in_settings(self, admin_cert_context, pki_origin):
        """After PKI login, certificate/PKI info should be visible in settings."""
        page = admin_cert_context.new_page()
        _pki_login(page, pki_origin)
        _open_security_settings(page)

        # PKI users see "MFA is handled by your identity provider (PKI/Keycloak)"
        # or certificate-related text in the security section
        page_text = page.text_content("body")
        cert_keywords = [
            "pki",
            "certificate",
            "identity provider",
            "keycloak",
            "serial number",
            "fingerprint",
        ]
        has_cert_info = any(kw in page_text.lower() for kw in cert_keywords)

        assert has_cert_info, "PKI/certificate information should be visible in security settings"
        page.close()

    def test_cert_shows_subject_dn(self, admin_cert_context, pki_origin):
        """Certificate Subject DN should be displayed in settings."""
        page = admin_cert_context.new_page()
        _pki_login(page, pki_origin)
        _open_security_settings(page)

        # Admin cert CN is "Admin User" — should appear in settings
        page_text = page.text_content("body")
        has_dn_info = "admin" in page_text.lower() or "subject" in page_text.lower()
        assert has_dn_info, "Certificate subject DN should be visible in settings"
        page.close()
