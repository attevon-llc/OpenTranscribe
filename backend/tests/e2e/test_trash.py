"""E2E: delete a file in the gallery, find it in the Trash, restore it (issue #1101).

Drives the real UI against the live dev stack: the gallery's delete confirmation says how long
the file stays recoverable, the file leaves the gallery and the API (404), the /trash page
lists it, Restore brings it back (200). The file is this test's own upload
(``owned_media_factory``); its teardown moves it to the trash again if needed and deletes it
forever (``delete_media_file`` ends in ``DELETE /api/files/{uuid}/purge``), so nothing is left
in the dev library or the dev trash whatever the outcome.

Run:
    ./scripts/e2e/run-e2e.sh backend/tests/e2e/test_trash.py
    DISPLAY=:11 pytest backend/tests/e2e/test_trash.py -v --headed
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import requests
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.gallery


def _status(backend_url: str, token: str, file_uuid: str) -> int:
    resp = requests.get(
        f"{backend_url}/api/files/{file_uuid}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return resp.status_code


def test_delete_moves_to_trash_and_restore_brings_it_back(
    browser: Any,
    shared_auth_state: str,
    base_url: str,
    backend_url: str,
    admin_token: str,
    owned_media_factory: Any,
) -> None:
    """Gallery delete -> confirmation names the window -> Trash lists it -> Restore."""
    owned = owned_media_factory(admin_token)
    file_uuid = str(owned["uuid"])
    name = str(owned["filename"])

    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    try:
        page = context.new_page()
        page.goto(base_url)
        page.wait_for_selector(".gallery-action-buttons", timeout=APP_SHELL_READY_MS)

        page.click(".select-btn")
        page.wait_for_selector(".select-all-btn", timeout=5000)
        card = page.locator(".file-card", has_text=name)
        expect(card).to_be_visible(timeout=15000)
        card.locator(".file-selector").click()
        page.locator(".delete-btn").click()

        dialog = page.locator("[role=dialog]")
        expect(dialog).to_be_visible(timeout=5000)
        # The confirmation states the server's trash window, not "cannot be undone".
        expect(dialog).to_contain_text(re.compile(r"Recoverable for \d+ days?"))
        dialog.locator(".modal-delete-button").click()

        expect(card).to_have_count(0, timeout=15000)
        assert _status(backend_url, admin_token, file_uuid) == 404, "a trashed file must 404"

        page.goto(f"{base_url}/trash")
        row = page.locator(f'[data-testid="trash-row"][data-file-uuid="{file_uuid}"]')
        expect(row).to_be_visible(timeout=APP_SHELL_READY_MS)
        expect(row).to_have_attribute("data-state", "remaining")
        row.get_by_test_id("trash-restore").click()

        expect(row).to_have_count(0, timeout=15000)
        assert _status(backend_url, admin_token, file_uuid) == 200, "restore must bring it back"
    finally:
        context.close()
