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
# 1100 and 900 are the widths that exposed the clipping: the sidebar leaves the list narrower
# than the viewport, so only a container-width breakpoint (not a viewport one) fits it.
WIDTHS = (1440, 1100, 1024, 900, 420)
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


_OVERFLOW_JS = """
() => {
  const list = document.querySelector('.file-list').getBoundingClientRect();
  const out = { overhang: 0, headerScroll: 0, rowScroll: 0, statusInside: false };
  const header = document.querySelector('.file-list-header');
  const row = document.querySelector('.file-list-row');
  out.headerScroll = header.scrollWidth - header.clientWidth;
  out.rowScroll = row.scrollWidth - row.clientWidth;
  for (const root of [header, row]) {
    for (const cell of root.querySelectorAll('.list-cell')) {
      if (getComputedStyle(cell).display === 'none') continue;
      const b = cell.getBoundingClientRect();
      out.overhang = Math.max(out.overhang, b.right - list.right, list.left - b.left);
    }
  }
  const s = row.querySelector('.list-cell-status').getBoundingClientRect();
  out.statusInside = s.width > 0 && s.left >= list.left - 0.5 && s.right <= list.right + 0.5;
  return out;
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
@pytest.mark.parametrize("width", WIDTHS)
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


@pytest.mark.parametrize("locale", ["en", "ar"])
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("selecting", [False, True], ids=["normal", "selecting"])
def test_no_list_cell_overflows_the_list(list_view_page, width, locale, selecting) -> None:
    """Columns degrade with the LIST's width: nothing overhangs it and Status stays visible."""
    page = list_view_page(width, locale)
    if selecting:
        page.click(".select-btn")
        page.wait_for_selector(".file-list-header.selecting-mode")

    result = page.evaluate(_OVERFLOW_JS)

    assert result["statusInside"], f"status cell clipped or hidden: {result}"
    assert result["overhang"] <= TOLERANCE_PX, f"a cell overhangs the list: {result}"
    assert result["headerScroll"] <= TOLERANCE_PX, f"header scrolls horizontally: {result}"
    assert result["rowScroll"] <= TOLERANCE_PX, f"row scrolls horizontally: {result}"
