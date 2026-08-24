"""E2E accessibility (a11y) smoke assertions via axe-core.

Regression guard for accessibility on the highest-value pages ahead of the
frontend component refactor (branch: refactor/frontend-overhaul). Uses
``axe-playwright-python`` (which BUNDLES axe-core — no CDN fetch at runtime)
against the sync Playwright ``Page``.

The app carries known, pre-existing a11y debt (dozens of ``svelte-ignore``
directives), so this is NOT a wall of red: it asserts "no NEW serious/critical
violations beyond a recorded baseline". The baseline is the set of axe rule IDs
that currently produce serious/critical violations, committed alongside this
test in ``a11y_baseline.json``. A test fails only when a serious/critical
violation appears whose rule ID is NOT already in the baseline.

Regenerate the baseline (e.g. after intentionally fixing or accepting changes)::

    python3 scripts/update-a11y-baseline.py

That script (not this file) OWNS writing ``a11y_baseline.json`` — see its module
docstring and ``a11y_lib.py`` for why the write path was pulled out of a pytest test.

Run (headless)::

    pytest backend/tests/e2e/test_a11y.py -v

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173, Backend at localhost:5174
  (admin@example.com / password)
"""

from __future__ import annotations

from typing import Any

import pytest
from a11y_lib import BASELINE_PATH
from a11y_lib import form_login_with_retry
from a11y_lib import gated_violations
from a11y_lib import load_baseline
from a11y_lib import run_axe
from playwright.sync_api import Page
from playwright.sync_api import expect

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


# ---------------------------------------------------------------------------
# Per-page axe scans. Each accumulates new serious/critical rule IDs into the
# shared dict so the final aggregation test can compare against the baseline.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def discovered_rule_ids() -> dict[str, set[str]]:
    """Accumulator for serious/critical rule IDs seen across all scanned pages."""
    return {"ids": set()}


def _assert_no_new_violations(
    page_label: str,
    results: Any,
    baseline: set[str],
    discovered: dict[str, set[str]],
) -> None:
    """Record gated rule IDs and assert none fall outside the baseline."""
    gated = gated_violations(results)
    rule_ids = {v["id"] for v in gated}
    discovered["ids"].update(rule_ids)

    new_rule_ids = rule_ids - baseline
    if new_rule_ids:
        details = "\n".join(
            f"  - {v['id']} ({v['impact']}): {v['help']} [{len(v['nodes'])} node(s)]"
            for v in gated
            if v["id"] in new_rule_ids
        )
        pytest.fail(
            f"New serious/critical a11y violation(s) on {page_label} not in "
            f"baseline ({BASELINE_PATH.name}):\n{details}\n\n"
            f"If these are intentional/accepted, regenerate the baseline with "
            f"python3 scripts/update-a11y-baseline.py"
        )


class TestAccessibility:
    """axe-core smoke scans on the highest-value pages."""

    def test_gallery_home_a11y(
        self,
        authed_page: Page,
        discovered_rule_ids: dict[str, set[str]],
    ) -> None:
        """The gallery / home page has no new serious/critical violations."""
        page = authed_page
        expect(page.locator(".user-button")).to_be_visible(timeout=30000)
        page.wait_for_load_state("networkidle")
        results = run_axe(page)
        _assert_no_new_violations("/ (gallery/home)", results, load_baseline(), discovered_rule_ids)

    def test_settings_modal_a11y(
        self,
        authed_page: Page,
        discovered_rule_ids: dict[str, set[str]],
    ) -> None:
        """The Settings modal has no new serious/critical violations."""
        page = authed_page
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
        _assert_no_new_violations("Settings modal", results, load_baseline(), discovered_rule_ids)

    def test_speakers_page_a11y(
        self,
        authed_page: Page,
        discovered_rule_ids: dict[str, set[str]],
        base_url: str,
    ) -> None:
        """The /speakers page has no new serious/critical violations."""
        page = authed_page
        page.goto(f"{base_url}/speakers")
        page.wait_for_load_state("networkidle")
        # The speakers route must actually render its main container.
        expect(page.locator("main, .speakers-page, .page-container").first).to_be_visible(
            timeout=15000
        )
        # Kept deliberately: same reason as the modal scan above — let the route's entry
        # transition finish before axe reads computed styles (issue #431).
        page.wait_for_timeout(500)
        results = run_axe(page)
        _assert_no_new_violations("/speakers", results, load_baseline(), discovered_rule_ids)

    def test_baseline_is_current(
        self,
        authed_page: Page,
        discovered_rule_ids: dict[str, set[str]],
    ) -> None:
        """The committed baseline exactly matches what the scans above just observed.

        Runs last (source order, after the three page scans above populate the
        shared ``discovered_rule_ids`` accumulator), so this compares the LIVE
        baseline file against rule IDs actually seen this run — not a re-run of
        axe. Real findings in both directions:

        - A rule id observed but not in the baseline: the app regressed. (The
          scan test for that page already fails on this — this assertion is
          a second, independent check of the same fact.)
        - A rule id in the baseline but NOT observed: the baseline is STALE —
          the violation was fixed (or the element removed) and nothing prunes
          the accepted-debt list automatically, so it silently keeps masking a
          rule ID that could reappear elsewhere without ever failing again.

        Regenerate with ``python3 scripts/update-a11y-baseline.py`` (not pytest —
        see that script and ``a11y_lib.py`` for why the write path lives there).
        """
        baseline = load_baseline()
        observed = discovered_rule_ids["ids"]
        assert observed == baseline, (
            f"a11y baseline ({BASELINE_PATH.name}) does not match what was just observed.\n"
            f"  In baseline but NOT observed (stale — remove or investigate): "
            f"{sorted(baseline - observed)}\n"
            f"  Observed but NOT in baseline (new regression): {sorted(observed - baseline)}\n"
            "Regenerate with: python3 scripts/update-a11y-baseline.py"
        )
