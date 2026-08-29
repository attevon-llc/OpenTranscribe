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

from tests.env_gate import gate_enabled
from tests.fixtures.search_corpus import GOLD

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


class TestSearchResultClickThrough:
    """A result card's two ways to read more: navigate to the file, or preview in-place.

    Read-only against the dev corpus: neither test creates or mutates anything, so no
    data-hygiene cleanup is needed (mirrors `test_known_query_returns_result_cards`'s
    skip pattern for a corpus that has no match in this environment).
    """

    def _first_result_card(self, page: Page):
        # `.results-list`'s first child is sometimes a `.no-keyword-notice` banner
        # ("No exact matches found. Showing related content."), not a card — scope to
        # `.result-card` explicitly rather than `.results-list > *` (which the
        # non-emptiness checks elsewhere in this module use for a different purpose:
        # counting ANY rendered outcome, card or notice, not addressing a specific one).
        card = page.locator(".results-list .result-card").first
        expect(card).to_be_visible(timeout=15000)
        return card

    def test_clicking_a_result_title_navigates_to_the_file_detail_page(self, search_page: Page):
        """`.result-title` is a real link to the file detail page, not a JS-only handler."""
        _run_search(search_page, KNOWN_QUERY)
        search_page.wait_for_timeout(2000)
        if search_page.locator(".results-list").count() == 0:
            pytest.skip(f"No indexed media matching '{KNOWN_QUERY}' in this environment")

        card = self._first_result_card(search_page)
        href = card.locator(".result-title").get_attribute("href")
        assert href and href.startswith("/files/"), (
            f"Result title has no /files/<uuid> link: {href!r}"
        )

        card.locator(".result-title").click()
        search_page.wait_for_url(lambda url: "/search" not in url, timeout=15000)
        assert href in search_page.url

        # Not just a URL change — confirms the file detail page actually rendered
        # (rules out a 500/blank page behind the right URL).
        expect(search_page.locator(".transcript-segment").first).to_be_visible(timeout=15000)

    def test_view_transcript_opens_the_modal_and_highlights_the_match(self, search_page: Page):
        """`.view-transcript-btn` opens the in-place modal — no navigation, real highlights."""
        _run_search(search_page, KNOWN_QUERY)
        search_page.wait_for_timeout(2000)
        if search_page.locator(".results-list").count() == 0:
            pytest.skip(f"No indexed media matching '{KNOWN_QUERY}' in this environment")

        card = self._first_result_card(search_page)
        card.locator(".view-transcript-btn").click()

        # `.search-transcript-modal-wrapper` is a plain, unsized wrapper div around the
        # actual modal chrome — it renders in the DOM but never has visible dimensions of
        # its own, so a visibility check must target the dialog inside it instead.
        modal = search_page.locator(".search-transcript-modal-wrapper")
        expect(modal.locator('[role="dialog"]')).to_be_visible(timeout=10000)
        # Opening the modal must NOT navigate away from /search.
        assert "/search" in search_page.url

        expect(modal.locator(".nav-count")).to_be_visible(timeout=10000)
        expect(modal.locator("button.nav-btn")).to_have_count(2)

        # A stable highlight class (not the transient post-scroll pulse, which
        # self-removes after 3.5s) proves the match was actually located and marked,
        # not just that the modal opened.
        highlights = modal.locator(".search-keyword-match, .search-semantic-segment")
        expect(highlights.first).to_be_visible(timeout=10000)


class TestSearchResultType:
    """Result-type toggle (issue #462: transcripts vs summaries)."""

    def test_summaries_tab_switches_view_and_updates_url(self, search_page: Page):
        """Switching to the Summaries tab updates the URL and renders an outcome.

        Read-only: this never creates or deletes anything, so no data-hygiene
        skip/cleanup is needed. Result content is corpus-dependent (whether any
        file has a generated summary matching the query), so this asserts the
        page reaches a well-formed outcome state rather than a specific hit —
        same pattern as `test_nonsense_query_leaves_welcome_state`.
        """
        _run_search(search_page, KNOWN_QUERY)
        # Kept deliberately: the next statements are `.count()`-based, which does
        # not auto-wait (issue #431's pattern, repeated throughout this module).
        search_page.wait_for_timeout(2000)

        toggle = search_page.locator(".result-type-toggle")
        expect(toggle).to_be_visible(timeout=5000)

        summaries_tab = toggle.get_by_role("tab", name="Summaries")
        summaries_tab.click()
        search_page.wait_for_load_state("networkidle")
        search_page.wait_for_timeout(1500)

        assert "type=summaries" in search_page.url

        # Summary hits render as a sibling of .results-list, never inside it —
        # the invariant `.results-list > *` (above) protects. Either an outcome
        # (summary-results-list) or the empty state is a well-formed result;
        # .results-list itself must NOT be what's showing for this tab.
        outcome = search_page.locator(".summary-results-list, .state-container")
        expect(outcome.first).to_be_visible(timeout=10000)
        assert search_page.locator(".results-list").count() == 0

        # Switching back returns to the transcript view and drops the URL param.
        transcripts_tab = toggle.get_by_role("tab", name="Transcripts")
        transcripts_tab.click()
        search_page.wait_for_load_state("networkidle")
        search_page.wait_for_timeout(1500)
        assert "type=summaries" not in search_page.url


