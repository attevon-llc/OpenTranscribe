"""
E2E tests for the media upload flow (issue #123 Phase 2).

Covers the upload stepper modal: opening, tab switching (file/url/record),
file selection/removal, client-side validation, and the full submit flow
(file lands in the gallery via the real upload pipeline, then is deleted
through the API so the dev environment stays clean).

Requirements:
- Dev environment running: ./opentr.sh start dev
- ffmpeg on the host (media fixtures are generated, never downloaded)

Run:
    pytest backend/tests/e2e/test_upload.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_upload.py -v --headed
"""

import os
import subprocess
import time

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.upload


@pytest.fixture
def upload_modal(gallery_page: Page) -> Page:
    """Open the upload stepper modal from the gallery and return the page.

    Uses the session-shared auth state (one login per run) so larger runs
    never trip the backend's login rate limiting.
    """
    gallery_page.wait_for_selector(".upload-btn", timeout=15000)
    gallery_page.click(".upload-btn")
    expect(
        gallery_page.locator("[role=dialog], .modal-backdrop, .upload-modal").first
    ).to_be_visible(timeout=5000)
    gallery_page.wait_for_selector(".tab-button", timeout=5000)
    return gallery_page


@pytest.fixture
def unique_audio(tmp_path):
    """A WAV with run-unique content so imohash dedup never flags a duplicate."""
    freq = 300 + int(time.time()) % 500
    out = tmp_path / f"e2e_upload_{freq}_{os.getpid()}.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration=1.5",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(out),
        ],
        check=True,
        timeout=60,
    )
    return out


class TestUploadModal:
    """The Add Media button opens the upload stepper with its three tabs."""

    def test_upload_button_opens_stepper(self, upload_modal: Page):
        """The stepper renders with a step indicator and tab navigation."""
        expect(upload_modal.locator(".uploader-container")).to_be_visible()
        expect(upload_modal.locator(".step-indicator")).to_be_visible()

    def test_stepper_shows_three_tabs(self, upload_modal: Page):
        """File, URL, and Record tabs are present."""
        tabs = upload_modal.locator(".tab-navigation .tab-button")
        expect(tabs).to_have_count(3)

    def test_switch_to_url_tab(self, upload_modal: Page):
        """Clicking the URL tab reveals the media URL input."""
        upload_modal.locator(".tab-navigation .tab-button").nth(1).click()
        expect(upload_modal.locator("#media-url")).to_be_visible(timeout=5000)

    def test_switch_back_to_file_tab(self, upload_modal: Page):
        """Returning to the file tab restores the drop zone."""
        upload_modal.locator(".tab-navigation .tab-button").nth(1).click()
        expect(upload_modal.locator("#media-url")).to_be_visible(timeout=5000)
        upload_modal.locator(".tab-navigation .tab-button").nth(0).click()
        expect(upload_modal.locator("#drop-zone")).to_be_visible(timeout=5000)


class TestFileSelection:
    """Selecting and removing files in the drop zone."""

    def test_select_file_shows_filename(self, upload_modal: Page, sample_audio):
        """Choosing a valid audio file shows its name and size."""
        upload_modal.set_input_files("#drop-zone input[type=file]", str(sample_audio))
        expect(upload_modal.locator(".selected-file .file-name")).to_have_text(
            sample_audio.name, timeout=10000
        )

    def test_remove_selected_file_restores_drop_zone(self, upload_modal: Page, sample_audio):
        """The remove button clears the selection."""
        upload_modal.set_input_files("#drop-zone input[type=file]", str(sample_audio))
        expect(upload_modal.locator(".selected-file")).to_be_visible(timeout=10000)
        upload_modal.click(".file-remove")
        expect(upload_modal.locator("#drop-zone")).to_be_visible(timeout=5000)

    def test_selecting_file_enables_next(self, upload_modal: Page, sample_audio):
        """The Next button unlocks once media is ready."""
        next_btn = upload_modal.locator(".nav-btn.nav-next")
        expect(next_btn).to_be_disabled()
        upload_modal.set_input_files("#drop-zone input[type=file]", str(sample_audio))
        expect(upload_modal.locator(".selected-file")).to_be_visible(timeout=10000)
        expect(next_btn).to_be_enabled(timeout=5000)

    def test_invalid_file_type_shows_error(self, upload_modal: Page, tmp_path):
        """A non-media file is rejected client-side with an error message.

        ``.exe`` rather than ``.txt``: it has no MIME mapping in any upload path,
        so it stays invalid regardless of what this modal
        (``MediaFilePanel``, ``accept="audio/*,video/*"``) grows to accept next.
        """
        bad_file = tmp_path / "not_media.exe"
        bad_file.write_bytes(b"MZ\x00\x00not a real executable, just bytes for upload rejection")
        upload_modal.set_input_files("#drop-zone input[type=file]", str(bad_file))
        expect(upload_modal.locator(".message.error-msg")).to_be_visible(timeout=5000)
        expect(upload_modal.locator(".selected-file")).to_have_count(0)


