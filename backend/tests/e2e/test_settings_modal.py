"""
E2E smoke test for the Settings modal.

Regression safety net ahead of the frontend component refactor
(branch: refactor/frontend-overhaul). Smoke-level only: opens the modal
the way the real UI exposes it (Navbar user dropdown -> Settings) and
switches between a handful of settings sections, asserting each renders
its section heading without new console errors.

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173, Backend at localhost:5174
  (admin@example.com / password)

Run (headless):
    pytest backend/tests/e2e/test_settings_modal.py -v

Run (visible on XRDP):
    DISPLAY=:11 pytest backend/tests/e2e/test_settings_modal.py -v --headed
"""

from __future__ import annotations

import os
import tempfile

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.settings

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")  # noqa: S105

# Stable, per-user sections that render a `.section-title` quickly without
# waiting on heavy admin data loads. Each entry: (sidebar nav label, expected
# title text). Labels are matched as substrings of the nav-item text.
SECTIONS_TO_SWITCH = [
    ("Profile & Security", "Profile"),
    ("Transcription Settings", "Transcription"),
    ("Recording Settings", "Recording"),
    ("Download", "Download"),
]

# Pre-existing, non-regression console noise (see test_file_detail_transcript).
BENIGN_CONSOLE_SUBSTRINGS = (
    "Failed to load resource",
    "status code 401",
    "Failed to fetch user info",
    "/suggestions",
    "favicon",
    "401 (Unauthorized)",
    "404 (Not Found)",
)


def _unexpected_console_errors(errors: list[str]) -> list[str]:
    """Drop known-benign console noise; return anything that looks like a real bug."""
    return [e for e in errors if not any(sub in e for sub in BENIGN_CONSOLE_SUBSTRINGS)]


