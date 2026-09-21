#!/usr/bin/env python3
"""Generate paste-ready lines for the a11y allowlist (backend/tests/e2e/a11y-allowlist.txt).

Issue #785 replaced the old flat, rule-ID-only baseline (``a11y_baseline.json``) with a
per-surface, count-aware, reason-carrying allowlist. This script used to OWN writing that
baseline directly (see the git history of ``a11y_lib.py``'s module docstring for why the write
path was pulled out of a pytest test in the first place — a "test" that skipped unless an env
var was set, and in that mode silently overwrote the exact record it claimed to verify). That
class of defect — a regeneration script that silently rewrites the accepted-findings record —
must not come back through the side door now that the record carries WRITTEN REASONS: a script
that "regenerates" the file can only ever invent a placeholder reason, and an unedited paste
of a placeholder is indistinguishable from a real, reviewed one once it is on disk.

So this script now **never writes**. It scans every surface ``test_a11y.py`` scans, in BOTH
``light`` and ``dark`` theme (issue #972), and prints paste-ready
``<surface>::<theme>::<rule id>::<count>  # reason`` lines to stdout with a reason that is
visibly wrong if pasted unedited (``BACKLOG — REPLACE THIS REASON``), and leaves editing +
committing the allowlist to a human.

Usage::

    python3 scripts/update-a11y-baseline.py
    python3 scripts/update-a11y-baseline.py --base-url http://localhost:5173

Requirements: dev stack running (``./opentr.sh start dev``), and ``backend/venv`` activated with
playwright + axe-playwright-python installed (``backend/requirements-dev.txt``).

Chat, file-detail and upload are scanned too (unlike the old 3-surface script) so this tool
never drifts from what ``test_a11y.py`` actually covers. Chat and file-detail need real data
this standalone script does not create for itself (an LLM provider; a completed recording) —
when unavailable, it prints a note and skips just that surface rather than failing the whole
run. ``file-status-badges`` scans the dedicated ``/a11y-fixtures/status-badges`` fixture route
(issue #972) rather than the live ``/file-status`` page, so its counts don't depend on which
task states happen to be queued right now.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_DIR = REPO_ROOT / 'backend' / 'tests' / 'e2e'
sys.path.insert(0, str(E2E_DIR))

from a11y_lib import (  # noqa: E402
    BACKLOG_PREFIX,
    KNOWN_THEMES,
    evaluate_surface,
    form_login_with_retry,
    run_axe,
    set_theme,
)
from playwright.sync_api import sync_playwright  # noqa: E402

#: Placeholder reason. Visibly wrong if pasted into the allowlist unedited — the point is
#: that a reviewer (or `scripts/audit-tests.py`-style CI check on the a11y allowlist parser)
#: can never mistake this for a considered decision.
_PLACEHOLDER_REASON = f'{BACKLOG_PREFIX} — REPLACE THIS REASON'

_SAMPLE_MEDIA = E2E_DIR.parent / 'fixtures' / 'media' / 'sample_short.wav'


def _print_surface(surface: str, theme: str, page: object) -> None:
    page.wait_for_timeout(500)  # let the entry transition settle before axe reads styles
    results = run_axe(page)  # type: ignore[arg-type]
    # empty allowlist: report EVERYTHING found
    outcome = evaluate_surface(surface, theme, results, {})
    if not outcome.observed:
        print(f'# {surface}::{theme}: clean, no serious/critical violations observed')
        return
    for rule_id, count in sorted(outcome.observed.items()):
        print(f'{surface}::{theme}::{rule_id}::{count}  # {_PLACEHOLDER_REASON}')


def _upload_sample(base_url: str, token: str) -> str | None:
    """Upload the committed sample clip and wait for completion; return its uuid or None."""
    import requests

    if not _SAMPLE_MEDIA.exists():
        print(f'# file-detail: skipped — missing fixture {_SAMPLE_MEDIA}')
        return None
    headers = {'Authorization': f'Bearer {token}'}
    name = f'a11y-baseline-scan-{uuid.uuid4().hex[:8]}{_SAMPLE_MEDIA.suffix}'
    with _SAMPLE_MEDIA.open('rb') as fh:
        resp = requests.post(
            f'{base_url}/api/files',
            headers=headers,
            files={'file': (name, fh, 'audio/wav')},
            timeout=300,
        )
    if resp.status_code != 200:
        print(f'# file-detail: skipped — upload failed ({resp.status_code})')
        return None
    file_uuid = str(resp.json()['uuid'])
    deadline = time.time() + 300
    consecutive = 0
    status = 'unknown'
    while time.time() < deadline:
        detail = requests.get(f'{base_url}/api/files/{file_uuid}', headers=headers, timeout=30)
        status = detail.json().get('status', 'unknown') if detail.status_code == 200 else 'unknown'
        if status in ('error', 'cancelled'):
            break
        consecutive = consecutive + 1 if status == 'completed' else 0
        if consecutive >= 2:
            return file_uuid
        time.sleep(3)
    print(f'# file-detail: skipped — upload never completed (status={status})')
    requests.delete(f'{base_url}/api/files/{file_uuid}', headers=headers, timeout=30)
    return None


def _delete_sample(base_url: str, token: str, file_uuid: str) -> None:
    import requests

    requests.delete(
        f'{base_url}/api/files/{file_uuid}',
        headers={'Authorization': f'Bearer {token}'},
        timeout=30,
    )


def _admin_token(backend_url: str) -> str | None:
    import requests

    resp = requests.post(
        f'{backend_url}/api/auth/token',
        data={'username': 'admin@example.com', 'password': 'password'},
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=30,
    )
    if resp.status_code != 200:
        return None
    return str(resp.json()['access_token'])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--base-url',
        default=os.environ.get('E2E_FRONTEND_URL', 'http://localhost:5173'),
        help='Frontend base URL (default: E2E_FRONTEND_URL env var or http://localhost:5173)',
    )
    parser.add_argument(
        '--backend-url',
        default=os.environ.get('E2E_BACKEND_URL', 'http://localhost:5174'),
        help='Backend base URL, for the file-detail scan only '
        '(default: E2E_BACKEND_URL env var or http://localhost:5174)',
    )
    args = parser.parse_args()

    print('# Paste-ready a11y-allowlist.txt lines — review EVERY reason before committing.')
    print(f'# Generated against {args.base_url}\n')

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080}, ignore_https_errors=True
        )
        page = context.new_page()
        form_login_with_retry(page, args.base_url)

        for theme in sorted(KNOWN_THEMES):
            # Gallery / home
            set_theme(page, theme)
            page.wait_for_selector('.user-button', timeout=30000)
            page.wait_for_load_state('networkidle')
            _print_surface('gallery', theme, page)

            # Settings modal — theme must be set BEFORE opening it: set_theme reloads, which
            # would dismiss an already-open modal.
            user_button = page.locator('.user-button')
            user_button.click()
            settings_item = page.locator('.dropdown-menu .dropdown-item', has_text='Settings')
            settings_item.first.click()
            page.wait_for_selector('.settings-modal', timeout=10000)
            _print_surface('settings-modal', theme, page)
            page.keyboard.press('Escape')

            # /speakers
            page.goto(f'{args.base_url}/speakers')
            set_theme(page, theme)
            page.wait_for_load_state('networkidle')
            page.wait_for_selector('main, .speakers-page, .page-container', timeout=15000)
            _print_surface('speakers', theme, page)

            # /search
            page.goto(f'{args.base_url}/search')
            set_theme(page, theme)
            page.wait_for_load_state('networkidle')
            _print_surface('search', theme, page)

            # /file-status — this surface's OWN controls (filters/selects) only. The
            # status-badge contrast rules are covered by file-status-badges below, on a
            # fixture route whose badge mix doesn't depend on the live queue (issue #972).
            page.goto(f'{args.base_url}/file-status')
            set_theme(page, theme)
            page.wait_for_load_state('networkidle')
            _print_surface('file-status', theme, page)

            # file-status-badges — the dedicated fixture route, all 4 states unconditionally.
            page.goto(f'{args.base_url}/a11y-fixtures/status-badges')
            set_theme(page, theme)
            page.wait_for_load_state('networkidle')
            page.wait_for_selector('.status-badge', timeout=15000)
            _print_surface('file-status-badges', theme, page)

            # Upload modal — opens only, never submits, so nothing to clean up. Same reload
            # ordering constraint as the settings modal above.
            page.goto(args.base_url)
            set_theme(page, theme)
            page.wait_for_selector('.upload-btn', timeout=15000)
            page.click('.upload-btn')
            page.wait_for_selector('.tab-button', timeout=5000)
            _print_surface('upload', theme, page)
            page.keyboard.press('Escape')

            # /chat — the shell renders with no LLM provider; scan it regardless.
            page.goto(f'{args.base_url}/chat')
            set_theme(page, theme)
            try:
                page.wait_for_selector('[data-testid="chat-composer-input"]', timeout=15000)
                _print_surface('chat', theme, page)
            except Exception:  # noqa: BLE001 - report and move on, this tool must not crash
                print(
                    f'# chat::{theme}: skipped — composer never rendered '
                    '(no LLM provider configured?)'
                )

            # /files/{uuid} — needs a real completed recording, which this standalone script
            # uploads and deletes itself (never the ambient dev library — issue #785 §4.5).
            # Uploaded fresh per theme rather than shared, so a failed delete in one theme's
            # pass can't leave the other theme's pass scanning a file that no longer exists.
            token = _admin_token(args.backend_url)
            if token is None:
                print(f'# file-detail::{theme}: skipped — could not obtain an admin token')
            else:
                file_uuid = _upload_sample(args.backend_url, token)
                if file_uuid is not None:
                    try:
                        page.goto(f'{args.base_url}/files/{file_uuid}')
                        set_theme(page, theme)
                        page.wait_for_load_state('networkidle')
                        _print_surface('file-detail', theme, page)
                    finally:
                        _delete_sample(args.backend_url, token, file_uuid)

        context.close()
        browser.close()

    print(
        '\n# Every line above has a placeholder reason. Replace each with a real, written\n'
        '# reason before pasting into backend/tests/e2e/a11y-allowlist.txt — an unedited\n'
        f"# '{_PLACEHOLDER_REASON}' must never be committed."
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