class TestVideoSelection:
    """Video files are accepted in the drop zone."""

    def test_select_video_shows_filename(self, upload_modal: Page, sample_video):
        upload_modal.set_input_files("#drop-zone input[type=file]", str(sample_video))
        expect(upload_modal.locator(".selected-file .file-name")).to_have_text(
            sample_video.name, timeout=10000
        )


class TestUrlUpload:
    """URL tab basics (no external network calls by default)."""

    def test_url_input_accepts_value(self, upload_modal: Page):
        """Typed URLs persist in the input."""
        upload_modal.locator(".tab-navigation .tab-button").nth(1).click()
        url_input = upload_modal.locator("#media-url")
        expect(url_input).to_be_visible(timeout=5000)
        url_input.fill("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        expect(url_input).to_have_value("https://www.youtube.com/watch?v=jNQXAC9IVRw")

    def test_url_next_disabled_without_url(self, upload_modal: Page):
        """Next stays disabled until a URL is processed into ready media."""
        upload_modal.locator(".tab-navigation .tab-button").nth(1).click()
        expect(upload_modal.locator("#media-url")).to_be_visible(timeout=5000)
        expect(upload_modal.locator(".nav-btn.nav-next")).to_be_disabled()


class TestFullUploadFlow:
    """End-to-end: select -> review with defaults -> submit -> file in gallery."""

    def test_submit_creates_file_and_cleans_up(self, upload_modal: Page, unique_audio, api_helper):
        """A submitted upload reaches the backend; teardown deletes it.

        The delete runs in a ``finally``. It used to run only on the happy path, after
        ``assert file_uuid``, which is the one assertion here that can fail *while the
        upload has already landed* — a slow pipeline pushing the file past the 30 s poll
        window left it permanently in the dev library, which is exactly what this suite is
        required never to do. The lookup therefore also moved inside the ``try``.
        """
        page = upload_modal
        filename = unique_audio.name

        # Step 1: choose the file
        page.set_input_files("#drop-zone input[type=file]", str(unique_audio))
        expect(page.locator(".selected-file .file-name")).to_have_text(filename, timeout=10000)
        page.click(".nav-btn.nav-next")

        # Steps 2..n: jump straight to review with defaults, then submit
        page.wait_for_selector(".nav-btn.nav-review-defaults", timeout=10000)
        page.click(".nav-btn.nav-review-defaults")
        submit_btn = page.locator(".nav-btn.nav-submit")
        expect(submit_btn).to_be_enabled(timeout=10000)
        submit_btn.click()

        login = api_helper.login("admin@example.com", "password")
        assert "access_token" in login, f"API login failed: {login}"

        file_uuid = None
        try:
            # Verify via API that the file was created.
            deadline = time.time() + 30
            while time.time() < deadline and file_uuid is None:
                listing = api_helper.get("/api/files?page=1&page_size=20&sort_by=upload_time")
                for item in listing.get("items", []):
                    if item.get("filename") == filename:
                        file_uuid = item["uuid"]
                        break
                if file_uuid is None:
                    # Kept (issue #431): verifying backend persistence directly via a polling
                    # API request rather than the gallery UI — no page locator reflects this
                    # state.
                    time.sleep(1)

            assert file_uuid, f"Uploaded file '{filename}' never appeared in /api/files"
        finally:
            if file_uuid is None:
                # The poll timed out but the upload may still be in flight — look the
                # file up one last time before giving up on removing it.
                file_uuid = self._find_uploaded_file(api_helper, filename)
            if file_uuid is not None:
                self._delete_uploaded_file(api_helper, file_uuid)

    @staticmethod
    def _find_uploaded_file(api_helper, filename: str) -> str | None:
        """Locate an upload by filename, tolerating a still-settling backend."""
        try:
            listing = api_helper.get("/api/files?page=1&page_size=50&sort_by=upload_time")
        except Exception:  # noqa: BLE001 - a lookup failure must not mask the test result
            return None
        for item in listing.get("items", []):
            if item.get("filename") == filename:
                return str(item["uuid"])
        return None

    @staticmethod
    def _delete_uploaded_file(api_helper, file_uuid: str) -> None:
        """Remove the test upload so dev data stays untouched.

        409 means the pipeline is still processing the clip — wait for it, then fall back
        to the admin force-delete endpoint.
        """
        status = None
        deadline = time.time() + 90
        while time.time() < deadline:
            status = api_helper.delete(f"/api/files/{file_uuid}")
            if status in (200, 204):
                return
            # Kept (issue #431): static helper polling the delete API on 409 (still
            # processing) — takes no page, no locator to wait on.
            time.sleep(3)
        status = api_helper.delete(f"/api/files/{file_uuid}/force")
        assert status in (200, 204), f"Cleanup delete failed with {status}"
