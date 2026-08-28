"""Browser-driven (Playwright) E2E tests for presigned media downloads.

Unlike test_media_download.py (which drives the API/SSE flow with `requests`),
these drive the real Svelte UI in Chromium: they click the file-detail download
dropdown and the gallery bulk-export menu and capture the actual browser download,
proving the presigned-URL flow works end to end through the front end.

Requires: ./opentr.sh start dev (frontend :5173, backend :5174). Creates and deletes
its own media — no pre-existing dev data required (admin@example.com / password).
"""

import os
import re
import uuid
import zipfile

import pytest
import requests
from conftest import COMMITTED_SAMPLE_MEDIA
from conftest import OWNED_MEDIA_PREFIX
from conftest import delete_media_file
from conftest import wait_for_stable_completion
from playwright.sync_api import Page

# This module used to define its own ``BACKEND_URL`` constant here. A module constant is
# evaluated at import time, so it could not see ``--backend-url`` and this file always
# talked to whatever was on the default port — even when the run was aimed at an isolated
# stack (issue #431). Everything below takes conftest's ``backend_url`` fixture instead
# (the browser side already used ``base_url``).
TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")
TEST_ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "password")


@pytest.fixture(scope="module")
def completed_uuid(backend_url: str):
    """UUID of a completed file this module OWNS, deleted on teardown.

    Previously scavenged the first `completed` file out of the account (one API
    login per module) — same race as `test_media_download.py::completed_file`: under
    the parallel suite this could grab another xdist worker's `e2e-owned-*` upload
    moments before that worker deleted it.
    """
    if not COMMITTED_SAMPLE_MEDIA.exists():  # pragma: no cover - the fixture is tracked
        pytest.skip(f"missing media fixture {COMMITTED_SAMPLE_MEDIA}")
    tok = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if tok.status_code != 200:
        pytest.skip(f"Cannot authenticate against dev stack (HTTP {tok.status_code})")
    token = str(tok.json()["access_token"])
    headers = {"Authorization": f"Bearer {token}"}
    # Dismiss FirstRunWizard (same pattern as test_visual_regression.py's api_token
    # fixture): it mounts unconditionally in the root layout and, on an account that
    # has never completed it — true of a brand-new empty/`--fresh` admin — opens a
    # BaseModal on first authenticated page load whose backdrop intercepts every
    # click, including this module's own `.download-button` / "Select Files".
    # Idempotent — safe even if already completed.
    requests.post(f"{backend_url}/api/admin/first-run-wizard/complete", headers=headers, timeout=10)
    name = f"{OWNED_MEDIA_PREFIX}{uuid.uuid4().hex[:8]}{COMMITTED_SAMPLE_MEDIA.suffix}"
    with COMMITTED_SAMPLE_MEDIA.open("rb") as fh:
        resp = requests.post(
            f"{backend_url}/api/files",
            headers=headers,
            files={"file": (name, fh, "audio/wav")},
            timeout=300,
        )
    assert resp.status_code == 200, f"Upload failed: {resp.status_code} {resp.text[:300]}"
    file_uuid = str(resp.json()["uuid"])
    try:
        status = wait_for_stable_completion(backend_url, token, file_uuid)
        assert status == "completed", f"uploaded fixture never completed (status={status})"
        yield file_uuid
    finally:
        delete_media_file(backend_url, token, file_uuid)


def test_file_detail_download_dropdown_downloads_audio(
    authenticated_page: Page, base_url: str, completed_uuid: str
):
    """Open a file's download dropdown, pick Audio — MP3, and capture the download."""
    page = authenticated_page
    page.goto(f"{base_url}/files/{completed_uuid}")

    # File detail can 401 on a post-login auth-init race; one reload recovers it.
    download_button = page.locator("button.download-button")
    try:
        download_button.wait_for(state="visible", timeout=15000)
    except Exception:
        page.reload()
        download_button.wait_for(state="visible", timeout=15000)

    download_button.click()

    # The dropdown delivers the file via a presigned URL (built async on the worker,
    # pushed over SSE, then triggered as an <a download>). Capture the browser download.
    with page.expect_download(timeout=120000) as dl_info:
        page.get_by_role("button", name="Audio — MP3").click()
    download = dl_info.value

    path = download.path()
    assert path is not None
    assert os.path.getsize(path) > 0
    assert download.suggested_filename  # browser received a filename


def test_gallery_bulk_export_downloads_zip(
    authenticated_page: Page, base_url: str, completed_uuid: str, tmp_path
):
    """Select files in the gallery, export subtitles, and capture the ZIP download."""
    page = authenticated_page
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    # Enter selection mode, then select every file to reveal the bulk-action toolbar.
    page.get_by_role("button", name="Select Files").first.click()
    page.get_by_role("button", name="Select all").first.click()

    # Open the "Organize" dropdown that contains the export options, then export SRT.
    page.get_by_role("button", name=re.compile("Organize")).first.click()
    with page.expect_download(timeout=120000) as dl_info:
        page.get_by_role("button", name="Export SRT").first.click()
    download = dl_info.value

    dest = tmp_path / "transcripts.zip"
    download.save_as(dest)
    assert dest.stat().st_size > 0
    assert zipfile.is_zipfile(dest)
