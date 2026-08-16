"""
E2E tests for gallery action buttons (issue #139).

Tests the gallery toolbar buttons in both normal mode and selection mode,
including bulk operations (reprocess, summarize, retry, export, etc.)
against a real running dev environment.

Requirements:
- Dev environment running: ./opentr.sh start dev
- At least one completed file in the gallery (e.g. "PyTorch at Tesla")
- Frontend at localhost:5173, Backend at localhost:5174

Run:
    pytest backend/tests/e2e/test_gallery_actions.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_gallery_actions.py -v --headed
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from typing import Any

import pytest
import requests

# Absolute import — the e2e dir is not a package, so a relative import breaks
# collection when invoked as `pytest backend/tests/e2e/` from the repo root.
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD
from playwright.sync_api import Page
from playwright.sync_api import expect

# The `gallery` marker is REGISTERED in e2e/pytest.ini and root CLAUDE.md documents
# `./scripts/e2e/run-e2e.sh -m gallery` as a supported selector — but nothing in the tree
# carried the marker, so the documented command deselected all 333 tests and pytest exited 5
# ("no tests ran") while this file's 52 tests sat here unselected. Module scope, so the whole
# file moves with the selector. test_pytest_config_consistency.py now fails if a registered
# marker selects nothing, so this cannot silently regress.
pytestmark = pytest.mark.gallery

# Test data
TEST_FILE_TITLE = "PyTorch at Tesla"

# URLs come from the `base_url` / `backend_url` fixtures in tests/e2e/conftest.py rather than
# module-level constants: a constant is evaluated at import time, so it can never see
# `--base-url` / `--backend-url` and a run aimed at an isolated stack silently drove the LIVE
# stack instead (issue #431).


def _assert_zip_of(download: Any, extension: str) -> None:
    """Assert a gallery export download is a ZIP whose entries match `extension`.

    Bulk export always produces a presigned ZIP (even for one file) since the
    download-architecture rollout — see /api/files/bulk-export/prepare.
    """
    import zipfile

    assert download.suggested_filename.endswith(".zip"), (
        f"Expected a .zip archive, got: {download.suggested_filename}"
    )
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        download.save_as(tmp.name)
        with zipfile.ZipFile(tmp.name) as archive:
            names = archive.namelist()
            assert names, "Export ZIP is empty"
            wrong = [n for n in names if not n.endswith(extension)]
            assert not wrong, f"Expected only {extension} entries, got: {names}"


# ---------------------------------------------------------------------------
# Session-scoped auth: login ONCE, reuse cookies for all tests
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def auth_storage_state(browser, base_url: str):  # type: ignore[no-untyped-def]
    """Login once and save browser storage state for reuse across all tests."""
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    page.wait_for_selector("#email", timeout=15000)
    page.fill("#email", TEST_ADMIN_EMAIL)
    page.fill("#password", TEST_ADMIN_PASSWORD)
    page.click("button[type=submit]")
    # Wait for gallery to load (confirms login succeeded)
    page.wait_for_selector(".gallery-action-buttons", timeout=30000)
    page.wait_for_selector(".file-card, .file-list-row", timeout=30000)

    # Save storage state to a temp file
    fd, state_file = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    context.storage_state(path=state_file)
    page.close()
    context.close()

    yield state_file

    # Cleanup
    if os.path.exists(state_file):
        os.unlink(state_file)


@pytest.fixture
def gallery_page(browser, auth_storage_state: str, base_url: str):  # type: ignore[no-untyped-def]
    """Create a new page with pre-authenticated cookies and navigate to gallery."""
    context = browser.new_context(
        storage_state=auth_storage_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(base_url)
    # Already authenticated via stored cookies, just wait for gallery
    page.wait_for_selector(".gallery-action-buttons", timeout=30000)
    page.wait_for_selector(".file-card, .file-list-row", timeout=30000)
    yield page
    page.close()
    context.close()


@pytest.fixture(scope="module")
def api_token(backend_url: str) -> str:
    """Get an API token once for the entire module."""
    resp = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    assert resp.status_code == 200, f"API login failed: {resp.status_code}"
    return str(resp.json()["access_token"])


def _api_params(**kwargs: Any) -> dict[str, str]:
    """Build query params dict with string values for requests."""
    return {k: str(v) for k, v in kwargs.items()}


def _get_completed_file_uuid(backend_url: str, token: str) -> str:
    """Get the UUID of a completed file via API."""
    resp = requests.get(
        f"{backend_url}/api/files",
        headers={"Authorization": f"Bearer {token}"},
        params=_api_params(page=1, page_size=20, sort_by="upload_time", sort_order="desc"),
        timeout=30,
    )
    data: dict[str, Any] = resp.json()
    files: list[dict[str, Any]] = data.get("items", data.get("files", []))
    # Prefer the known test file
    for f in files:
        if TEST_FILE_TITLE.lower() in f.get("filename", "").lower():
            return str(f["uuid"])
    # Fall back to any completed file
    for f in files:
        if f.get("status") == "completed":
            return str(f["uuid"])
    pytest.skip("No completed file found in gallery")
    return ""  # unreachable, satisfies mypy


# ---------------------------------------------------------------------------
# Normal Mode Button Tests
# ---------------------------------------------------------------------------
class TestNormalModeButtons:
    """Tests for the gallery action buttons in normal (non-selecting) mode."""

    def test_add_media_button_visible(self, gallery_page: Page) -> None:
        """Add Media button should be visible in the gallery header."""
        btn = gallery_page.locator(".upload-btn")
        expect(btn).to_be_visible(timeout=5000)
        expect(btn).to_contain_text("Add Media")

    def test_add_media_button_has_tooltip(self, gallery_page: Page) -> None:
        """Add Media button should have a descriptive tooltip."""
        btn = gallery_page.locator(".upload-btn")
        title = btn.get_attribute("title")
        assert title is not None and len(title) > 10

    def test_add_media_opens_upload_dialog(self, gallery_page: Page) -> None:
        """Clicking Add Media should open the upload dialog."""
        gallery_page.click(".upload-btn")
        dialog = gallery_page.locator("[role=dialog], .modal-backdrop, .upload-modal")
        expect(dialog.first).to_be_visible(timeout=5000)

    def test_collections_button_visible(self, gallery_page: Page) -> None:
        """Collections button should be visible."""
        btn = gallery_page.locator(".collections-btn")
        expect(btn).to_be_visible(timeout=5000)
        expect(btn).to_contain_text("Collections")

    def test_collections_button_has_tooltip(self, gallery_page: Page) -> None:
        """Collections button should have a descriptive tooltip."""
        btn = gallery_page.locator(".collections-btn")
        title = btn.get_attribute("title")
        assert title is not None and len(title) > 10

    def test_select_button_visible(self, gallery_page: Page) -> None:
        """Select Files button should be visible."""
        btn = gallery_page.locator(".select-btn")
        expect(btn).to_be_visible(timeout=5000)
        expect(btn).to_contain_text("Select")

    def test_select_button_has_tooltip(self, gallery_page: Page) -> None:
        """Select Files button should have a descriptive tooltip."""
        btn = gallery_page.locator(".select-btn")
        title = btn.get_attribute("title")
        assert title is not None and len(title) > 10

    def test_select_enters_selection_mode(self, gallery_page: Page) -> None:
        """Clicking Select should switch to selection mode buttons."""
        gallery_page.click(".select-btn")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        expect(gallery_page.locator(".select-all-btn")).to_be_visible(timeout=5000)
        expect(gallery_page.locator(".process-btn")).to_be_visible(timeout=5000)
        expect(gallery_page.locator(".organize-btn")).to_be_visible(timeout=5000)
        expect(gallery_page.locator(".delete-btn")).to_be_visible(timeout=5000)
        expect(gallery_page.locator(".cancel-btn")).to_be_visible(timeout=5000)

    def test_normal_buttons_not_visible_when_selecting(self, gallery_page: Page) -> None:
        """Normal mode buttons should disappear when entering selection mode."""
        gallery_page.click(".select-btn")
        gallery_page.wait_for_selector(".select-all-btn", timeout=5000)
        expect(gallery_page.locator(".upload-btn")).not_to_be_visible(timeout=3000)
        expect(gallery_page.locator(".collections-btn")).not_to_be_visible(timeout=3000)

    def test_sort_and_view_controls_visible(self, gallery_page: Page) -> None:
        """Sort dropdown, view toggle, and count chip should be on the right."""
        expect(gallery_page.locator(".gallery-header-right")).to_be_visible(timeout=5000)


# ---------------------------------------------------------------------------
# Selection Mode Button Tests
# ---------------------------------------------------------------------------
class TestSelectionModeButtons:
    """Tests for selection mode toolbar buttons."""

    @pytest.fixture(autouse=True)
    def enter_selection_mode(self, gallery_page: Page):  # type: ignore[no-untyped-def]
        """Enter selection mode before each test."""
        gallery_page.click(".select-btn")
        gallery_page.wait_for_selector(".select-all-btn", timeout=5000)
        self.page = gallery_page
        yield

    def test_select_all_button_visible_and_has_tooltip(self) -> None:
        """Select All button should be visible with a tooltip."""
        btn = self.page.locator(".select-all-btn")
        expect(btn).to_be_visible()
        title = btn.get_attribute("title")
        assert title is not None and len(title) > 10

    def test_select_all_toggles_all_files(self) -> None:
        """Clicking Select All should select all files, clicking again deselects."""
        btn = self.page.locator(".select-all-btn")

        # Click to select all
        btn.click()
        # Kept (issue #431): the label flip is a local Svelte re-render with no network
        # activity, and `text_content()` below is a one-shot read that cannot auto-wait.
        self.page.wait_for_timeout(500)
        text_after_select = btn.text_content() or ""
        assert "deselect" in text_after_select.lower() or "all" in text_after_select.lower()

        # Delete button should now show a count > 0
        delete_btn = self.page.locator(".delete-btn")
        delete_text = delete_btn.text_content() or ""
        numbers = re.findall(r"\d+", delete_text)
        assert numbers and int(numbers[0]) > 0, (
            f"Delete button should show non-zero count, got: {delete_text}"
        )

        # Click again to deselect
        btn.click()
        # Kept for the same reason as above: one-shot `text_content()` read (issue #431).
        self.page.wait_for_timeout(500)
        text_after_deselect = btn.text_content() or ""
        assert "select" in text_after_deselect.lower()

    def test_process_dropdown_visible_and_has_tooltip(self) -> None:
        """Process dropdown button should be visible with a tooltip."""
        btn = self.page.locator(".process-btn")
        expect(btn).to_be_visible()
        title = btn.get_attribute("title")
        assert title is not None and len(title) > 10

    def test_process_dropdown_opens(self) -> None:
        """Clicking Process should open dropdown with action items."""
        self.page.click(".process-btn")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        menu = self.page.locator(".dropdown-menu")
        expect(menu).to_be_visible(timeout=3000)

        # Assert by label, not raw count — counts silently break when actions
        # are added (e.g. Redact arrived with the content-redaction feature).
        items = menu.locator(".dropdown-item")
        labels = [items.nth(i).inner_text().strip() for i in range(items.count())]
        for expected in (
            "Reprocess",
            "Summarize",
            "Redact",
            "Retry Failed",
            "Speaker ID",
            "Cancel Processing",
        ):
            assert any(expected.lower() in lbl.lower() for lbl in labels), (
                f"Process dropdown missing '{expected}'. Items: {labels}"
            )

    def test_process_dropdown_items_have_tooltips(self) -> None:
        """Each Process dropdown item should have a tooltip."""
        self.page.click(".process-btn")
        # Kept (issue #431): the loop below drives `items.count()`, a snapshot that
        # returns 0 rather than waiting for the menu to render.
        self.page.wait_for_timeout(300)
        menu = self.page.locator(".dropdown-menu")
        items = menu.locator(".dropdown-item")
        for i in range(items.count()):
            title = items.nth(i).get_attribute("title")
            assert title is not None and len(title) > 10, f"Dropdown item {i} missing tooltip"

    def test_process_items_disabled_when_no_selection(self) -> None:
        """All process dropdown items should be disabled when no files selected."""
        self.page.click(".process-btn")
        # Kept (issue #431): `items.count()` / `is_disabled()` below are snapshots that
        # cannot auto-wait for the menu to render.
        self.page.wait_for_timeout(300)
        menu = self.page.locator(".dropdown-menu")
        items = menu.locator(".dropdown-item")
        for i in range(items.count()):
            assert items.nth(i).is_disabled(), f"Item {i} should be disabled with no files selected"

    def test_organize_dropdown_visible_and_has_tooltip(self) -> None:
        """Organize dropdown button should be visible with a tooltip."""
        btn = self.page.locator(".organize-btn")
        expect(btn).to_be_visible()
        title = btn.get_attribute("title")
        assert title is not None and len(title) > 10

    def test_organize_dropdown_opens(self) -> None:
        """Clicking Organize should open a dropdown offering each organize action."""
        self.page.click(".organize-btn")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        menu = self.page.locator(".dropdown-menu")
        expect(menu).to_be_visible(timeout=3000)

        # Assert WHICH actions are offered, not how many. A bare count == 4 failed the
        # moment Tags was added to this menu, and a count cannot distinguish "the export
        # actions are present" from "there are four of something". Naming them makes the
        # failure message say what is actually missing.
        for label in ("Collection", "Tags", "SRT", "WebVTT", "Text"):
            expect(menu.locator(".dropdown-item", has_text=label)).to_have_count(1, timeout=3000)

    def test_organize_dropdown_items_have_tooltips(self) -> None:
        """Each Organize dropdown item should have a tooltip."""
        self.page.click(".organize-btn")
        # Kept (issue #431): the loop below drives `items.count()`, a snapshot that
        # returns 0 rather than waiting for the menu to render.
        self.page.wait_for_timeout(300)
        menu = self.page.locator(".dropdown-menu")
        items = menu.locator(".dropdown-item")
        for i in range(items.count()):
            title = items.nth(i).get_attribute("title")
            assert title is not None and len(title) > 10, f"Organize item {i} missing tooltip"

    def test_delete_button_visible_and_has_tooltip(self) -> None:
        """Delete button should be visible with a tooltip."""
        btn = self.page.locator(".delete-btn")
        expect(btn).to_be_visible()
        title = btn.get_attribute("title")
        assert title is not None and len(title) > 10

    def test_delete_shows_zero_count_with_no_selection(self) -> None:
        """Delete button should show '0' when no files are checked."""
        btn = self.page.locator(".delete-btn")
        text = btn.text_content() or ""
        numbers = re.findall(r"\d+", text)
        assert numbers and int(numbers[0]) == 0, (
            f"Delete button should show 0 count with no selection, got: {text}"
        )

    def test_cancel_button_visible_and_has_tooltip(self) -> None:
        """Cancel (X) button should be visible with a tooltip."""
        btn = self.page.locator(".cancel-btn")
        expect(btn).to_be_visible()
        title = btn.get_attribute("title")
        assert title is not None and len(title) > 10

    def test_cancel_exits_selection_mode(self) -> None:
        """Clicking Cancel should return to normal mode."""
        self.page.click(".cancel-btn")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        expect(self.page.locator(".upload-btn")).to_be_visible(timeout=5000)
        expect(self.page.locator(".select-btn")).to_be_visible(timeout=5000)

    def test_dropdown_closes_on_outside_click(self) -> None:
        """Dropdowns should close when clicking outside."""
        self.page.click(".process-btn")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        expect(self.page.locator(".dropdown-menu")).to_be_visible()

        self.page.locator(".gallery-header").click(position={"x": 5, "y": 5})
        # Same here — to_have_count(0) polls until the menu is gone (issue #431).
        expect(self.page.locator(".dropdown-menu")).to_have_count(0, timeout=3000)

    def test_opening_one_dropdown_closes_other(self) -> None:
        """Opening Process dropdown should close Organize, and vice versa."""
        self.page.click(".process-btn")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        expect(self.page.locator(".dropdown-menu")).to_have_count(1)

        self.page.click(".organize-btn")
        # Same here (issue #431).
        expect(self.page.locator(".dropdown-menu")).to_have_count(1)

        menu = self.page.locator(".dropdown-menu")
        expect(menu.locator(".dropdown-item").first).to_contain_text("Collection")

    def test_toolbar_does_not_overflow_header(self) -> None:
        """Selection toolbar should not extend past the gallery header right controls."""
        left_section = self.page.locator(".gallery-header-left")
        right_section = self.page.locator(".gallery-header-right")
        left_box = left_section.bounding_box()
        right_box = right_section.bounding_box()

        if left_box and right_box:
            assert left_box["x"] + left_box["width"] <= right_box["x"] + 5, (
                "Action buttons overflow into sort/view controls"
            )


# ---------------------------------------------------------------------------
# Bulk Action Integration Tests (with file selection)
# ---------------------------------------------------------------------------
class TestBulkActions:
    """Tests for bulk actions with actual file selection and backend interaction."""

    @pytest.fixture(autouse=True)
    def setup_selection(self, gallery_page: Page, api_token: str):  # type: ignore[no-untyped-def]
        """Enter selection mode and prepare for bulk action tests."""
        self.page = gallery_page
        self.token = api_token
        gallery_page.click(".select-btn")
        gallery_page.wait_for_selector(".select-all-btn", timeout=5000)
        yield

    def _select_all_files(self) -> None:
        """Select all files in the gallery."""
        self.page.click(".select-all-btn")
        # Kept (issue #431): callers read the resulting selection through
        # `is_disabled()` / `text_content()` snapshots, which cannot auto-wait for the
        # selection store to propagate to the toolbar.
        self.page.wait_for_timeout(500)

    def test_process_reprocess_enabled_with_selection(self) -> None:
        """Reprocess should be enabled when completed files are selected."""
        self._select_all_files()
        self.page.click(".process-btn")
        # Kept (issue #431): `is_disabled()` below is a snapshot — no auto-wait.
        self.page.wait_for_timeout(300)
        menu = self.page.locator(".dropdown-menu")
        reprocess_item = menu.locator(".dropdown-item").first
        assert not reprocess_item.is_disabled(), (
            "Reprocess should be enabled when completed files are selected"
        )

    def test_process_summarize_enabled_with_selection(self) -> None:
        """Summarize should be enabled when completed files are selected."""
        self._select_all_files()
        self.page.click(".process-btn")
        # Kept (issue #431): `is_disabled()` below is a snapshot — no auto-wait.
        self.page.wait_for_timeout(300)
        menu = self.page.locator(".dropdown-menu")
        summarize_item = menu.locator(".dropdown-item").nth(1)
        assert not summarize_item.is_disabled(), (
            "Summarize should be enabled when completed files are selected"
        )

    def test_process_cancel_processing_disabled_for_completed(self) -> None:
        """Cancel Processing should be disabled for completed files."""
        self._select_all_files()
        self.page.click(".process-btn")
        # Kept (issue #431): `is_disabled()` below is a snapshot — no auto-wait.
        self.page.wait_for_timeout(300)
        menu = self.page.locator(".dropdown-menu")
        # Select by text, not index — positional locators break when items are added
        cancel_item = menu.locator(".dropdown-item", has_text="Cancel Processing")
        assert cancel_item.is_disabled(), (
            "Cancel Processing should be disabled when no processing files selected"
        )

    def test_process_speaker_id_enabled_with_selection(self) -> None:
        """Speaker ID should be enabled when completed files are selected."""
        self._select_all_files()
        self.page.click(".process-btn")
        # Kept (issue #431): `is_disabled()` below is a snapshot — no auto-wait.
        self.page.wait_for_timeout(300)
        menu = self.page.locator(".dropdown-menu")
        speaker_item = menu.locator(".dropdown-item").nth(3)
        assert not speaker_item.is_disabled(), (
            "Speaker ID should be enabled when completed files are selected"
        )

    def test_delete_count_updates_with_selection(self) -> None:
        """Delete button count should update when files are selected."""
        self._select_all_files()
        delete_btn = self.page.locator(".delete-btn")
        text = delete_btn.text_content() or ""
        numbers = re.findall(r"\d+", text)
        assert numbers and int(numbers[0]) > 0, (
            f"Delete button should show non-zero count after selecting files, got: {text}"
        )

    def test_export_srt_via_api(self, api_token: str, backend_url: str) -> None:
        """SRT export should return valid subtitle content via the backend API."""
        file_uuid = _get_completed_file_uuid(backend_url, api_token)
        resp = requests.get(
            f"{backend_url}/api/files/{file_uuid}/subtitles",
            headers={"Authorization": f"Bearer {api_token}"},
            params={"subtitle_format": "srt"},
            timeout=30,
        )
        assert resp.status_code == 200, f"SRT export failed: {resp.status_code} {resp.text[:200]}"
        assert "-->" in resp.text, "SRT content should contain --> timestamps"
        assert len(resp.text) > 50, "SRT content should not be empty"

    def test_export_webvtt_via_api(self, api_token: str, backend_url: str) -> None:
        """WebVTT export should return valid subtitle content."""
        file_uuid = _get_completed_file_uuid(backend_url, api_token)
        resp = requests.get(
            f"{backend_url}/api/files/{file_uuid}/subtitles",
            headers={"Authorization": f"Bearer {api_token}"},
            params={"subtitle_format": "webvtt"},
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"WebVTT export failed: {resp.status_code} {resp.text[:200]}"
        )
        assert "WEBVTT" in resp.text, "WebVTT content should start with WEBVTT header"
        assert "-->" in resp.text, "WebVTT content should contain --> timestamps"

    def test_export_txt_via_api(self, api_token: str, backend_url: str) -> None:
        """TXT export should return plain text transcript content."""
        file_uuid = _get_completed_file_uuid(backend_url, api_token)
        resp = requests.get(
            f"{backend_url}/api/files/{file_uuid}/subtitles",
            headers={"Authorization": f"Bearer {api_token}"},
            params={"subtitle_format": "txt"},
            timeout=30,
        )
        assert resp.status_code == 200, f"TXT export failed: {resp.status_code} {resp.text[:200]}"
        assert len(resp.text) > 50, "TXT content should not be empty"

    def test_export_srt_via_ui(self) -> None:
        """Clicking Export SRT in the Organize dropdown should trigger a download."""
        self._select_all_files()
        self.page.click(".organize-btn")

        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        # Selected by TEXT, not .nth(): positional selectors silently retarget when a menu
        # item is inserted above them, which is exactly how this test came to click Tags.
        menu = self.page.locator(".dropdown-menu")
        srt_btn = menu.locator(".dropdown-item", has_text="SRT")
        expect(srt_btn).to_have_count(1)

        with self.page.expect_download(timeout=30000) as download_info:
            srt_btn.click()
        download = download_info.value
        # Bulk export always delivers a presigned ZIP (download-architecture
        # rollout, commit c40c9a8) — verify the archive holds .srt entries.
        _assert_zip_of(download, ".srt")

    def test_export_webvtt_via_ui(self) -> None:
        """Clicking Export WebVTT should trigger a download."""
        self._select_all_files()
        self.page.click(".organize-btn")

        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        # Selected by TEXT, not .nth() — see test_export_srt_via_ui.
        menu = self.page.locator(".dropdown-menu")
        webvtt_btn = menu.locator(".dropdown-item", has_text="WebVTT")
        expect(webvtt_btn).to_have_count(1)

        with self.page.expect_download(timeout=30000) as download_info:
            webvtt_btn.click()
        download = download_info.value
        # ZIP of .vtt files (bulk export always zips; 'webvtt' format -> .vtt)
        _assert_zip_of(download, ".vtt")

    def test_export_txt_via_ui(self) -> None:
        """Clicking Export Text should trigger a download."""
        self._select_all_files()
        self.page.click(".organize-btn")

        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        # Selected by TEXT, not .nth() — see test_export_srt_via_ui.
        menu = self.page.locator(".dropdown-menu")
        txt_btn = menu.locator(".dropdown-item", has_text="Text")
        expect(txt_btn).to_have_count(1)

        with self.page.expect_download(timeout=30000) as download_info:
            txt_btn.click()
        download = download_info.value
        _assert_zip_of(download, ".txt")

    def test_bulk_reprocess_shows_confirmation(self) -> None:
        """Clicking Reprocess should show a confirmation dialog."""
        self._select_all_files()
        self.page.click(".process-btn")
        # Kept (issue #431): the next statement is a positional `.first.click()`, so the
        # menu items must be rendered in their final order before it fires — an index
        # resolved mid-render would click the wrong action.
        self.page.wait_for_timeout(300)
        menu = self.page.locator(".dropdown-menu")
        menu.locator(".dropdown-item").first.click()

        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        modal = self.page.locator("[role=dialog]")
        expect(modal).to_be_visible(timeout=5000)

    def test_add_to_collection_via_organize(self) -> None:
        """Add to Collection in Organize dropdown should trigger the collection dialog."""
        self._select_all_files()
        self.page.click(".organize-btn")

        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        menu = self.page.locator(".dropdown-menu")
        add_btn = menu.locator(".dropdown-item").first
        expect(add_btn).to_contain_text("Collection")
        add_btn.click()

        # Same for the dialog assertion below (issue #431).
        dialog = self.page.locator("[role=dialog], .modal-backdrop")
        expect(dialog.first).to_be_visible(timeout=5000)


# ---------------------------------------------------------------------------
# API-Level Bulk Action Tests (no browser needed)
# ---------------------------------------------------------------------------
class TestBulkActionAPI:
    """Test the backend bulk-action endpoint directly for new actions."""

    def test_bulk_summarize_action(self, api_token: str, backend_url: str) -> None:
        """POST /files/management/bulk-action with action=summarize for a completed file."""
        file_uuid = _get_completed_file_uuid(backend_url, api_token)
        resp = requests.post(
            f"{backend_url}/api/files/management/bulk-action",
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            json={"file_uuids": [file_uuid], "action": "summarize"},
            timeout=30,
        )
        assert resp.status_code == 200, (
            f"Bulk summarize failed: {resp.status_code} {resp.text[:300]}"
        )
        results: list[dict[str, Any]] = resp.json()
        assert len(results) == 1
        # Summarize may fail with LLM_NOT_AVAILABLE if no LLM configured
        if not results[0]["success"]:
            assert results[0].get("error") == "LLM_NOT_AVAILABLE", (
                f"Unexpected summarize error: {results[0]}"
            )

    def test_bulk_action_invalid_action(self, api_token: str, backend_url: str) -> None:
        """Bulk action with unknown action should return an error per file."""
        file_uuid = _get_completed_file_uuid(backend_url, api_token)
        resp = requests.post(
            f"{backend_url}/api/files/management/bulk-action",
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            json={"file_uuids": [file_uuid], "action": "nonexistent_action"},
            timeout=30,
        )
        assert resp.status_code == 200
        results: list[dict[str, Any]] = resp.json()
        assert len(results) == 1
        assert results[0]["success"] is False

    def test_subtitle_export_formats(self, api_token: str, backend_url: str) -> None:
        """All three subtitle formats should return valid content."""
        file_uuid = _get_completed_file_uuid(backend_url, api_token)
        for fmt, marker in [("srt", "-->"), ("webvtt", "WEBVTT"), ("txt", "")]:
            resp = requests.get(
                f"{backend_url}/api/files/{file_uuid}/subtitles",
                headers={"Authorization": f"Bearer {api_token}"},
                params={"subtitle_format": fmt},
                timeout=30,
            )
            assert resp.status_code == 200, f"{fmt} export failed: {resp.status_code}"
            if marker:
                assert marker in resp.text, f"{fmt} content missing expected marker '{marker}'"
            assert len(resp.text) > 20, f"{fmt} content too short"


# ---------------------------------------------------------------------------
# Helpers for end-to-end processing tests
# ---------------------------------------------------------------------------
def _get_shortest_completed_file(backend_url: str, token: str) -> dict[str, Any]:
    """Find the shortest completed file for fast reprocessing tests."""
    resp = requests.get(
        f"{backend_url}/api/files",
        headers={"Authorization": f"Bearer {token}"},
        params=_api_params(page=1, page_size=100, sort_by="duration", sort_order="asc"),
        timeout=30,
    )
    files: list[dict[str, Any]] = resp.json().get("items", [])
    for f in files:
        dur = f.get("duration", 0) or 0
        if f.get("status") == "completed" and 0 < dur <= 300:
            return f
    pytest.skip("No short completed file found for processing tests")
    return {}  # unreachable, satisfies mypy


def _get_error_file(backend_url: str, token: str) -> dict[str, Any] | None:
    """Find a file in error status."""
    resp = requests.get(
        f"{backend_url}/api/files",
        headers={"Authorization": f"Bearer {token}"},
        params=_api_params(page=1, page_size=20, sort_by="upload_time", sort_order="desc"),
        timeout=30,
    )
    files: list[dict[str, Any]] = resp.json().get("items", [])
    for f in files:
        if f.get("status") == "error":
            return f
    return None


def _get_file_status(backend_url: str, token: str, file_uuid: str) -> str:
    """Get the current status of a file."""
    resp = requests.get(
        f"{backend_url}/api/files/{file_uuid}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert resp.status_code == 200, f"Get file failed: {resp.status_code}"
    return str(resp.json().get("status", "unknown"))


def _wait_for_status(
    backend_url: str, token: str, file_uuid: str, target_status: str, timeout_secs: int = 180
) -> str:
    """Poll until file reaches target status or timeout."""
    start = time.time()
    while time.time() - start < timeout_secs:
        status = _get_file_status(backend_url, token, file_uuid)
        if status == target_status:
            return status
        if status == "error" and target_status != "error":
            return status  # Don't keep waiting if it errored
        # Kept (issue #431): pure API polling via requests — this helper takes no Playwright
        # page, so no locator exists to wait on; the sleep is the poll interval itself.
        time.sleep(3)
    return _get_file_status(backend_url, token, file_uuid)


# ---------------------------------------------------------------------------
# End-to-End Processing Tests (reprocess, summarize, retry)
# ---------------------------------------------------------------------------
class TestEndToEndProcessing:
    """Tests that verify reprocess and summarize actually work on short files.

    These tests use the shortest completed file (~1-2 min) for fast turnaround.
    Reprocess test waits for the file to finish processing again.
    """

    def test_reprocess_api_changes_status(self, api_token: str, backend_url: str) -> None:
        """Reprocess via API should change file status from completed to processing."""
        file = _get_shortest_completed_file(backend_url, api_token)
        file_uuid = file["uuid"]
        original_status = file["status"]
        assert original_status == "completed"

        # Trigger reprocess
        resp = requests.post(
            f"{backend_url}/api/files/management/bulk-action",
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            json={"file_uuids": [file_uuid], "action": "reprocess"},
            timeout=30,
        )
        assert resp.status_code == 200
        results: list[dict[str, Any]] = resp.json()
        assert results[0]["success"] is True, f"Reprocess failed: {results[0]}"

        # Status should change from completed
        # Kept (issue #431): verifying backend status transition directly via the API on
        # purpose — this test takes no page fixture, so there is no UI signal to observe.
        time.sleep(2)
        new_status = _get_file_status(backend_url, api_token, file_uuid)
        assert new_status in ("pending", "processing", "queued"), (
            f"After reprocess, expected pending/processing/queued, got: {new_status}"
        )

    def test_reprocess_completes_successfully(self, api_token: str, backend_url: str) -> None:
        """After reprocess, the short file should complete processing again."""
        file = _get_shortest_completed_file(backend_url, api_token)
        file_uuid = file["uuid"]

        # Check current status - if still processing from previous test, just wait
        current = _get_file_status(backend_url, api_token, file_uuid)
        if current == "completed":
            # Trigger reprocess
            resp = requests.post(
                f"{backend_url}/api/files/management/bulk-action",
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                json={"file_uuids": [file_uuid], "action": "reprocess"},
                timeout=30,
            )
            assert resp.status_code == 200
            assert resp.json()[0]["success"] is True

        # Wait for completion (5 min max for a short file with GPU processing)
        final_status = _wait_for_status(
            backend_url, api_token, file_uuid, "completed", timeout_secs=300
        )
        assert final_status == "completed", (
            f"Short file did not complete reprocessing: status={final_status}"
        )

    def test_reprocess_preserves_transcript(self, api_token: str, backend_url: str) -> None:
        """After reprocessing, the file should still have a valid transcript."""
        file = _get_shortest_completed_file(backend_url, api_token)
        file_uuid = file["uuid"]

        # Ensure file is completed
        current = _get_file_status(backend_url, api_token, file_uuid)
        if current != "completed":
            final = _wait_for_status(
                backend_url, api_token, file_uuid, "completed", timeout_secs=300
            )
            assert final == "completed", f"File not completed: {final}"

        # Verify transcript exists via subtitle export
        resp = requests.get(
            f"{backend_url}/api/files/{file_uuid}/subtitles",
            headers={"Authorization": f"Bearer {api_token}"},
            params={"subtitle_format": "srt"},
            timeout=30,
        )
        assert resp.status_code == 200, f"SRT export failed after reprocess: {resp.status_code}"
        assert "-->" in resp.text, "Transcript should have timestamps after reprocess"
        assert len(resp.text) > 50, "Transcript should not be empty after reprocess"

    def test_summarize_api_returns_result(self, api_token: str, backend_url: str) -> None:
        """Summarize via API should either succeed or report LLM not configured."""
        file = _get_shortest_completed_file(backend_url, api_token)
        file_uuid = file["uuid"]

        # Ensure file is completed first
        current = _get_file_status(backend_url, api_token, file_uuid)
        if current != "completed":
            final = _wait_for_status(
                backend_url, api_token, file_uuid, "completed", timeout_secs=300
            )
            assert final == "completed", f"File not completed for summarize: {final}"

        resp = requests.post(
            f"{backend_url}/api/files/management/bulk-action",
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            json={"file_uuids": [file_uuid], "action": "summarize"},
            timeout=30,
        )
        assert resp.status_code == 200
        results: list[dict[str, Any]] = resp.json()
        assert len(results) == 1
        # Either it succeeds (LLM configured) or returns LLM_NOT_AVAILABLE
        if results[0]["success"]:
            assert "message" in results[0]
        else:
            assert results[0].get("error") in ("LLM_NOT_AVAILABLE",), (
                f"Unexpected error: {results[0]}"
            )

    def test_retry_failed_api(self, api_token: str, backend_url: str) -> None:
        """Retry action on an error file should succeed (if error files exist)."""
        error_file = _get_error_file(backend_url, api_token)
        if error_file is None:
            pytest.skip("No error files to test retry")
            return  # unreachable, satisfies mypy

        resp = requests.post(
            f"{backend_url}/api/files/management/bulk-action",
            headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
            json={"file_uuids": [error_file["uuid"]], "action": "retry"},
            timeout=30,
        )
        assert resp.status_code == 200
        results: list[dict[str, Any]] = resp.json()
        assert len(results) == 1
        # Retry should succeed for error files (queues a new task)
        assert results[0]["success"] is True, f"Retry failed: {results[0]}"

    def test_speaker_id_api(self, api_token: str, backend_url: str) -> None:
        """Speaker identification via API should start a task or report LLM not available."""
        file = _get_shortest_completed_file(backend_url, api_token)
        file_uuid = file["uuid"]

        # Ensure file is completed
        current = _get_file_status(backend_url, api_token, file_uuid)
        if current != "completed":
            final = _wait_for_status(
                backend_url, api_token, file_uuid, "completed", timeout_secs=300
            )
            assert final == "completed", f"File not completed for speaker ID: {final}"

        resp = requests.post(
            f"{backend_url}/api/files/{file_uuid}/identify-speakers",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30,
        )
        # Either succeeds (LLM configured) or returns 503 (LLM not available)
        if resp.status_code == 200:
            data: dict[str, Any] = resp.json()
            assert "task_id" in data, f"Expected task_id in response: {data}"
            assert "message" in data
        elif resp.status_code == 503:
            # LLM not configured - this is acceptable
            data = resp.json()
            assert "detail" in data
        else:
            pytest.fail(
                f"Speaker ID returned unexpected status {resp.status_code}: {resp.text[:300]}"
            )


# ---------------------------------------------------------------------------
# UI File Selection & Interaction Tests
# ---------------------------------------------------------------------------
class TestFileSelectionUI:
    """Tests for individual file selection and interaction in the gallery."""

    @pytest.fixture(autouse=True)
    def setup(self, gallery_page: Page):  # type: ignore[no-untyped-def]
        """Store page reference."""
        self.page = gallery_page
        yield

    def test_clicking_file_card_navigates_to_details(self) -> None:
        """Clicking a file card in normal mode should navigate to file details."""
        first_card = self.page.locator(".file-card").first
        expect(first_card).to_be_visible(timeout=5000)
        first_card.click()
        # Should navigate to file details page
        self.page.wait_for_url("**/files/**", timeout=10000)
        assert "/files/" in self.page.url

    def test_individual_file_selection_via_ctrl_click(self) -> None:
        """Ctrl+clicking file cards should toggle individual selection."""
        first_card = self.page.locator(".file-card").first
        expect(first_card).to_be_visible(timeout=5000)

        # Ctrl+click to select (enters selection mode)
        first_card.click(modifiers=["Control"])

        # Should enter selection mode and show the selection toolbar; expect() polls, so a
        # fixed wait here would be pure waste (issue #431).
        expect(self.page.locator(".select-all-btn")).to_be_visible(timeout=5000)

        # File should be marked as selected
        selected = self.page.locator(".file-card.selected")
        assert selected.count() >= 1, "At least one file should be selected"

    def test_shift_click_range_selection(self) -> None:
        """Shift+click should select a range of files."""
        cards = self.page.locator(".file-card")
        assert cards.count() >= 3, "Need at least 3 files for range selection test"

        # Ctrl+click first card to start selection
        cards.first.click(modifiers=["Control"])
        # Kept (issue #431): the shift+click below must land AFTER the anchor selection has
        # registered, or the range is computed from the wrong start card — an ordering
        # constraint no locator assertion expresses.
        self.page.wait_for_timeout(300)

        # Shift+click third card to select range
        cards.nth(2).click(modifiers=["Shift"])
        # Kept (issue #431): `selected.count()` below is a snapshot — no auto-wait.
        self.page.wait_for_timeout(300)

        # Should have at least 3 files selected
        selected = self.page.locator(".file-card.selected")
        assert selected.count() >= 3, (
            f"Range selection should select at least 3 files, got {selected.count()}"
        )

    def test_collections_button_opens_panel(self) -> None:
        """Collections button should open the collections panel/sidebar."""
        self.page.click(".collections-btn")
        # expect() below already polls, so a fixed wait here is pure waste (issue #431).
        # Collections panel should appear (varies by implementation)
        panel = self.page.locator(
            ".collections-panel, .collections-sidebar, [role=dialog], .modal-backdrop"
        )
        expect(panel.first).to_be_visible(timeout=5000)
