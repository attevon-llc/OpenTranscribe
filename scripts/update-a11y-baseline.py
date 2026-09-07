#!/usr/bin/env python3
"""Regenerate the a11y regression baseline (backend/tests/e2e/a11y_baseline.json).

Standalone replacement for the old ``UPDATE_A11Y_BASELINE=1 pytest backend/tests/e2e/test_a11y.py``
invocation. That flow ran through ``test_baseline_is_current``, which was not a test at all: it
called ``pytest.skip(...)`` unless the env var was set, and in that mode overwrote the exact
baseline file it claimed to verify — a no-assertion finding under ``scripts/audit-tests.py``.
``test_baseline_is_current`` is now a real assertion (it checks the committed baseline matches
what the E2E scans just observed); this script owns the write path exclusively.

Scans the same pages ``test_a11y.py::TestAccessibility`` scans (gallery/home, the Settings
modal, /speakers) with axe-core via the shared helpers in ``backend/tests/e2e/a11y_lib.py``, and
overwrites the committed baseline with every serious/critical rule id observed.

Usage::

    python3 scripts/update-a11y-baseline.py
    python3 scripts/update-a11y-baseline.py --base-url http://localhost:5173

Requirements: dev stack running (``./opentr.sh start dev``), and ``backend/venv`` activated with
playwright + axe-playwright-python installed (``backend/requirements-dev.txt``).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_DIR = REPO_ROOT / 'backend' / 'tests' / 'e2e'
sys.path.insert(0, str(E2E_DIR))

from a11y_lib import (  # noqa: E402
    BASELINE_PATH,
    form_login_with_retry,
    gated_violations,
    run_axe,
    write_baseline,
)
from playwright.sync_api import sync_playwright  # noqa: E402


def _scan(page: object, discovered: set[str]) -> None:
    results = run_axe(page)  # type: ignore[arg-type]
    discovered.update(v['id'] for v in gated_violations(results))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--base-url',
        default=os.environ.get('E2E_FRONTEND_URL', 'http://localhost:5173'),
        help='Frontend base URL (default: E2E_FRONTEND_URL env var or http://localhost:5173)',
    )
    args = parser.parse_args()

    discovered: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080}, ignore_https_errors=True
        )
        page = context.new_page()
        form_login_with_retry(page, args.base_url)

        # Gallery / home
        page.wait_for_selector('.user-button', timeout=30000)
        page.wait_for_load_state('networkidle')
        _scan(page, discovered)

        # Settings modal
        user_button = page.locator('.user-button')
        user_button.click()
        settings_item = page.locator('.dropdown-menu .dropdown-item', has_text='Settings')
        settings_item.first.click()
        page.wait_for_selector('.settings-modal', timeout=10000)
        # Let the modal's open transition finish before axe reads computed styles —
        # scanning mid-animation reports contrast/visibility findings that do not exist
        # once it settles (matches test_a11y.py's same wait).
        page.wait_for_timeout(500)
        _scan(page, discovered)
        page.keyboard.press('Escape')

        # /speakers
        page.goto(f'{args.base_url}/speakers')
        page.wait_for_load_state('networkidle')
        page.wait_for_selector('main, .speakers-page, .page-container', timeout=15000)
        page.wait_for_timeout(500)
        _scan(page, discovered)

        context.close()
        browser.close()

    write_baseline(discovered)
    print(f'Wrote {len(discovered)} rule id(s) to {BASELINE_PATH}')
    for rule_id in sorted(discovered):
        print(f'  - {rule_id}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
