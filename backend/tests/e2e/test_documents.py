"""E2E smoke tests for the documents lane (issue #362): upload -> parse -> list -> detail.

The #362 documents feature (upload, parse via the torch-free slim in-process tier for
md/txt/csv/html, list, detail, share, quarantine) shipped with a full frontend and ZERO
browser coverage. This is a tight smoke suite, not exhaustive: it exercises the one path
every other document feature depends on being true — a real file, uploaded through the
real UI, ends up parsed and its extracted text is visible on the detail page.

The fixture ``.md`` file is chosen deliberately: the slim in-process tier
(``services/documents/backends/docling_slim.py``) handles ``.md``/``.txt``/``.csv`` with
**no sidecar**, so this suite passes on the plain dev stack (no ``--with-documents``
needed) and never touches docling-serve/Tika.

Requirements:
- Dev environment running: ./opentr.sh start dev

Run:
    pytest backend/tests/e2e/test_documents.py -v
    DISPLAY=:11 pytest backend/tests/e2e/test_documents.py -v --headed
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.documents


@pytest.fixture
def documents_page(gallery_page: Page, base_url: str) -> Page:
    """Navigate the pre-authenticated session to /documents."""
    gallery_page.goto(f"{base_url}/documents")
    gallery_page.wait_for_selector(".documents-page", timeout=15000)
    return gallery_page


@pytest.fixture
def unique_document(tmp_path: Path) -> tuple[Path, str]:
    """A small Markdown file with a run-unique name and a distinctive marker string.

    The marker (not just the filename) is what ``test_detail_renders_extracted_content``
    asserts is visible after parsing — proving the slim tier actually extracted THIS
    file's text, not stale content from a previous run.
    """
    marker = uuid4().hex[:12]
    name = f"e2e_doc_{marker}.md"
    path = tmp_path / name
    path.write_text(
        f"# E2E Test Document {marker}\n\n"
        f"This paragraph exists only to be parsed by an automated test and contains a "
        f"distinctive marker string: DOCMARKER-{marker}. It is uploaded, parsed, "
        "verified, and deleted within a single test run.\n\n"
        "## Second Section\n\n"
        "A second paragraph gives the slim parser more than one block to chunk.\n"
    )
    return path, marker


def _delete_document(api_helper, document_uuid: str | None) -> None:
    """Remove the test document via the API so dev data stays untouched.

    Not a happy-path delete: every caller runs this from a ``finally``, so it fires
    even when an assertion above it failed after the document was already created.
    """
    if document_uuid is None:
        return
    login = api_helper.login("admin@example.com", "password")
    assert "access_token" in login, f"API login failed: {login}"
    status = api_helper.delete(f"/api/documents/{document_uuid}")
    assert status in (200, 204), f"cleanup delete failed with {status}"


@pytest.fixture
def uploaded_document(documents_page: Page, unique_document: tuple[Path, str], api_helper):
    """Upload a document through the real UI flow and wait for it to reach 'Ready'.

    Yields ``(page, document_uuid, marker)``. Cleanup runs in a ``finally`` around the
    ``yield`` regardless of what the test asserts afterward — a happy-path delete does
    not count per backend/tests/CLAUDE.md's data-hygiene rule.
    """
    page = documents_page
    path, marker = unique_document
    document_uuid: str | None = None
    try:
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.split("?")[0].endswith("/api/documents")
        ) as response_info:
            page.set_input_files(".dropzone input[type=file]", str(path))
        response = response_info.value
        assert response.status == 201, (
            f"document upload failed: {response.status} {response.text()}"
        )
        document_uuid = response.json()["uuid"]

        card = page.locator(f'a.document-card[href="/documents/{document_uuid}"]')
        expect(card).to_be_visible(timeout=15000)
        expect(card.locator(".status-badge")).to_have_text("Ready", timeout=60000)

        yield page, document_uuid, marker
    finally:
        _delete_document(api_helper, document_uuid)


class TestDocumentUploadFlow:
    """Upload -> slim-tier parse -> appears completed in the documents list."""

    def test_upload_parses_and_lists_completed(self, uploaded_document):
        """The uploaded file's card shows the 'Ready' status once parsing finishes.

        Most of the behaviour under test already ran in the ``uploaded_document``
        fixture (upload, wait for 'Ready') — that IS the assertion this test exists to
        make; the class-and-content checks below are additional, cheap signal that the
        card is not just showing the right text by accident (e.g. a status color/CSS
        class regression that leaves the text right but the badge visually wrong).
        """
        page, document_uuid, _marker = uploaded_document
        card = page.locator(f'a.document-card[href="/documents/{document_uuid}"]')
        expect(card.locator(".status-badge")).to_have_class(re.compile(r"status-ready"))
        expect(card.locator(".card-filename")).to_be_visible()


class TestDocumentDetailView:
    """Opening a completed document's detail page renders its extracted text."""

    def test_detail_renders_extracted_content(self, uploaded_document, base_url: str):
        page, document_uuid, marker = uploaded_document

        page.goto(f"{base_url}/documents/{document_uuid}")
        page.wait_for_selector(".detail-page", timeout=15000)

        expect(page.locator(".header-main h1")).to_be_visible()
        expect(page.locator(".header-meta .status-badge")).to_have_text("Ready")

        # Default tab is 'text' (DocumentParsedTextViewer) for a completed document, so
        # no tab click is needed before the extracted chunks are on screen.
        expect(page.locator(".parsed-text")).to_be_visible(timeout=10000)
        expect(page.locator(".parsed-text")).to_contain_text(f"DOCMARKER-{marker}", timeout=15000)
