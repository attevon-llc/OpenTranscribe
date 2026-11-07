"""E2E: the first-run wizard's operator-terms step, walked in a real browser (#1366).

Until now only a vitest component test (``FirstRunWizard.test.ts``) covered the terms step; four
e2e files instead *dismiss* the wizard through the completion endpoint. This one exercises the
wizard itself, so it must NOT use ``first_run_wizard_completed`` / ``complete_first_run_wizard``
(the guard below fails if anything requests that fixture), and it needs a fresh stack whose
wizard has never been touched. Run it first, alone, with ``scripts/e2e/run-e2e-first-run.sh``;
``run-e2e.sh`` excludes the ``first_run`` marker.

It changes server state on purpose (the operator acknowledgment is recorded, then the wizard is
skipped to completion), which is why a throwaway ``--fresh`` stack is the only valid target.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.first_run

STATUS = "/api/admin/first-run-wizard/status"


def _status(backend_url: str, token: str) -> dict[str, Any]:
    response = requests.get(
        f"{backend_url}{STATUS}", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    assert response.status_code == 200, f"wizard status: HTTP {response.status_code}"
    return dict(response.json())


@pytest.fixture
def wizard_page(browser: Any, shared_auth_state: str, base_url: str) -> Any:
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    yield page
    page.close()
    context.close()


def test_terms_step_gates_the_wizard_until_the_operator_accepts(
    request: pytest.FixtureRequest,
    wizard_page: Page,
    base_url: str,
    backend_url: str,
    admin_token: str,
) -> None:
    assert "first_run_wizard_completed" not in request.fixturenames, (
        "this test exercises the wizard; a fixture that completes it first makes it unreachable"
    )
    before = _status(backend_url, admin_token)
    assert before["completed_at"] is None and before["legal_required"], (
        f"not a fresh stack (wizard already completed or terms not required): {before}"
    )
    assert not before["legal_acknowledged"], f"terms already acknowledged: {before}"

    page = wizard_page
    page.goto(base_url)

    # Step 1 of 5, and it cannot be skipped past.
    terms = page.get_by_test_id("wizard-terms-step")
    expect(terms).to_be_visible(timeout=APP_SHELL_READY_MS)
    indicator = page.get_by_test_id("wizard-step-indicator")
    expect(indicator).to_have_attribute("data-step", "1")
    expect(page.get_by_test_id("terms-panel")).to_be_visible(timeout=15000)
    expect(page.locator(".wizard-skip-link")).to_have_count(0)

    # Accepting requires the checkbox.
    accept = page.get_by_test_id("terms-accept-button")
    checkbox = page.get_by_test_id("terms-checkbox")
    expect(checkbox).not_to_be_checked()
    expect(accept).to_be_disabled()
    assert not _status(backend_url, admin_token)["legal_acknowledged"]

    checkbox.check()
    expect(accept).to_be_enabled()
    accept.click()

    # Accepting advances to step 2 and the acknowledgment is recorded on the server.
    expect(indicator).to_have_attribute("data-step", "2", timeout=15000)
    expect(terms).to_have_count(0)
    after = _status(backend_url, admin_token)
    assert after["legal_acknowledged"], f"acknowledgment not recorded: {after}"
    assert after["completed_at"] is None, "accepting the terms must not complete the wizard"

    # From step 2 the wizard may be skipped, which completes it.
    page.locator(".wizard-skip-link").click()
    expect(indicator).to_have_count(0, timeout=15000)
    assert _status(backend_url, admin_token)["completed_at"] is not None