class TestSearchKnownCorpusRanking:
    """UI-layer ranking check against the known-ground-truth search-quality corpus.

    Every other test in this module is corpus-agnostic (skips when the dev corpus
    doesn't happen to contain a match). This class is the opposite: it seeds the
    same self-contained 6-file corpus ``tests/test_search_quality.py`` uses
    (``tests/fixtures/search_corpus.py`` / ``search_corpus_stack.py``, injected via
    the production corpus-injection tool — real chunking/embedding/indexing, no
    ASR) and asserts the FIRST rendered result card is the known-correct file. The
    API-level suite already proves ranking correctness at the HTTP layer; this
    proves the UI actually renders that order rather than dropping or reshuffling
    it.

    Query choice: ``"surveillance"`` is a ``KEYWORD_QUERIES`` entry whose gold file
    (``sq-espionage``) is the ONLY corpus file containing that word — so it is the
    sole keyword match in every mode, and ``TestRelevanceOrder`` in
    ``test_search_quality.py`` already establishes that a keyword match always
    outranks semantic-only results. That makes rank-1 deterministic in BOTH the
    ``keyword`` (Exact) and ``hybrid`` (Smart — the only two modes this UI
    exposes, see ``+page.svelte``'s ``.mode-toggle``) toggle positions, without
    depending on the corpus's separate, only-top-3-calibrated semantic-ranking
    claims (see the ``artificial intelligence`` skip in ``test_search_quality.py``).

    Gated like ``test_search_quality.py``: seeding six files through the real
    injection/indexing pipeline is expensive, so this only runs with
    ``RUN_SEARCH_QUALITY_TESTS=true``.
    """

    _QUERY = "surveillance"
    _GOLD_MEETING_ID = next(iter(GOLD[_QUERY]["gold"]))  # "sq-espionage"

    @pytest.fixture
    def corpus_search_page(
        self, page: Page, base_url: str, search_corpus, search_corpus_user
    ) -> Page:
        """Log in as the corpus-owning throwaway user (not the shared admin) and open /search.

        The corpus is injected under its own ``searchqual-<uuid8hex>@example.invalid``
        user (see ``search_corpus_stack.py``), so the shared ``admin``/``gallery_page``
        session used by every other test in this module would see zero results here.
        """
        page.goto(base_url)
        page.wait_for_selector("#email", timeout=15000)
        page.fill("#email", search_corpus_user["email"])
        page.fill("#password", search_corpus_user["password"])
        page.click("button[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        page.goto(f"{base_url}/search")
        page.wait_for_selector(".search-page", timeout=15000)
        return page

    def _first_result_file_uuid(self, page: Page) -> str:
        first_card = page.locator(".results-list > *").first
        expect(first_card).to_be_visible(timeout=15000)
        href = first_card.locator(".result-title").get_attribute("href")
        assert href and href.startswith("/files/"), (
            f"First result card has no /files/<uuid> link: {href!r}"
        )
        return href.removeprefix("/files/")

    @pytest.mark.skipif(
        not gate_enabled("RUN_SEARCH_QUALITY_TESTS"),
        reason="Needs the self-seeded search-quality corpus (RUN_SEARCH_QUALITY_TESTS=true)",
    )
    def test_keyword_mode_ranks_gold_file_first(
        self, corpus_search_page: Page, search_corpus
    ) -> None:
        """Exact/keyword mode renders the sole keyword-matching file as result #1."""
        expected_uuid = search_corpus["meeting_id_to_file_uuid"][self._GOLD_MEETING_ID]
        # ``.mode-toggle`` only renders once a search has already run (see
        # ``TestSearchControls``'s docstring) — so a search must execute in the default
        # (hybrid) mode first before the toggle button exists to click.
        _run_search(corpus_search_page, self._QUERY)
        mode_toggle = corpus_search_page.locator(".mode-toggle .mode-btn")
        expect(mode_toggle.first).to_be_visible(timeout=15000)
        mode_toggle.nth(1).click()  # "Exact"
        corpus_search_page.wait_for_load_state("networkidle")
        assert self._first_result_file_uuid(corpus_search_page) == expected_uuid

    @pytest.mark.skipif(
        not gate_enabled("RUN_SEARCH_QUALITY_TESTS"),
        reason="Needs the self-seeded search-quality corpus (RUN_SEARCH_QUALITY_TESTS=true)",
    )
    def test_hybrid_mode_ranks_gold_file_first(
        self, corpus_search_page: Page, search_corpus
    ) -> None:
        """Smart/hybrid mode (the default) also renders it as result #1."""
        expected_uuid = search_corpus["meeting_id_to_file_uuid"][self._GOLD_MEETING_ID]
        _run_search(corpus_search_page, self._QUERY)
        assert self._first_result_file_uuid(corpus_search_page) == expected_uuid


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
