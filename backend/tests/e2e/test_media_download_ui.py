"""Browser-driven (Playwright) E2E tests for presigned media downloads.

Unlike test_media_download.py (which drives the API/SSE flow with `requests`),
these drive the real Svelte UI in Chromium: they click the file-detail download
dropdown and the gallery bulk-export menu and capture the actual browser download,
proving the presigned-URL flow works end to end through the front end.

Requires: ./opentr.sh start dev (frontend :5173, backend :5174) with at least one
completed media file in the dev dataset (admin@example.com / password).
"""

import os
import re
import zipfile

import pytest
import requests
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
    """UUID of a completed file in the dev dataset (one API login per module)."""
    tok = requests.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if tok.status_code != 200:
        pytest.skip(f"Cannot authenticate against dev stack (HTTP {tok.status_code})")
    files = requests.get(
        f"{backend_url}/api/files?limit=100",
        headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
        timeout=30,
    ).json()
    target = next((f for f in files.get("items", []) if f.get("status") == "completed"), None)
    if not target:
        pytest.skip("No completed media file in dev dataset — required for media UI E2E")
    assert target is not None
    return str(target["uuid"])


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
