"""Shared a11y-scan helpers for ``test_a11y.py`` and ``scripts/update-a11y-baseline.py``.

Deliberately a plain module with no ``pytest`` import, so the standalone baseline-update
script can reuse the exact same scan/baseline logic the E2E test uses without pulling in
pytest fixture machinery. Splitting this out replaced ``test_a11y.py::test_baseline_is_current``,
which used to be the ONLY place ``_write_baseline`` was called — from inside a "test" that
called ``pytest.skip`` unless ``UPDATE_A11Y_BASELINE=1``, in which case it overwrote the very
baseline it claimed to verify. See ``scripts/update-a11y-baseline.py`` for the regeneration
entry point and ``test_a11y.py::TestAccessibility::test_baseline_is_current`` for the real
"is it current" assertion.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

# Impacts we gate on. "minor"/"moderate" are tolerated (legacy debt, low value).
GATED_IMPACTS = frozenset({"serious", "critical"})

# Baseline of currently-known serious/critical rule IDs. Committed to git so the
# test is a regression guard, not a snapshot of today's debt.
BASELINE_PATH = Path(__file__).parent / "a11y_baseline.json"


def load_baseline() -> set[str]:
    """Load the set of accepted serious/critical axe rule IDs from disk."""
    if not BASELINE_PATH.exists():
        return set()
    data = json.loads(BASELINE_PATH.read_text())
    return set(data.get("serious_critical_rule_ids", []))


def write_baseline(rule_ids: set[str]) -> None:
    """Persist the accepted serious/critical rule IDs as the new baseline."""
    payload = {
        "_comment": (
            "Accepted (pre-existing) serious/critical axe-core rule IDs. Regenerate with "
            "python3 scripts/update-a11y-baseline.py. The a11y test fails only on rule "
            "IDs NOT listed here."
        ),
        "serious_critical_rule_ids": sorted(rule_ids),
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def gated_violations(results: Any) -> list[dict[str, Any]]:
    """Return only the serious/critical violations from an axe result."""
    return [v for v in results.response["violations"] if v.get("impact") in GATED_IMPACTS]


def run_axe(page: Page) -> Any:
    """Run axe-core against the current page, reporting only violations."""
    axe = Axe()
    return axe.run(page)


def form_login_with_retry(page: Page, base_url: str, attempts: int = 4) -> None:
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
