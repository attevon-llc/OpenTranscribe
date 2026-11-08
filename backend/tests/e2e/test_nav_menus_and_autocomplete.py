"""E2E: the UI-batch checks #1282 listed as "not covered anywhere" (#1266, #1272).

Mobile nav drawer, user dropdown overflow, and search autocomplete. Read-only: nothing is
created or changed, and the autocomplete prefix is the title of the file the suite itself uploaded.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = [pytest.mark.responsive, pytest.mark.search]

NO_SUCH_TERM = "zxqv-no-such-term-9817263"
SUGGESTIONS = "#search-suggestions-listbox"


def _page_at(browser: Any, state: str, width: int, height: int) -> tuple[Any, Page]:
    context = browser.new_context(
        storage_state=state,
        viewport={"width": width, "height": height},
        ignore_https_errors=True,
    )
    return context, context.new_page()


@pytest.fixture
def mobile_page(browser: Any, shared_auth_state: str, base_url: str) -> Any:
    context, page = _page_at(browser, shared_auth_state, 390, 844)
    page.goto(base_url)
    page.wait_for_selector(".mobile-toggle button", timeout=APP_SHELL_READY_MS)
    yield page
    page.close()
    context.close()


def test_mobile_drawer_opens_and_closes_from_toggle_and_overlay(mobile_page: Page) -> None:
    toggle = mobile_page.locator(".mobile-toggle button")
    links = mobile_page.locator(".nav-links")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(mobile_page.locator(".mobile-overlay")).to_have_count(0)

    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(links).to_have_class(re.compile(r"\bopen\b"))
    overlay = mobile_page.locator(".mobile-overlay")
    expect(overlay).to_be_visible()

    # The drawer must put the user menu inside the viewport, not off to the side.
    box = mobile_page.locator(".nav-links.open .user-button").first.bounding_box()
    assert box is not None, "user button has no box inside the open drawer"
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390 + 0.5, box

    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(mobile_page.locator(".mobile-overlay")).to_have_count(0)

    toggle.click()
    expect(mobile_page.locator(".mobile-overlay")).to_be_visible()
    # Click the strip of overlay the drawer does not cover.
    mobile_page.mouse.click(385, 835)
    expect(toggle).to_have_attribute("aria-expanded", "false")


def test_mobile_drawer_does_not_scroll_the_page_sideways(mobile_page: Page) -> None:
    mobile_page.locator(".mobile-toggle button").click()
    expect(mobile_page.locator(".mobile-overlay")).to_be_visible()
    overflow = mobile_page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"open drawer causes {overflow}px of horizontal overflow"


@pytest.mark.parametrize("width", [1440, 1180])
def test_user_dropdown_has_no_horizontal_scrollbar(
    browser: Any, shared_auth_state: str, base_url: str, width: int
) -> None:
    context, page = _page_at(browser, shared_auth_state, width, 900)
    try:
        page.goto(base_url)
        page.wait_for_selector(".user-button", timeout=APP_SHELL_READY_MS)
        page.locator(".user-button").first.click()
        menu = page.locator(".dropdown-menu").first
        expect(menu).to_be_visible()
        items = menu.locator(".dropdown-item")
        assert items.count() >= 2, "user dropdown lists fewer than two items"
        # scrollWidth > clientWidth is exactly what draws a horizontal scrollbar.
        spill = menu.evaluate("el => el.scrollWidth - el.clientWidth")
        assert spill <= 1, f"user dropdown scrolls sideways by {spill}px at {width}px"
        box = menu.bounding_box()
        assert box is not None
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5, box
        page.keyboard.press("Escape")
    finally:
        page.close()
        context.close()


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_user_dropdown_stays_inside_a_390px_viewport(
    browser: Any, shared_auth_state: str, base_url: str, locale: str
) -> None:
    """At phone width the menu must fit, in RTL as well (it spilled ~10px in `ar`)."""
    context, page = _page_at(browser, shared_auth_state, 390, 844)
    try:
        page.add_init_script(f"window.localStorage.setItem('locale', '{locale}')")
        page.goto(base_url)
        page.wait_for_selector(".mobile-toggle button", timeout=APP_SHELL_READY_MS)
        expect(page.locator("html")).to_have_attribute("dir", "rtl" if locale == "ar" else "ltr")
        page.locator(".mobile-toggle button").click()
        page.locator(".nav-links.open .user-button").first.click()
        menu = page.locator(".dropdown-menu").first
        expect(menu).to_be_visible()
        box = menu.bounding_box()
        assert box is not None
        assert box["x"] >= 0 and box["x"] + box["width"] <= 390 + 0.5, box
    finally:
        page.close()
        context.close()


@pytest.fixture
def search_input(gallery_page: Page, base_url: str) -> Page:
    gallery_page.goto(f"{base_url}/search")
    gallery_page.wait_for_selector(".search-input", timeout=APP_SHELL_READY_MS)
    return gallery_page


def test_autocomplete_suggests_for_an_indexed_title_and_navigates_by_keyboard(
    search_input: Page, owned_transcribed_file: dict[str, Any]
) -> None:
    # Suggestions are title and speaker prefixes (search/suggestions.py), never transcript
    # words, so type the start of the title of the file this suite uploaded (and deletes).
    detail = owned_transcribed_file["detail"]
    title = str(detail.get("title") or detail["filename"])
    assert len(title) >= 9, title
    box = search_input.locator(".search-input")
    box.click()
    box.press_sequentially(title[:9], delay=40)

    listbox = search_input.locator(SUGGESTIONS)
    expect(listbox).to_be_visible(timeout=15000)
    options = listbox.get_by_role("option")
    assert options.count() >= 1
    expect(box).to_have_attribute("aria-expanded", "true")
    expect(options.first).to_have_attribute("aria-selected", "false")

    box.press("ArrowDown")
    expect(options.first).to_have_attribute("aria-selected", "true")

    box.press("Escape")
    expect(listbox).to_have_count(0)
    expect(box).to_have_attribute("aria-expanded", "false")


def test_autocomplete_offers_nothing_for_a_term_that_is_not_indexed(search_input: Page) -> None:
    """Control: the listbox is driven by the server's answer, not rendered on any typing."""
    box = search_input.locator(".search-input")
    box.click()
    box.press_sequentially(NO_SUCH_TERM, delay=20)
    # Wait for the debounced request to settle before asserting absence.
    search_input.wait_for_load_state("networkidle")
    expect(search_input.locator(SUGGESTIONS)).to_have_count(0)
    expect(box).to_have_attribute("aria-expanded", "false")
