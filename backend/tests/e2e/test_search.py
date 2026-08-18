"""
E2E tests for the search page (issue #123 Phase 5).

Covers: route load, welcome/no-results/results states, URL parameter
round-trips (?q= restores a search), clear behavior, the filter sidebar,
and the keyword/semantic mode toggle.

Result-dependent tests skip gracefully when the dev corpus has no matching
indexed media (OpenSearch content varies per environment).

Requirements:
- Dev environment running: ./opentr.sh start dev

Run:
    pytest backend/tests/e2e/test_search.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_search.py -v --headed
"""

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.search

# This module used to define its own ``FRONTEND_URL`` constant here. A module constant is
# evaluated at import time, so it could not see ``--base-url`` and this file always drove
# whatever was on the default port — even when the run was aimed at an isolated stack
# (issue #431). Everything below takes conftest's ``base_url`` fixture instead.

# A term present in the standard dev corpus; result tests skip if absent.
KNOWN_QUERY = "PyTorch"
NONSENSE_QUERY = "zxqv-no-such-term-9817263"


@pytest.fixture
def search_page(gallery_page: Page, base_url: str) -> Page:
    """Navigate the pre-authenticated session to /search."""
    gallery_page.goto(f"{base_url}/search")
    gallery_page.wait_for_selector(".search-page", timeout=15000)
    return gallery_page


def _run_search(page: Page, query: str) -> None:
    page.fill(".search-input", query)
    page.click(".search-btn")
    page.wait_for_load_state("networkidle")


class TestSearchPage:
    """Page load and initial state."""

    def test_search_route_loads(self, search_page: Page):
        """The search page renders its input and search button."""
        expect(search_page.locator(".search-input")).to_be_visible()
        expect(search_page.locator(".search-btn")).to_be_visible()

    def test_welcome_state_when_empty(self, search_page: Page):
        """Without a query the welcome state is shown."""
        expect(search_page.locator(".state-container.welcome")).to_be_visible()

    def test_filter_sidebar_present(self, search_page: Page):
        """The filter sidebar (with its toggle) renders."""
        # Two variants exist (desktop + mobile) — assert at least one is visible
        expect(search_page.locator(".filter-sidebar").first).to_be_visible()
        expect(search_page.locator(".filter-toggle-btn").first).to_be_visible()


