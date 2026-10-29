"""E2E: keyboard navigation, ARIA wiring and deep links of the Chat and Watch Sources tabs (#1335).

Both settings sections use the shared ``Tabs`` strip. What is asserted here is what a
screen-reader or keyboard user depends on and a unit test cannot see across component
boundaries: roving tabindex, arrow/Home/End movement, the ``role=tab`` to ``role=tabpanel``
link in BOTH directions (``aria-controls`` and ``aria-labelledby`` must resolve to real
elements), only the active panel being visible, and the settings search landing on the named
tab rather than the section's first one.

Runs as the shared admin (these tabs are admin/super-admin features), read-only: no setting
is saved.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import open_settings
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.settings


@pytest.fixture
def admin_home(browser: Any, shared_auth_state: str, base_url: str) -> Any:
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
    yield page
    page.close()
    context.close()


def _open_section(page: Page, label: re.Pattern[str]) -> None:
    open_settings(page)
    nav = page.locator(".settings-sidebar .nav-item").filter(has_text=label)
    expect(nav.first).to_be_visible(timeout=10000)
    nav.first.click()
    expect(page.locator('.settings-content [role="tablist"]').first).to_be_visible(timeout=15000)


def _selected(page: Page) -> list[str]:
    """Ids of the tabs in the active tablist that report ``aria-selected=true``."""
    ids: list[str] = page.eval_on_selector_all(
        '.settings-content [role="tablist"] [role="tab"][aria-selected="true"]',
        "els => els.map(e => e.id)",
    )
    return ids


def _assert_wiring(page: Page) -> int:
    """Every tab points at a real panel that points back; returns the tab count."""
    wiring = page.eval_on_selector_all(
        '.settings-content [role="tablist"] [role="tab"]',
        """els => els.map(e => {
            const panel = document.getElementById(e.getAttribute('aria-controls'));
            return {
              id: e.id,
              panel: !!panel,
              role: panel && panel.getAttribute('role'),
              back: panel && panel.getAttribute('aria-labelledby'),
              hidden: panel && panel.hidden,
              tabindex: e.getAttribute('tabindex'),
              selected: e.getAttribute('aria-selected'),
            };
          })""",
    )
    assert len(wiring) >= 2, wiring
    for item in wiring:
        assert item["panel"], item
        assert item["role"] == "tabpanel", item
        assert item["back"] == item["id"], item
        # Roving tabindex: exactly the selected tab is in the tab order, and only its
        # panel is shown.
        assert (item["tabindex"] == "0") == (item["selected"] == "true"), item
        assert item["hidden"] == (item["selected"] != "true"), item
    return len(wiring)


class TestChatTabs:
    LABEL = re.compile(r"^\s*Chat\s*$")

    def test_arrow_keys_move_selection_and_focus(self, admin_home: Page) -> None:
        page = admin_home
        _open_section(page, self.LABEL)
        count = _assert_wiring(page)
        first = page.locator("#tab-chat-general")
        last = page.locator("#tab-chat-advanced")
        assert count == 2  # admin sees both; a plain user would see one tab and no strip
        assert _selected(page) == ["tab-chat-general"]

        first.focus()
        page.keyboard.press("ArrowRight")
        expect(last).to_be_focused()
        assert _selected(page) == ["tab-chat-advanced"]
        _assert_wiring(page)

        page.keyboard.press("ArrowRight")  # wraps
        expect(first).to_be_focused()
        assert _selected(page) == ["tab-chat-general"]

        page.keyboard.press("End")
        expect(last).to_be_focused()
        page.keyboard.press("Home")
        expect(first).to_be_focused()
        page.keyboard.press("ArrowLeft")  # wraps backwards
        expect(last).to_be_focused()
        assert _selected(page) == ["tab-chat-advanced"]

    def test_search_deep_link_opens_the_named_tab(self, admin_home: Page) -> None:
        page = admin_home
        open_settings(page)
        search = page.locator(".settings-search input").first
        search.fill("Retrieval cache (seconds)")
        result = page.locator(".settings-search .result-item").first
        expect(result).to_be_visible(timeout=10000)
        result.click()
        expect(page.locator("#tab-chat-advanced")).to_have_attribute(
            "aria-selected", "true", timeout=10000
        )
        expect(page.locator("#tabpanel-chat-advanced")).to_be_visible()
        expect(page.locator("#tabpanel-chat-general")).to_be_hidden()


class TestWatchSourcesTabs:
    LABEL = re.compile(r"Watch Sources")

    def test_arrow_keys_and_wiring(self, admin_home: Page) -> None:
        page = admin_home
        _open_section(page, self.LABEL)
        count = _assert_wiring(page)
        assert count >= 2
        before = _selected(page)
        assert before == ["tab-sources"]

        page.locator("#tab-sources").focus()
        page.keyboard.press("ArrowRight")
        after = _selected(page)
        assert after and after != before
        _assert_wiring(page)
        page.keyboard.press("Home")
        assert _selected(page) == ["tab-sources"]

    def test_search_deep_link_opens_the_email_tab(self, admin_home: Page) -> None:
        page = admin_home
        open_settings(page)
        page.locator(".settings-search input").first.fill("Email Notifications")
        result = page.locator(".settings-search .result-item").first
        expect(result).to_be_visible(timeout=10000)
        result.click()
        expect(page.locator("#tab-email")).to_have_attribute("aria-selected", "true", timeout=10000)
        expect(page.locator("#tabpanel-email")).to_be_visible()
        expect(page.locator("#tabpanel-sources")).to_be_hidden()
