"""E2E: the file-detail menus at 1180 px and 390 px (#1282 item B).

The Export and Download dropdowns, and the Reprocess control, open on the file-detail page
for the suite's own injected transcript. At each width an open menu must sit inside the
viewport, must not scroll sideways, and must close on Escape. Read-only: nothing is created
or changed.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Locator
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = [pytest.mark.responsive]

WIDTHS = [(1180, 900), (390, 844)]


@pytest.fixture
def detail_page(
    browser: Any, shared_auth_state: str, base_url: str, owned_diarized_transcript_file: dict
) -> Any:
    def _open(width: int, height: int) -> tuple[Any, Page]:
        context = browser.new_context(
            storage_state=shared_auth_state,
            viewport={"width": width, "height": height},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.goto(f"{base_url}/files/{owned_diarized_transcript_file['uuid']}")
        expect(page.locator(".transcript-segment").first).to_be_visible(timeout=APP_SHELL_READY_MS)
        return context, page

    opened: list[tuple[Any, Page]] = []

    def factory(width: int, height: int) -> Page:
        pair = _open(width, height)
        opened.append(pair)
        return pair[1]

    yield factory
    for context, page in opened:
        page.close()
        context.close()


def _inside_viewport(box: dict[str, float] | None, width: int, what: str) -> None:
    assert box is not None, f"{what} has no box"
    assert box["x"] >= -0.5 and box["x"] + box["width"] <= width + 0.5, (
        f"{what} leaves the {width}px viewport: {box}"
    )


def _open_menu_metrics(page: Page, trigger: Locator, content: Locator) -> dict[str, Any]:
    """Open the menu and measure it; Escape closes it afterwards (checked by the caller)."""
    trigger.scroll_into_view_if_needed()
    trigger.click()
    expect(content).to_be_visible()
    metrics = {
        "entries": content.locator("button").count(),
        "box": content.bounding_box(),
        "menu_spill": content.evaluate("el => el.scrollWidth - el.clientWidth"),
        "page_spill": page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        ),
    }
    page.keyboard.press("Escape")
    expect(content).to_be_hidden()
    return metrics


def _check_menu(metrics: dict[str, Any], width: int, what: str) -> None:
    assert metrics["entries"] >= 2, f"{what} lists fewer than two entries"
    _inside_viewport(metrics["box"], width, f"{what} menu")
    assert metrics["menu_spill"] <= 1, f"{what} menu scrolls sideways by {metrics['menu_spill']}px"
    assert metrics["page_spill"] <= 1, f"{what} adds {metrics['page_spill']}px of page overflow"


@pytest.mark.parametrize(("width", "height"), WIDTHS)
def test_export_menu_fits_and_closes(detail_page: Any, width: int, height: int) -> None:
    page = detail_page(width, height)
    metrics = _open_menu_metrics(
        page,
        page.locator(".export-transcript-button"),
        page.locator(".export-dropdown:not(.download-dropdown) .export-dropdown-content"),
    )
    _check_menu(metrics, width, "Export")
    assert metrics["entries"] >= 6, f"Export lists too few formats: {metrics['entries']}"


@pytest.mark.parametrize(("width", "height"), WIDTHS)
def test_download_menu_fits_and_closes(detail_page: Any, width: int, height: int) -> None:
    page = detail_page(width, height)
    metrics = _open_menu_metrics(
        page,
        page.locator(".download-button"),
        page.locator(".download-dropdown .export-dropdown-content"),
    )
    _check_menu(metrics, width, "Download")
    assert metrics["entries"] >= 2, "Download lists no audio choices"


@pytest.mark.parametrize(("width", "height"), WIDTHS)
def test_reprocess_control_is_reachable_and_opens_inside_the_viewport(
    detail_page: Any, width: int, height: int
) -> None:
    page = detail_page(width, height)
    button = page.locator(".reprocess-button-header")
    button.scroll_into_view_if_needed()
    expect(button).to_be_visible()
    _inside_viewport(button.bounding_box(), width, "Reprocess button")
    button.click()
    dialog = page.locator('[role="dialog"]').first
    expect(dialog).to_be_visible()
    _inside_viewport(dialog.bounding_box(), width, "Reprocess dialog")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
