"""E2E: the gallery list view's header cells sit over their data cells (issue #1243 item 14).

In selection mode every header label was shifted 24px right of its column (and wrongly in
RTL). jsdom cannot cascade the scoped CSS, so this measures real bounding boxes.

Read-only: switches the view mode in localStorage of a throwaway context and enters/leaves
selection mode; it creates and changes nothing.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.gallery

TOLERANCE_PX = 1.5
COLUMNS = ("checkbox", "type", "title", "speakers", "duration", "date", "size", "status")

_MEASURE_JS = """
(columns) => {
  const header = document.querySelector('.file-list-header');
  const row = document.querySelector('.file-list-row');
  const edges = {};
  for (const name of columns) {
    const h = header.querySelector('.list-cell-' + name);
    const r = row.querySelector('.list-cell-' + name);
    if (!h || !r || getComputedStyle(h).display === 'none') continue;
    const hb = h.getBoundingClientRect();
    const rb = r.getBoundingClientRect();
    edges[name] = [rb.left - hb.left, rb.right - hb.right];
  }
  return edges;
}
"""


@pytest.fixture
def list_view_page(browser: Browser, shared_auth_state, base_url: str):
    """Pre-authenticated gallery page forced into list view (per-test throwaway context)."""
    pages: list = []

    def make(width: int, locale: str):
        context = browser.new_context(
            storage_state=shared_auth_state,
            viewport={"width": width, "height": 900},
            ignore_https_errors=True,
        )
        context.add_init_script(
            "localStorage.setItem('gallery-view-mode','list');"
            f"localStorage.setItem('locale','{locale}');"
        )
        page = context.new_page()
        pages.append((page, context))
        page.goto(base_url)
        page.wait_for_selector('[data-testid="gallery-files-loaded"]', timeout=APP_SHELL_READY_MS)
        page.wait_for_selector(".file-list-row", timeout=APP_SHELL_READY_MS)
        return page

    yield make
    for page, context in pages:
        page.close()
        context.close()


@pytest.mark.parametrize("locale", ["en", "ar"])
@pytest.mark.parametrize("width", [1440, 1024, 420])
@pytest.mark.parametrize("selecting", [False, True], ids=["normal", "selecting"])
def test_header_cells_align_with_data_cells(list_view_page, width, locale, selecting) -> None:
    page = list_view_page(width, locale)
    if selecting:
        page.click(".select-btn")
        page.wait_for_selector(".file-list-header.selecting-mode")

    edges = page.evaluate(_MEASURE_JS, list(COLUMNS))

    # Guard against a vacuous pass: the always-present columns must have been compared.
    expected = {"type", "title", "status"} | ({"checkbox"} if selecting else set())
    assert expected <= set(edges), f"columns not measured: {expected - set(edges)}"
    misaligned = {
        name: offsets
        for name, offsets in edges.items()
        if max(abs(offsets[0]), abs(offsets[1])) > TOLERANCE_PX
    }
    assert not misaligned, f"header vs data cell [left, right] offsets (px): {misaligned}"
