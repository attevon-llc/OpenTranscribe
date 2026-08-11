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

    UPDATE_A11Y_BASELINE=1 pytest backend/tests/e2e/test_a11y.py -v

Run (headless)::

    pytest backend/tests/e2e/test_a11y.py -v

Requirements:
- Dev environment running: ./opentr.sh start dev
- Frontend at localhost:5173, Backend at localhost:5174
  (admin@example.com / password)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page
from playwright.sync_api import expect

# This module used to define its own ``FRONTEND_URL`` constant here. A module constant is
# evaluated at import time, so it could not see ``--base-url`` and this file always drove
# whatever was on the default port — even when the run was aimed at an isolated stack
# (issue #431). Everything below takes conftest's ``base_url`` fixture instead.

# Impacts we gate on. "minor"/"moderate" are tolerated (legacy debt, low value).
GATED_IMPACTS = frozenset({"serious", "critical"})

# Baseline of currently-known serious/critical rule IDs. Committed to git so the
# test is a regression guard, not a snapshot of today's debt.
BASELINE_PATH = Path(__file__).parent / "a11y_baseline.json"

# Set to "1" to rewrite the baseline from the current run instead of asserting.
UPDATE_BASELINE = os.environ.get("UPDATE_A11Y_BASELINE") == "1"


def _load_baseline() -> set[str]:
    """Load the set of accepted serious/critical axe rule IDs from disk."""
    if not BASELINE_PATH.exists():
        return set()
    data = json.loads(BASELINE_PATH.read_text())
    return set(data.get("serious_critical_rule_ids", []))


def _write_baseline(rule_ids: set[str]) -> None:
    """Persist the accepted serious/critical rule IDs as the new baseline."""
    payload = {
        "_comment": (
            "Accepted (pre-existing) serious/critical axe-core rule IDs. "
            "Regenerate with UPDATE_A11Y_BASELINE=1 pytest "
            "backend/tests/e2e/test_a11y.py. The a11y test fails only on rule "
            "IDs NOT listed here."
        ),
        "serious_critical_rule_ids": sorted(rule_ids),
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def _gated_violations(results: Any) -> list[dict[str, Any]]:
    """Return only the serious/critical violations from an axe result."""
    return [v for v in results.response["violations"] if v.get("impact") in GATED_IMPACTS]


def _run_axe(page: Page) -> Any:
    """Run axe-core against the current page, reporting only violations."""
    axe = Axe()
    return axe.run(page)


# ---------------------------------------------------------------------------
# Module-scoped auth: log in ONCE via the form, reuse cookies for every test.
# Per-test form logins trip the backend's per-IP auth rate limiting, so we save
# storage state once and hand each test a pre-authenticated context.
# ---------------------------------------------------------------------------
def _form_login_with_retry(page: Page, base_url: str, attempts: int = 4) -> None:
    """Submit the login form, retrying through transient auth rate-limiting."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(base_url)
            # Already authenticated (cookie still valid) — no form to fill.
            if page.locator(".user-button").count():
                page.wait_for_selector(".user-button", timeout=10000)
                return
            page.wait_for_selector("#email", timeout=15000)
            page.fill("#email", "admin@example.com")
            page.fill("#password", "password")
            page.click("button[type=submit]")
            page.wait_for_selector(".user-button", timeout=20000)
            return
        except Exception as exc:  # noqa: BLE001 - retry on any login-flow failure
            last_error = exc
            # Kept deliberately: this wait IS the rate-limit backoff, not a settle for
            # something a locator could poll for (issue #431).
            page.wait_for_timeout(5000 * (attempt + 1))
    raise AssertionError(f"Could not log in via form after {attempts} attempts: {last_error}")


@pytest.fixture(scope="module")
def auth_storage_state(browser: Any, base_url: str) -> Any:
    """Log in once and persist browser storage state for reuse across tests."""
    import tempfile

    context = browser.new_context(
        viewport={"width": 1920, "height": 1080}, ignore_https_errors=True
    )
    page = context.new_page()
    _form_login_with_retry(page, base_url)

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
# shared dict so the final aggregation test can rewrite the baseline atomically
# when UPDATE_A11Y_BASELINE=1.
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
    gated = _gated_violations(results)
    rule_ids = {v["id"] for v in gated}
    discovered["ids"].update(rule_ids)

    if UPDATE_BASELINE:
        # In update mode we only collect; the aggregation test rewrites the file.
        return

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
            f"UPDATE_A11Y_BASELINE=1 pytest backend/tests/e2e/test_a11y.py"
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
        page.wait_for_selector(".user-button", timeout=30000)
        page.wait_for_load_state("networkidle")
        results = _run_axe(page)
        _assert_no_new_violations(
            "/ (gallery/home)", results, _load_baseline(), discovered_rule_ids
        )

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
        # that do not exist once it settles. _run_axe is an evaluate(), not a locator, so
        # there is nothing to auto-wait on (issue #431).
        page.wait_for_timeout(500)
        results = _run_axe(page)
        _assert_no_new_violations("Settings modal", results, _load_baseline(), discovered_rule_ids)

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
        # Wait for the speakers route to render its main container.
        page.wait_for_selector("main, .speakers-page, .page-container", timeout=15000)
        # Kept deliberately: same reason as the modal scan above — let the route's entry
        # transition finish before axe reads computed styles (issue #431).
        page.wait_for_timeout(500)
        results = _run_axe(page)
        _assert_no_new_violations("/speakers", results, _load_baseline(), discovered_rule_ids)

    def test_baseline_is_current(
        self,
        authed_page: Page,
        discovered_rule_ids: dict[str, set[str]],
    ) -> None:
        """Aggregate scan results: rewrite baseline in update mode, else assert clean.

        This runs last (alphabetically after the scan tests' fixtures populate
        ``discovered_rule_ids``) only meaningfully under UPDATE_A11Y_BASELINE=1,
        where it rewrites the committed baseline from everything observed.
        """
        if not UPDATE_BASELINE:
            pytest.skip("Baseline update only runs under UPDATE_A11Y_BASELINE=1")
        # Ensure the scan tests have already populated the accumulator by running
        # a final home scan here too (idempotent set union).
        page = authed_page
        page.wait_for_load_state("networkidle")
        results = _run_axe(page)
        discovered_rule_ids["ids"].update(v["id"] for v in _gated_violations(results))
        _write_baseline(discovered_rule_ids["ids"])
