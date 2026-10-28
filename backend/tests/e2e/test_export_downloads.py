"""E2E: every Export menu entry downloads a real file with real content (#1335).

``test_file_detail_transcript.py`` proves the menu LISTS the formats. This module clicks
each one and reads the bytes the browser received, so a format that opens a dialog and then
saves nothing, or saves an empty or mislabelled file, is caught. DOCX and PDF are checked
by magic number (``PK`` zip container, ``%PDF-``) and by the transcript text inside them.

Uses the suite's injected two-speaker transcript (``owned_diarized_transcript_file``), which
is read-only here: exporting changes no stored data.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any

import pytest
import requests
from playwright.sync_api import Download
from playwright.sync_api import Page
from playwright.sync_api import expect
from timeouts import APP_SHELL_READY_MS

pytestmark = pytest.mark.transcription

FIRST_LINE = "Point 1: let us settle"
SPEAKERS = ("Ada Vance", "Bo Ruiz")

#: (menu label, file extension, whether an options dialog stands between click and download)
FORMATS = [
    ("Plain Text (.txt)", "txt", True),
    ("JSON Format (.json)", "json", False),
    ("CSV Format (.csv)", "csv", False),
    ("SubRip Subtitles (.srt)", "srt", False),
    ("WebVTT Subtitles (.vtt)", "vtt", False),
    ("Markdown (.md)", "md", False),
    ("Word Document (.docx)", "docx", True),
    ("PDF Document (.pdf)", "pdf", True),
]


@pytest.fixture(scope="module")
def documents_enabled(backend_url: str, admin_token: str) -> bool:
    response = requests.get(
        f"{backend_url}/api/system/capabilities",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    assert response.status_code == 200, response.text[:200]
    return bool(response.json().get("capabilities", {}).get("exports.documents") is not False)


@pytest.fixture
def export_page(
    browser: Any, shared_auth_state: str, base_url: str, owned_diarized_transcript_file: dict
) -> Any:
    context = browser.new_context(
        storage_state=shared_auth_state,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
        accept_downloads=True,
    )
    page = context.new_page()
    page.goto(f"{base_url}/files/{owned_diarized_transcript_file['uuid']}")
    expect(page.locator(".transcript-segment").first).to_be_visible(timeout=APP_SHELL_READY_MS)
    yield page
    page.close()
    context.close()


def _download(page: Page, label: str, with_dialog: bool) -> tuple[str, bytes]:
    """Pick *label* in the Export menu and return (suggested filename, bytes received)."""
    page.locator(".export-transcript-button").click()
    entry = page.locator(".export-dropdown-content button", has_text=label)
    expect(entry).to_be_visible()
    with page.expect_download(timeout=120000) as pending:
        entry.click()
        if with_dialog:
            confirm = page.locator("button.export-confirm")
            expect(confirm).to_be_visible(timeout=15000)
            confirm.click()
    download: Download = pending.value
    path = download.path()
    assert path is not None
    with open(path, "rb") as handle:
        return download.suggested_filename, handle.read()


@pytest.mark.parametrize(("label", "ext", "with_dialog"), FORMATS, ids=[f[1] for f in FORMATS])
def test_each_export_format_downloads_real_content(
    label: str,
    ext: str,
    with_dialog: bool,
    export_page: Page,
    documents_enabled: bool,
    owned_diarized_transcript_file: dict,
) -> None:
    if ext in ("docx", "pdf"):
        # Deselect-by-capability is not possible for a parametrized id, so the document
        # formats assert the matching ABSENCE instead of silently doing nothing.
        if not documents_enabled:
            export_page.locator(".export-transcript-button").click()
            entries = export_page.locator(".export-dropdown-content button", has_text=label)
            expect(entries).to_have_count(0)
            return
    name, body = _download(export_page, label, with_dialog)

    assert name.endswith(f".{ext}"), name
    assert body, f"{ext} export is empty"
    if ext == "txt":
        text = body.decode("utf-8")
        assert FIRST_LINE in text
        assert any(speaker in text for speaker in SPEAKERS)
    elif ext == "json":
        data = json.loads(body.decode("utf-8"))
        assert FIRST_LINE in json.dumps(data)
    elif ext == "csv":
        lines = body.decode("utf-8-sig").splitlines()
        assert len(lines) > owned_diarized_transcript_file["segment_count"] // 2
        assert FIRST_LINE in "\n".join(lines)
    elif ext == "srt":
        text = body.decode("utf-8")
        assert re.search(r"^1\n\d\d:\d\d:\d\d,\d{3} --> ", text, re.M), text[:200]
        assert FIRST_LINE in text
    elif ext == "vtt":
        text = body.decode("utf-8")
        assert text.startswith("WEBVTT"), text[:40]
        assert FIRST_LINE in text
    elif ext == "md":
        text = body.decode("utf-8")
        headings = [line for line in text.splitlines() if line.startswith("# ")]
        assert headings, text[:300]
        assert owned_diarized_transcript_file["title"] in headings[0]
        assert FIRST_LINE in text
    elif ext == "docx":
        assert body[:2] == b"PK", body[:8]
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        assert FIRST_LINE in re.sub(r"<[^>]+>", "", document_xml)
    elif ext == "pdf":
        assert body.startswith(b"%PDF-"), body[:8]
        assert body.rstrip().endswith(b"%%EOF")
    else:  # pragma: no cover - the FORMATS table is the only source of ext
        pytest.fail(f"unhandled format {ext}")
