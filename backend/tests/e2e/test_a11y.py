"""E2E accessibility (a11y) smoke assertions via axe-core.

Regression guard for accessibility on the app's main authenticated surfaces. Uses
``axe-playwright-python`` (which BUNDLES axe-core — no CDN fetch at runtime) against the
sync Playwright ``Page``.

The app carries known, pre-existing a11y debt (dozens of ``svelte-ignore`` directives and
the findings recorded in ``a11y-allowlist.txt``), so this is NOT a wall of red: it asserts
"no NEW serious/critical violations beyond the allowlist for THIS surface+theme, and no MORE
nodes than the allowlist accepts". Issue #785 replaced the old flat, rule-ID-only baseline
with this per-surface, count-aware, reason-carrying allowlist — see ``a11y_lib.py`` and
``a11y-allowlist.txt``'s header for the format and the properties it enforces.

Issue #972: every surface below is now scanned in BOTH ``light`` and ``dark`` theme via the
module-scoped, function-scoped ``theme`` fixture (``params=("light", "dark")``, applied via
``a11y_lib.set_theme``). This matters because dark is its own risk, not a duplicate of light:
a status badge composites a translucent colour tint over the dark surface, and that composited
background can fail AA even where the same token passes on a plain surface — measured,
``TasksGrid.svelte``'s dark ``.status-error`` was 4.38:1 on the badge while the SAME token read
5.29:1 on the plain dark surface. Scanning light only would never see that class of defect.

Regenerate paste-ready allowlist lines (never a silent overwrite — see that script's module
docstring) with::

    python3 scripts/update-a11y-baseline.py

Run (headless)::

    pytest backend/tests/e2e/test_a11y.py -v

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173, Backend at localhost:5174
  (admin@example.com / password)
- The `chat` surface additionally needs an LLM provider (e.g. `--with-mock-llm`) and is
  deselected by the `chat` marker otherwise, same as `test_chat.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from a11y_lib import ALLOWLIST_PATH
from a11y_lib import KNOWN_THEMES
from a11y_lib import evaluate_surface
from a11y_lib import form_login_with_retry
from a11y_lib import load_allowlist
from a11y_lib import run_axe
from a11y_lib import set_theme
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.a11y

# This module used to define its own ``FRONTEND_URL`` constant here. A module constant is
# evaluated at import time, so it could not see ``--base-url`` and this file always drove
# whatever was on the default port — even when the run was aimed at an isolated stack
# (issue #431). Everything below takes conftest's ``base_url`` fixture instead.


# ---------------------------------------------------------------------------
# Module-scoped auth: log in ONCE via the form, reuse cookies for every test.
# Per-test form logins trip the backend's per-IP auth rate limiting, so we save
# storage state once and hand each test a pre-authenticated context.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def auth_storage_state(browser: Any, base_url: str) -> Any:
    """Log in once and persist browser storage state for reuse across tests."""
    import os
    import tempfile

    context = browser.new_context(
        viewport={"width": 1920, "height": 1080}, ignore_https_errors=True
    )
    page = context.new_page()
    form_login_with_retry(page, base_url)

    fd, state_file = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    context.storage_state(path=state_file)
    page.close()
    context.close()

    yield state_file

    if os.path.exists(state_file):
        os.unlink(state_file)


@pytest.fixture
def authed_page(browser: Any, auth_storage_state: str, base_url: str) -> Any:
    """A pre-authenticated page on the app home."""
    context = browser.new_context(
        storage_state=auth_storage_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector(".user-button", timeout=30000)
    yield page
    page.close()
    context.close()


@pytest.fixture(params=sorted(KNOWN_THEMES))
def theme(request: Any) -> str:
    """Parameterises every test that takes it over BOTH themes (issue #972).

    A plain ``params=`` fixture (not ``pytest.mark.parametrize`` on each test function)
    because every per-surface scan needs it identically — declaring it once here means a new
    surface test picks up both-theme coverage just by requesting the fixture, with no
    per-test parametrize decorator to forget.
    """
    return str(request.param)


# ---------------------------------------------------------------------------
# Per-page axe scans. Each records what it observed into the shared accumulator so the
# final "is the allowlist current" test can compare against every surface+theme actually
# scanned this run — see a11y_lib.SurfaceResult and the module docstring on
# discovered_results.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def discovered_results() -> dict[tuple[str, str], dict[str, int]]:
    """Accumulator: (surface, theme) -> {rule_id: observed node count}, for combos scanned.

    Deliberately keyed by (surface, theme) — not a flat rule-id set, and not surface alone —
    so the final consistency check can tell "this surface+theme was scanned and is clean"
    from "this surface+theme was never scanned this run" (e.g. `chat`, deselected by marker
    without an LLM provider) — conflating the two would report every allowlist entry for a
    deselected surface as stale (issue #785 §11.3, extended to the theme axis by #972).
    """
    return {}


def _assert_surface_clean(
    surface: str,
    theme: str,
    results: Any,
    allowlist: dict[tuple[str, str, str], Any],
    discovered: dict[tuple[str, str], dict[str, int]],
) -> None:
    """Evaluate one surface+theme's axe results and record what was observed."""
    outcome = evaluate_surface(surface, theme, results, allowlist)
    discovered[(surface, theme)] = outcome.observed
    if outcome.failures:
        pytest.fail(
            f"a11y regression on surface {surface!r} theme {theme!r} "
            f"(allowlist: {ALLOWLIST_PATH.name}):\n"
            + "\n".join(outcome.failures)
            + "\n\nIf these are intentional/accepted, regenerate allowlist lines with "
            "python3 scripts/update-a11y-baseline.py and paste them in with a reason."
        )


class TestAccessibility:
    """axe-core smoke scans on the app's main authenticated surfaces, in both themes."""

    def test_gallery_home_a11y(
        self,
        authed_page: Page,
        theme: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
    ) -> None:
        """The gallery / home page has no new serious/critical violations."""
        page = authed_page
        set_theme(page, theme)
        expect(page.locator(".user-button")).to_be_visible(timeout=30000)
        page.wait_for_load_state("networkidle")
        results = run_axe(page)
        _assert_surface_clean("gallery", theme, results, load_allowlist(), discovered_results)

    def test_settings_modal_a11y(
        self,
        authed_page: Page,
        theme: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
    ) -> None:
        """The Settings modal has no new serious/critical violations."""
        page = authed_page
        set_theme(page, theme)
        user_button = page.locator(".user-button")
        expect(user_button).to_be_visible(timeout=15000)
        user_button.click()
        settings_item = page.locator(".dropdown-menu .dropdown-item", has_text="Settings")
        expect(settings_item.first).to_be_visible(timeout=5000)
        settings_item.first.click()
        expect(page.locator(".settings-modal")).to_be_visible(timeout=10000)
        # Kept deliberately: the modal's open transition must finish before axe reads
        # computed styles — scanning mid-animation reports contrast/visibility findings
        # that do not exist once it settles. run_axe is an evaluate(), not a locator, so
        # there is nothing to auto-wait on (issue #431).
        page.wait_for_timeout(500)
        results = run_axe(page)
        _assert_surface_clean(
            "settings-modal", theme, results, load_allowlist(), discovered_results
        )
        page.keyboard.press("Escape")

    def test_speakers_page_a11y(
        self,
        authed_page: Page,
        theme: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
        base_url: str,
    ) -> None:
        """The /speakers page has no new serious/critical violations."""
        page = authed_page
        page.goto(f"{base_url}/speakers")
        set_theme(page, theme)
        page.wait_for_load_state("networkidle")
        # The speakers route must actually render its main container.
        expect(page.locator("main, .speakers-page, .page-container").first).to_be_visible(
            timeout=15000
        )
        # Kept deliberately: same reason as the modal scan above — let the route's entry
        # transition finish before axe reads computed styles (issue #431).
        page.wait_for_timeout(500)
        results = run_axe(page)
        _assert_surface_clean("speakers", theme, results, load_allowlist(), discovered_results)

    def test_search_page_a11y(
        self,
        authed_page: Page,
        theme: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
        base_url: str,
    ) -> None:
        """The /search page has no new serious/critical violations."""
        page = authed_page
        page.goto(f"{base_url}/search")
        set_theme(page, theme)
        page.wait_for_load_state("networkidle")
        expect(page.locator(".search-page, main").first).to_be_visible(timeout=15000)
        page.wait_for_timeout(500)
        results = run_axe(page)
        _assert_surface_clean("search", theme, results, load_allowlist(), discovered_results)

    def test_file_status_page_a11y(
        self,
        authed_page: Page,
        theme: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
        base_url: str,
    ) -> None:
        """The /file-status page has no new serious/critical violations.

        Covers this page's own controls (filters, selects) — NOT the status-badge contrast
        rules, whose node count depends on which task states happen to be queued right now.
        Those are covered unconditionally by ``test_file_status_badges_a11y`` below
        (issue #972).
        """
        page = authed_page
        page.goto(f"{base_url}/file-status")
        set_theme(page, theme)
        page.wait_for_load_state("networkidle")
        # The route must actually render its page container before axe scans it — a
        # rendering failure and an a11y violation are different failures.
        expect(page.locator(".file-status-page").first).to_be_visible(timeout=15000)
        page.wait_for_timeout(500)
        results = run_axe(page)
        _assert_surface_clean("file-status", theme, results, load_allowlist(), discovered_results)

    def test_file_status_badges_a11y(
        self,
        authed_page: Page,
        theme: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
        base_url: str,
    ) -> None:
        """Every status-badge state (pending/in_progress/completed/failed) has no new violation.

        Scans ``/a11y-fixtures/status-badges`` — a dedicated fixture route that mounts
        ``TasksGrid.svelte`` with one task per status UNCONDITIONALLY, rather than the live
        ``/file-status`` page above, whose badge mix depends on the dev queue's current
        contents. That dependency is exactly how ``file-status::light::color-contrast`` was
        once allowlisted at ``1`` and later observed at ``13`` (issue #972) — more failing
        tasks on screen, same single defect, and a count the allowlist could never pin down.
        """
        page = authed_page
        page.goto(f"{base_url}/a11y-fixtures/status-badges")
        set_theme(page, theme)
        page.wait_for_load_state("networkidle")
        expect(page.locator(".status-badge").first).to_be_visible(timeout=15000)
        # 4 statuses must all be on screen — the whole point of the fixture page is that
        # this can never be fewer depending on what the live queue holds.
        expect(page.locator(".status-badge")).to_have_count(4)
        page.wait_for_timeout(500)
        results = run_axe(page)
        _assert_surface_clean(
            "file-status-badges", theme, results, load_allowlist(), discovered_results
        )

    def test_upload_panel_a11y(
        self,
        authed_page: Page,
        theme: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
        base_url: str,
    ) -> None:
        """The upload stepper modal has no new serious/critical violations.

        Opens the modal only — never submits a file — so this scan creates no data to own
        or clean up (unlike `file-detail` below, which needs a real completed recording).
        """
        page = authed_page
        set_theme(page, theme)
        page.wait_for_selector(".upload-btn", timeout=15000)
        page.click(".upload-btn")
        expect(page.locator("[role=dialog], .modal-backdrop, .upload-modal").first).to_be_visible(
            timeout=5000
        )
        page.wait_for_selector(".tab-button", timeout=5000)
        page.wait_for_timeout(500)
        results = run_axe(page)
        _assert_surface_clean("upload", theme, results, load_allowlist(), discovered_results)
        page.keyboard.press("Escape")

    @pytest.mark.chat
    def test_chat_page_a11y(
        self,
        authed_page: Page,
        theme: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
        base_url: str,
    ) -> None:
        """The /chat page has no new serious/critical violations.

        Marked `chat` (same marker `test_chat.py` uses) so it is DESELECTED, not silently
        skipped, on a run with no LLM provider configured — `run-e2e.sh`'s default
        `-m "not visual and not chat"` selection already excludes it, and the mock-LLM
        overlay auto-management in `scripts/lib/dev-test-overlays.sh` brings the marker back
        in when a phase needs it (issue #785 §4.5 — a surface that cannot be scanned must be
        deselected by marker, never left to skip, or it inflates run-e2e.sh's skip ceiling).
        """
        page = authed_page
        page.goto(f"{base_url}/chat")
        set_theme(page, theme)
        page.wait_for_load_state("networkidle")
        # The composer must actually render before axe scans it — a rendering failure and an
        # a11y violation are different failures.
        expect(page.locator('[data-testid="chat-composer-input"]')).to_be_visible(timeout=15000)
        page.wait_for_timeout(500)
        results = run_axe(page)
        _assert_surface_clean("chat", theme, results, load_allowlist(), discovered_results)

    def test_file_detail_a11y(
        self,
        browser: Any,
        auth_storage_state: str,
        base_url: str,
        theme: str,
        owned_media_factory: Any,
        admin_token: str,
        discovered_results: dict[tuple[str, str], dict[str, int]],
    ) -> None:
        """The file-detail page has no new serious/critical violations.

        Uses `owned_media_factory` (issue #541) for its OWN uploaded, completed recording
        rather than scanning whatever the dev library happens to hold — issue #785 §4.5's
        second coverage constraint. Runs in its own context (not `authed_page`) because it
        needs a real upload through `admin_token` before the page has anything to render.
        """
        media = owned_media_factory(admin_token)
        context = browser.new_context(
            storage_state=auth_storage_state,
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = context.new_page()
        try:
            page.goto(f"{base_url}/files/{media['uuid']}")
            set_theme(page, theme)
            page.wait_for_load_state("networkidle")
            # The route must actually render its page container before axe scans it — a
            # rendering failure and an a11y violation are different failures.
            expect(page.locator(".file-detail-page").first).to_be_visible(timeout=15000)
            page.wait_for_timeout(1000)
            results = run_axe(page)
            _assert_surface_clean(
                "file-detail", theme, results, load_allowlist(), discovered_results
            )
        finally:
            page.close()
            context.close()

    def test_allowlist_is_current(
        self,
        authed_page: Page,
        discovered_results: dict[tuple[str, str], dict[str, int]],
    ) -> None:
        """Every allowlist entry for a surface+theme scanned this run matches what was observed.

        Runs last (source order, after the scans above populate the shared
        ``discovered_results`` accumulator across BOTH theme param instances of every prior
        test), so this compares the LIVE allowlist file against node counts actually seen
        this run — not a re-run of axe. Real findings:

        - An entry whose surface+theme WAS scanned this run but whose observed count is lower
          than the allowlisted count (or the rule id was not observed at all): the allowlist
          is STALE — a node was fixed (or removed) and nothing shrinks the accepted count
          automatically, so it silently keeps covering headroom that could mask a
          regression elsewhere.
        - An observed count HIGHER than allowlisted already fails in that surface's own scan
          test above; this is a second, independent check of the same fact.

        A surface+theme combo that was NOT scanned this run (e.g. `chat`, deselected by
        marker without an LLM provider — deselected identically in both themes) is skipped
        here entirely — an unscanned combo's entries are neither confirmed nor stale, and
        treating an intentional deselection as staleness would delete every entry for that
        surface the moment the marker excludes it (issue #785 §11.3).

        Regenerate with ``python3 scripts/update-a11y-baseline.py`` (prints paste-ready
        lines; never writes the file — see that script and ``a11y_lib.py`` for why).
        """
        allowlist = load_allowlist()
        stale: list[str] = []
        regressed: list[str] = []
        for (surface, entry_theme, rule_id), entry in sorted(allowlist.items()):
            if (surface, entry_theme) not in discovered_results:
                continue
            observed = discovered_results[(surface, entry_theme)].get(rule_id, 0)
            if observed < entry.count:
                stale.append(
                    f"  - {surface}::{entry_theme}::{rule_id}::{entry.count} "
                    f"(line {entry.lineno}) — observed {observed} node(s) this run, lower "
                    "the count or remove the line"
                )
            elif observed > entry.count:
                regressed.append(
                    f"  - {surface}::{entry_theme}::{rule_id}::{entry.count} "
                    f"(line {entry.lineno}) — observed {observed} node(s) this run, exceeds "
                    "the allowlisted count"
                )
        assert not stale and not regressed, (
            f"a11y allowlist ({ALLOWLIST_PATH.name}) does not match what was just observed.\n"
            f"Stale (allowlisted but not fully observed — shrink or remove):\n"
            + ("\n".join(stale) or "  (none)")
            + "\nRegressed (observed more than allowlisted — should have failed above):\n"
            + ("\n".join(regressed) or "  (none)")
            + "\nRegenerate with: python3 scripts/update-a11y-baseline.py"
        )
