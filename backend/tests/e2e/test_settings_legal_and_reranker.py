"""E2E: the Legal settings tab with its document reader, and the reranker-threads row (#1282).

Two items of the by-hand browser pass that no e2e covered. Both are read-only: the reader only
GETs a document (it never acknowledges one) and the statistics tab only reads.

Organization collections, the audit log and legal holds are NOT here: they are managed-edition
surfaces (see ``test_org_privacy_panel.py``), which a community stack cannot render.
"""

from __future__ import annotations

import pytest
import requests
from playwright.sync_api import Page
from playwright.sync_api import expect
from privacy_ui import PAGE_READY_MS
from privacy_ui import open_settings_section

pytestmark = pytest.mark.settings


def test_legal_tab_lists_documents_and_the_reader_opens_and_closes(
    authenticated_page: Page,
) -> None:
    page = authenticated_page
    open_settings_section(page, "Legal")
    rows = page.locator('[data-testid="legal-doc-list"] .legal-row')
    expect(rows.first).to_be_visible(timeout=PAGE_READY_MS)
    assert rows.count() >= 1

    read = rows.first.get_by_role("button", name="Read")
    read.click()
    # The reader is a dialog stacked above Settings; its fingerprint proves the document loaded.
    fingerprint = page.locator('[data-testid="legal-fingerprint"]')
    expect(fingerprint).to_be_visible(timeout=15000)
    assert (fingerprint.get_attribute("title") or "").strip(), "document has no sha256"
    expect(page.locator('[data-testid="legal-fullscreen-toggle"]')).to_be_visible()

    page.keyboard.press("Escape")
    expect(fingerprint).to_have_count(0)
    # Escape closed only the reader: Settings is still open behind it.
    expect(page.locator(".settings-modal")).to_be_visible()


def test_reranker_threads_row_matches_the_server(
    authenticated_page: Page, backend_url: str, admin_token: str
) -> None:
    response = requests.get(
        f"{backend_url}/api/system/stats",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    assert response.status_code == 200, response.text[:200]
    reranker = response.json()["system"]["chat_reranker_threads"]
    assert reranker, "the server reports no reranker thread cap"

    page = authenticated_page
    open_settings_section(page, "System Statistics")
    row = page.locator('[data-testid="reranker-threads"]')
    expect(row).to_be_visible(timeout=PAGE_READY_MS)
    expect(row).to_contain_text("Reranker threads")
    if reranker["threads"] is None:
        expect(row).to_contain_text("OMP_NUM_THREADS")
    else:
        expect(row).to_contain_text(str(reranker["threads"]))