class TestSearchExecution:
    """Running searches and state transitions."""

    def test_search_updates_url_query_param(self, search_page: Page):
        """Submitting a search puts ?q= into the URL."""
        _run_search(search_page, NONSENSE_QUERY)
        assert f"q={NONSENSE_QUERY}" in search_page.url

    def test_nonsense_query_leaves_welcome_state(self, search_page: Page):
        """A nonsense query completes into a results or no-results state.

        With neural search enabled the hybrid pipeline soft-demotes (never
        hard-suppresses) semantic matches, so even a gibberish keyword may
        return semantic-only results flagged with the no-keyword notice.
        """
        _run_search(search_page, NONSENSE_QUERY)
        # Kept deliberately: the outcome is read with `.count()`, which does NOT auto-wait,
        # and either count being 0 fails the assertion below. `_run_search` already waits
        # for networkidle, so this is the render settle on top of it (issue #431).
        search_page.wait_for_timeout(2000)
        # The welcome state must be replaced by an outcome state
        expect(search_page.locator(".state-container.welcome")).to_have_count(0)
        no_results = search_page.locator(".state-container .state-title").count()
        semantic_results = search_page.locator(".results-list").count()
        assert no_results or semantic_results, "Search produced neither results nor empty state"

    def test_clear_button_resets_input(self, search_page: Page):
        """The clear button empties the input and restores the welcome state."""
        search_page.fill(".search-input", "anything")
        clear_btn = search_page.locator(".clear-btn")
        expect(clear_btn).to_be_visible(timeout=5000)
        clear_btn.click()
        expect(search_page.locator(".search-input")).to_have_value("")

    def test_url_query_param_restores_search(self, search_page: Page, base_url: str):
        """Visiting /search?q=... prefills the input and runs the search."""
        search_page.goto(f"{base_url}/search?q={NONSENSE_QUERY}")
        search_page.wait_for_load_state("networkidle")
        expect(search_page.locator(".search-input")).to_have_value(NONSENSE_QUERY, timeout=10000)
        # The restored query executes — welcome state must be gone
        expect(search_page.locator(".state-container.welcome")).to_have_count(0)

    def test_known_query_returns_result_cards(self, search_page: Page):
        """Searching the dev corpus returns result cards (skips if no corpus)."""
        _run_search(search_page, KNOWN_QUERY)
        # Kept deliberately: the next statement is a `.count()`-based SKIP gate, which does
        # not auto-wait. Removing the settle would turn "results not rendered yet" into a
        # silent skip — passing for the wrong reason (issue #431).
        search_page.wait_for_timeout(2000)
        results = search_page.locator(".results-list")
        if results.count() == 0:
            pytest.skip(f"No indexed media matching '{KNOWN_QUERY}' in this environment")
        expect(results).to_be_visible()
        assert search_page.locator(".results-list > *").count() > 0

    def test_pagination_never_shows_ellipsis_beside_an_adjacent_page(self, search_page: Page):
        """Regression test for SearchPagination's windowStart-clamping bug.

        `getVisiblePages()` used to insert a leading '...' whenever `current`
        exceeded a threshold, without checking whether the *clamped* window
        start actually left a gap — so paging to page 7 or 8 of a 20-page
        result set rendered a nonsensical "5 ... 6" (page 6 sits directly
        beside page 5, zero-page gap). `SearchPagination.test.ts` already
        proves this for the pure function in isolation; this walks the real
        rendered pager to confirm the fix reaches the live page. Skips if the
        dev corpus doesn't have enough matches for the known query to page
        that deep (paging is server-driven, not something this test can seed).
        """
        _run_search(search_page, KNOWN_QUERY)
        search_page.wait_for_timeout(2000)
        pagination = search_page.locator(".pagination")
        if pagination.count() == 0:
            pytest.skip(f"No pagination rendered for '{KNOWN_QUERY}' in this environment")

        next_btn = pagination.locator(".page-btn.next")
        reached_page_7 = False
        for _ in range(6):
            active = pagination.locator(".page-btn.active")
            if active.count() and active.inner_text().strip() == "7":
                reached_page_7 = True
                break
            if next_btn.count() == 0 or next_btn.is_disabled():
                break
            next_btn.click()
            search_page.wait_for_load_state("networkidle")
            search_page.wait_for_timeout(500)

        if not reached_page_7:
            pytest.skip(f"Fewer than 7 result pages for '{KNOWN_QUERY}' in this environment")

        # Walk the rendered pager in DOM order; an ellipsis directly between
        # two page numbers that differ by exactly 1 is the bug (no real gap).
        # The selector already excludes .prev/.next, so every non-ellipsis
        # match is a numbered page button — int() should never fail here,
        # and if it does that's a real assertion failure worth seeing, not
        # something to swallow.
        items = pagination.locator(".page-btn:not(.prev):not(.next), .ellipsis").all()
        # None marks an ellipsis; an int is a rendered page number.
        parsed: list[int | None] = []
        for el in items:
            cls = el.get_attribute("class") or ""
            if "ellipsis" in cls:
                parsed.append(None)
            else:
                parsed.append(int(el.inner_text().strip()))

        ellipsis_indices = [i for i, v in enumerate(parsed) if v is None]
        # At page 7 of a 20-page result a windowed pager (1 ... 5 6 7 8 9 ... 20)
        # always renders at least one real ellipsis — asserted so this can't
        # pass vacuously if the pager ever stops rendering any gap markers.
        assert ellipsis_indices, f"Expected at least one '...' in a page-7-of-20+ pager: {parsed}"

        for i in ellipsis_indices:
            prev_page = parsed[i - 1] if i > 0 else None
            next_page = parsed[i + 1] if i + 1 < len(parsed) else None
            if prev_page is not None and next_page is not None:
                assert next_page - prev_page != 1, (
                    f"Spurious ellipsis rendered between adjacent pages {prev_page} and {next_page}"
                )


class TestSearchControls:
    """Result-area controls (only rendered once a search ran)."""

    def test_mode_toggle_buttons_present(self, search_page: Page):
        """Keyword/semantic mode toggle appears with search results info."""
        _run_search(search_page, KNOWN_QUERY)
        # Kept deliberately: the next statement is a `.count()`-based SKIP gate, which does
        # not auto-wait. Removing the settle would turn "results not rendered yet" into a
        # silent skip — passing for the wrong reason (issue #431).
        search_page.wait_for_timeout(2000)
        if search_page.locator(".results-list").count() == 0:
            pytest.skip(f"No indexed media matching '{KNOWN_QUERY}' in this environment")
        mode_buttons = search_page.locator(".mode-toggle .mode-btn")
        assert mode_buttons.count() >= 2

    def test_results_info_shows_summary(self, search_page: Page):
        """The result summary line is shown for a successful search."""
        _run_search(search_page, KNOWN_QUERY)
        # Kept deliberately: the next statement is a `.count()`-based SKIP gate, which does
        # not auto-wait. Removing the settle would turn "results not rendered yet" into a
        # silent skip — passing for the wrong reason (issue #431).
        search_page.wait_for_timeout(2000)
        if search_page.locator(".results-list").count() == 0:
            pytest.skip(f"No indexed media matching '{KNOWN_QUERY}' in this environment")
        expect(search_page.locator(".results-info .result-summary")).to_be_visible()
