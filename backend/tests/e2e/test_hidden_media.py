"""E2E: hide a file from my view, find it under the Hidden filter, unhide it (issue #1102).

Drives the real gallery against the live dev stack: Organize > "Hide from my view" takes the file
out of the default gallery (and the API list) while it stays readable by direct link, the
"Only hidden" chip lists it, and "Show in my view" brings it back. The file is this test's own
upload (``owned_media_factory``), deleted on teardown, so no hide row or file outlives the test.

Run:
    ./scripts/e2e/run-e2e.sh backend/tests/e2e/test_hidden_media.py
    DISPLAY=:11 pytest backend/tests/e2e/test_hidden_media.py -v --headed
"""

from __future__ import annotations

from typing import Any

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.gallery


def _listed(backend_url: str, token: str, file_uuid: str, hidden: str) -> bool:
    resp = requests.get(
        f"{backend_url}/api/files",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": "100", "ownership": "all", "hidden": hidden},
        timeout=30,
    )
    assert resp.status_code == 200, resp.text[:300]
    return any(item["uuid"] == file_uuid for item in resp.json()["items"])


def _status(backend_url: str, token: str, file_uuid: str) -> int:
    return requests.get(
        f"{backend_url}/api/files/{file_uuid}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ).status_code


def _select_card(page: Page, name: str) -> None:
    if page.locator(".select-all-btn").count() == 0:  # a bulk action may leave select mode on
        page.click(".select-btn")
        page.wait_for_selector(".select-all-btn", timeout=5000)
    card = page.locator(".file-card", has_text=name)
    expect(card).to_be_visible(timeout=15000)
    card.locator(".file-selector").click()


def _organize_action(page: Page, testid: str) -> None:
    page.locator(".organize-btn").click()
    page.get_by_test_id(testid).click()


def test_hide_then_hidden_filter_then_unhide(
    browser: Any,
    shared_auth_state: str,
    base_url: str,
    backend_url: str,
    admin_token: str,
    owned_media_factory: Any,
) -> None:
    """Hide -> gone from the gallery -> Hidden filter lists it -> unhide -> back."""
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
        card = page.locator(".file-card", has_text=name)

        _select_card(page, name)
        _organize_action(page, "gallery-hide-selected")

        expect(card).to_have_count(0, timeout=15000)
        assert not _listed(backend_url, admin_token, file_uuid, "exclude")
        assert _listed(backend_url, admin_token, file_uuid, "only")
        assert _status(backend_url, admin_token, file_uuid) == 200, "hide is not access control"

        if page.get_by_test_id("hidden-filter").count() == 0:
            page.locator(".filter-toggle-container button").first.click()
        page.get_by_test_id("hidden-filter").get_by_role("button", name="Only hidden").click()
        expect(card).to_be_visible(timeout=15000)
        expect(card.get_by_test_id("hidden-badge")).to_be_visible()

        _select_card(page, name)
        _organize_action(page, "gallery-unhide-selected")

        expect(card).to_have_count(0, timeout=15000)  # the "Only hidden" list no longer has it
        assert _listed(backend_url, admin_token, file_uuid, "exclude")
        assert not _listed(backend_url, admin_token, file_uuid, "only")
    finally:
        context.close()
