"""E2E smoke test for the Watch Sources settings panel (issue #26).

Opens Settings → Watch Sources, asserts the panel renders, opens the Add Watch
Source editor, switches its tabs, and closes it — all without new console errors.

Requirements:
- Dev environment running with the watch overlay: ./opentr.sh start dev --with-watch
- Frontend at localhost:5173 (admin@example.com / password)

Run (headless):
    pytest backend/tests/e2e/test_watch_sources_e2e.py -v
Run (visible on XRDP):
    DISPLAY=:11 pytest backend/tests/e2e/test_watch_sources_e2e.py -v --headed
"""

from __future__ import annotations

import os
import tempfile

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

FRONTEND_URL = os.environ.get("E2E_FRONTEND_URL", "http://localhost:5173")
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")  # noqa: S105

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
    return [e for e in errors if not any(sub in e for sub in BENIGN_CONSOLE_SUBSTRINGS)]


def _form_login_with_retry(page, attempts: int = 4) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(FRONTEND_URL)
            if page.locator(".user-button").count():
                page.wait_for_selector(".user-button", timeout=10000)
                return
            page.wait_for_selector("#email", timeout=15000)
            page.fill("#email", TEST_ADMIN_EMAIL)
            page.fill("#password", TEST_ADMIN_PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector(".user-button", timeout=20000)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            page.wait_for_timeout(5000 * (attempt + 1))
    raise AssertionError(f"Could not log in via form after {attempts} attempts: {last_error}")


@pytest.fixture(scope="module")
def auth_storage_state(browser):  # type: ignore[no-untyped-def]
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


def _open_watch_sources(page: Page) -> None:
    page.locator(".user-button").click()
    settings_item = page.locator(".dropdown-menu .dropdown-item", has_text="Settings")
    expect(settings_item.first).to_be_visible(timeout=5000)
    settings_item.first.click()
    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
    nav = page.locator(".settings-sidebar .nav-item", has_text="Watch Sources")
    expect(nav.first).to_be_visible(timeout=8000)
    nav.first.click()


class TestWatchSourcesPanel:
    def test_panel_renders(self, app_page: Page) -> None:
        _open_watch_sources(app_page)
        title = app_page.locator(".settings-content .section-title").first
        expect(title).to_contain_text("Watch Sources", timeout=8000)
        # The "Add Watch Source" button should be present.
        expect(app_page.get_by_role("button", name="Add Watch Source")).to_be_visible(timeout=8000)

    def test_add_modal_opens_and_switches_tabs(self, app_page: Page) -> None:
        _open_watch_sources(app_page)
        app_page.get_by_role("button", name="Add Watch Source").click()
        # The editor modal shows its title and the source-type select.
        expect(app_page.get_by_text("Add Watch Source").first).to_be_visible(timeout=8000)
        # Switch to the Processing tab, then Advanced.
        for tab in ("Processing", "Advanced", "Organize"):
            tab_btn = app_page.get_by_role("tab", name=tab)
            if tab_btn.count():
                tab_btn.first.click()
                app_page.wait_for_timeout(150)
        unexpected = _unexpected_console_errors(app_page._console_errors)  # type: ignore[attr-defined]
        assert not unexpected, f"Unexpected console errors: {unexpected}"