# ---------------------------------------------------------------------------
# Module-scoped auth: log in ONCE via the form, reuse cookies for every test.
# Per-test form logins trip the backend's per-IP auth rate limiting (the same
# reason test_gallery_actions.py / test_media_download.py log in once). When
# the wider e2e suite has already spent the rate-limit budget, the login can
# briefly bounce; retry through that window rather than flapping.
# ---------------------------------------------------------------------------
def _form_login_with_retry(page, attempts: int = 4) -> None:
    """Submit the login form, retrying through transient auth rate-limiting."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(FRONTEND_URL)
            # Already authenticated (cookie still valid) — no form to fill.
            if page.locator(".user-button").count():
                page.wait_for_selector(".user-button", timeout=10000)
                return
            page.wait_for_selector("#email", timeout=15000)
            page.fill("#email", TEST_ADMIN_EMAIL)
            page.fill("#password", TEST_ADMIN_PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector(".user-button", timeout=20000)
            return
        except Exception as exc:  # noqa: BLE001 - retry on any login-flow failure
            last_error = exc
            page.wait_for_timeout(5000 * (attempt + 1))
    raise AssertionError(f"Could not log in via form after {attempts} attempts: {last_error}")


@pytest.fixture(scope="module")
def auth_storage_state(browser):  # type: ignore[no-untyped-def]
    """Login once and persist browser storage state for reuse across tests."""
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080}, ignore_https_errors=True
    )
    page = context.new_page()
    _form_login_with_retry(page)

    fd, state_file = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    context.storage_state(path=state_file)
    page.close()
    context.close()

    yield state_file

    if os.path.exists(state_file):
        os.unlink(state_file)


@pytest.fixture
def app_page(browser, auth_storage_state: str):  # type: ignore[no-untyped-def]
    """A pre-authenticated page on the app home, with console-error capture."""
    context = browser.new_context(
        storage_state=auth_storage_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page._console_errors = errors  # type: ignore[attr-defined]
    page.goto(FRONTEND_URL)
    page.wait_for_selector(".user-button", timeout=30000)
    yield page
    page.close()
    context.close()


def _open_settings_modal(page: Page) -> None:
    """Open the Settings modal via the Navbar user dropdown -> Settings."""
    user_button = page.locator(".user-button")
    expect(user_button).to_be_visible(timeout=15000)
    user_button.click()

    settings_item = page.locator(".dropdown-menu .dropdown-item", has_text="Settings")
    expect(settings_item.first).to_be_visible(timeout=5000)
    settings_item.first.click()

    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)


class TestSettingsModal:
    """Smoke coverage for opening the modal and navigating sections."""

    def test_settings_modal_opens(self, app_page: Page) -> None:
        """The Settings modal opens from the Navbar and shows its sidebar."""
        _open_settings_modal(app_page)
        expect(app_page.locator(".settings-modal[role=dialog]")).to_be_visible(timeout=5000)
        expect(app_page.locator(".settings-sidebar")).to_be_visible(timeout=5000)
        # Sidebar should expose multiple navigable sections.
        nav_items = app_page.locator(".settings-sidebar .nav-item")
        assert nav_items.count() >= 3, "Settings sidebar should list several sections"

    def test_switching_sections_renders_each(self, app_page: Page) -> None:
        """Switching between sections renders each one without new console errors."""
        _open_settings_modal(app_page)

        switched = 0
        for nav_label, expected_title in SECTIONS_TO_SWITCH:
            nav_item = app_page.locator(".settings-sidebar .nav-item", has_text=nav_label)
            if nav_item.count() == 0:
                # Section not available for this user/role — tolerate and move on.
                continue
            nav_item.first.click()

            # The active section's content heading should reflect the selection.
            title = app_page.locator(".settings-content .section-title").first
            expect(title).to_be_visible(timeout=8000)
            expect(title).to_contain_text(expected_title, timeout=8000)
            app_page.wait_for_timeout(300)
            switched += 1

        assert switched >= 3, (
            f"Expected to switch through at least 3 settings sections, did {switched}"
        )

        unexpected = _unexpected_console_errors(app_page._console_errors)  # type: ignore[attr-defined]
        assert not unexpected, f"Unexpected console errors while switching sections: {unexpected}"

    def test_settings_modal_closes(self, app_page: Page) -> None:
        """The Settings modal can be closed via its close button."""
        _open_settings_modal(app_page)
        app_page.locator(".settings-modal .modal-close-button").click()
        expect(app_page.locator(".settings-modal")).to_have_count(0, timeout=8000)


def _open_section(page: Page, nav_label: str) -> None:
    """Open the Settings modal and switch to the given sidebar section."""
    _open_settings_modal(page)
    nav_item = page.locator(".settings-sidebar .nav-item", has_text=nav_label)
    if nav_item.count() == 0:
        pytest.skip(f"Settings section '{nav_label}' not available for this user")
    nav_item.first.click()
    expect(page.locator(".settings-content .section-title").first).to_be_visible(timeout=8000)


class TestProfileSection:
    """Profile & Security section (issue #123 Phase 3) — mutation-free checks."""

    def test_account_fields_render(self, app_page: Page) -> None:
        """Email (read-only) and full-name fields render with current values."""
        _open_section(app_page, "Profile & Security")
        email = app_page.locator("#email")
        expect(email).to_be_visible(timeout=8000)
        expect(email).to_be_disabled()
        expect(email).to_have_value(TEST_ADMIN_EMAIL)
        expect(app_page.locator("#fullName")).to_be_visible()
        expect(app_page.locator("#fullName")).to_be_editable()

    def test_password_change_rejects_wrong_current(self, app_page: Page) -> None:
        """A wrong current password is rejected server-side (nothing persists)."""
        _open_section(app_page, "Profile & Security")
        current = app_page.locator("#currentPassword")
        expect(current).to_be_visible(timeout=8000)
        current.fill("definitely-not-the-password-123!")
        app_page.fill("#newPassword", "SomeNewSecure123!")
        app_page.fill("#confirmPassword", "SomeNewSecure123!")
        # Locale-independent: the submit button of the change-password form
        app_page.locator("form:has(#currentPassword) button[type=submit]").click()
        # The backend rejects the wrong current password -> error toast shown
        expect(app_page.locator(".toast.toast-error")).to_be_visible(timeout=10000)


class TestTranscriptionSection:
    """Transcription Settings section (issue #123 Phase 3) — read-only checks."""

    def test_transcription_controls_render(self, app_page: Page) -> None:
        """Speaker-behavior selector renders; min/max inputs when applicable."""
        _open_section(app_page, "Transcription Settings")
        expect(app_page.locator("#speaker-behavior")).to_be_visible(timeout=8000)
        # min/max inputs render only for the defaults/saved behaviors
        if app_page.locator("#min-speakers").count():
            expect(app_page.locator("#min-speakers")).to_be_visible()
            expect(app_page.locator("#max-speakers")).to_be_visible()
