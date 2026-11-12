"""E2E: the gallery selection bar's Process and Organize menus at 1180 px and 390 px (#1282 item B).

These are the two dropdowns in ``GallerySelectionActions.svelte`` (the issue body calls them the
Process and Organize menus). At each width an open menu must sit inside the viewport, must not
scroll sideways, must not add page overflow, and must close on an outside click. Read-only: the
menus are only opened, no entry is chosen and nothing is created or changed.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = [pytest.mark.responsive, pytest.mark.gallery]

WIDTHS = [(1180, 900), (390, 844)]


@pytest.fixture
def selecting_gallery(browser: Any, shared_auth_state: str, base_url: str) -> Any:
    opened: list[tuple[Any, Page]] = []

    def factory(width: int, height: int) -> Page:
        context = browser.new_context(
            storage_state=shared_auth_state,
            viewport={"width": width, "height": height},
            ignore_https_errors=True,
        )
        page: Page = context.new_page()
        opened.append((context, page))
        page.goto(base_url)
        page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)
        page.locator(".select-btn").click()
        expect(page.locator(".selection-actions")).to_be_visible()
        return page

    yield factory
    for context, page in opened:
        page.close()
        context.close()


@pytest.mark.parametrize(("width", "height"), WIDTHS)
@pytest.mark.parametrize("button", [".process-btn", ".organize-btn"])
def test_selection_menu_fits_and_closes(
    selecting_gallery: Any, width: int, height: int, button: str
) -> None:
    page = selecting_gallery(width, height)
    trigger = page.locator(button)
    trigger.scroll_into_view_if_needed()
    trigger.click()
    menu = page.locator(".selection-actions .dropdown-menu, body > .dropdown-menu").first
    expect(menu).to_be_visible()

    box = menu.bounding_box()
    assert box is not None, f"{button} menu has no box"
    assert box["x"] >= -0.5 and box["x"] + box["width"] <= width + 0.5, (
        f"{button} menu leaves the {width}px viewport: {box}"
    )
    assert menu.locator("button.dropdown-item").count() >= 1, f"{button} menu lists no entries"
    assert menu.evaluate("el => el.scrollWidth - el.clientWidth") <= 1, (
        f"{button} menu scrolls sideways"
    )
    page_spill = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert page_spill <= 1, f"{button} menu adds {page_spill}px of page overflow"

    # Outside click closes it (the component's document click handler).
    page.mouse.click(2, height - 2)
    expect(menu).to_be_hidden()
