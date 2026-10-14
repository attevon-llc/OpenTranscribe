"""
E2E coverage for where File Status lives (issue #1243).

File Status used to be a button in the gallery's sub-navigation, a gallery tab, a Settings
panel and a page. It now has one canonical page (``/file-status``), a condensed face in
Settings -> System, and no top-level navigation entry; the gallery's "All files" tab (a
duplicate of the Gallery link) went with it.

Requirements:
- Dev environment running: ./opentr.sh start dev (admin@example.com / password)

Run (headless):
    pytest backend/tests/e2e/test_file_status_nav.py -v
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.settings


@pytest.fixture
def home_page(browser, shared_auth_state: str, base_url: str):  # type: ignore[no-untyped-def]
    """A pre-authenticated page on the app home (one login per run, via the shared state)."""
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".user-button", timeout=30000)
    yield page
    page.close()
    context.close()


def _open_file_status_section(page: Page) -> None:
    page.locator(".user-button").click()
    page.locator(".dropdown-menu .dropdown-item", has_text="Settings").first.click()
    expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
    page.locator(".settings-sidebar .nav-item", has_text="File status").first.click()
    expect(page.locator(".settings-content .section-title").first).to_have_text(
        "File status", timeout=8000
    )


class TestFileStatusPlacement:
    """One canonical page, reachable from Settings; nothing duplicated in the navbar."""

    def test_navbar_has_no_file_status_or_all_files_entry(self, home_page: Page) -> None:
        """No File Status / All files control, and exactly one link home (Gallery)."""
        navbar = home_page.locator(".navbar")
        expect(navbar.locator("a[href='/']")).to_have_count(1)
        expect(navbar.locator("a[href='/file-status']")).to_have_count(0)
        expect(navbar.get_by_text(re.compile(r"File Status|All files", re.I))).to_have_count(0)

    def test_settings_panel_summarises_and_opens_full_screen(self, home_page: Page) -> None:
        """Settings -> System -> File status shows counts; "Open full screen" opens the page."""
        _open_file_status_section(home_page)
        expect(home_page.locator(".settings-content .count-card")).to_have_count(5, timeout=10000)

        home_page.get_by_test_id("file-status-open-full").click()
        expect(home_page).to_have_url(re.compile(r"/file-status$"), timeout=10000)
        expect(home_page.locator(".file-status-page").first).to_be_visible(timeout=15000)
        # The modal closes itself on the route change.
        expect(home_page.locator(".settings-modal")).to_have_count(0, timeout=8000)

    def test_full_page_still_loads_by_deep_link(self, home_page: Page, base_url: str) -> None:
        """The route is kept: bookmarks and notification links to /file-status keep working."""
        home_page.goto(f"{base_url}/file-status")
        expect(home_page.locator(".file-status-page").first).to_be_visible(timeout=15000)
        expect(home_page.locator(".file-status-container")).to_be_visible(timeout=15000)
