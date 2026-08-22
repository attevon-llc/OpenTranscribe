"""API tests for the document endpoints (#362 Stage 6d).

``POST /api/documents`` performs a real upload-shaped round trip (spooled temp file,
mime detection, DB row, dispatch) with storage skipped under ``SKIP_S3`` — the same
convention ``files/upload.py``'s ``upload_file_to_storage`` uses, so these run in CI with
no MinIO. ``.delay()`` never reaches a real broker (autouse session fixture, conftest).
"""

from __future__ import annotations

import io
import os
import uuid
from unittest.mock import patch

import pytest
from fastapi import status

from app.core.security import get_password_hash
from app.models.document import Document

# The download endpoint presigns against the real object store, so these two
# tests need a reachable MinIO — same gate as test_files_upload.py.
S3_LIVE = os.environ.get("SKIP_S3", "True").lower() != "true"
from app.models.document import DocumentChunk
from app.models.user import User

_HTML_DOC = (
    b"<html><body><h1>Report</h1><p>Quarterly figures for the finance team.</p></body></html>"
)


@pytest.fixture
def other_user_token_headers(client, db_session):
    """A second, distinct user — for cross-owner 404 checks."""
    unique_id = uuid.uuid4().hex[:8]
    user = User(
        email=f"docs_other_{unique_id}@example.com",
        full_name="Other User",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    response = client.post(
        "/api/auth/token",
        data={"username": user.email, "password": "password123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _upload(client, headers, *, filename="report.html", data=_HTML_DOC, content_type="text/html"):
    return client.post(
        "/api/documents",
        headers=headers,
        files={"file": (filename, io.BytesIO(data), content_type)},
    )


def _make_document(db_session, owner, **overrides) -> Document:
    defaults = {
        "user_id": owner.id,
        "filename": "seed.html",
        "storage_path": f"documents/test/{uuid.uuid4().hex}.html",
        "file_size": 512,
        "content_type": "text/html",
    }
    defaults.update(overrides)
    doc = Document(**defaults)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


def test_upload_unauthorized(client):
    response = client.post(
        "/api/documents", files={"file": ("x.html", io.BytesIO(b"x"), "text/html")}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_list_unauthorized(client):
    assert client.get("/api/documents").status_code == status.HTTP_401_UNAUTHORIZED


def test_detail_unauthorized(client):
    assert client.get(f"/api/documents/{uuid.uuid4()}").status_code == status.HTTP_401_UNAUTHORIZED


def test_delete_unauthorized(client):
    assert (
        client.delete(f"/api/documents/{uuid.uuid4()}").status_code == status.HTTP_401_UNAUTHORIZED
    )


def test_chunks_unauthorized(client):
    assert (
        client.get(f"/api/documents/{uuid.uuid4()}/chunks").status_code
        == status.HTTP_401_UNAUTHORIZED
    )


def test_download_unauthorized(client):
    assert (
        client.get(f"/api/documents/{uuid.uuid4()}/download").status_code
        == status.HTTP_401_UNAUTHORIZED
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_upload_creates_a_pending_document_and_dispatches_parsing(
    client, user_token_headers, db_session
):
    response = _upload(client, user_token_headers)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    body = response.json()

    assert body["filename"] == "report.html"
    assert body["status"] == "pending"
    assert body["display_status"] == "Pending"
    assert body["content_type"] == "text/html"
    assert body["chunk_count"] == 0
    assert body["word_count"] == 0

    doc = db_session.query(Document).filter(Document.uuid == body["uuid"]).one()
    assert doc.filename == "report.html"
    assert doc.storage_path  # set even though SKIP_S3 means no real object exists


def test_upload_rejects_an_empty_file(client, user_token_headers):
    response = _upload(client, user_token_headers, data=b"")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_upload_rejects_an_unsupported_format(client, user_token_headers):
    response = _upload(
        client,
        user_token_headers,
        filename="mystery.bin",
        data=b"not a document, no magic bytes match anything at all",
        content_type="application/x-bogus-unsupported-format",
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_upload_rejects_no_filename(client, user_token_headers):
    """An empty filename isn't parsed as a file part at all — Starlette hands FastAPI a
    plain string where an ``UploadFile`` was declared, so this is a framework-level 422,
    the same shape ``test_files_upload.py::test_upload_no_file_422`` documents for a
    missing ``file`` field entirely.
    """
    response = client.post(
        "/api/documents",
        headers=user_token_headers,
        files={"file": ("", io.BytesIO(b"x"), "text/html")},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_is_empty_by_default(client, user_token_headers):
    response = client.get("/api/documents", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body == {"documents": [], "total": 0, "skip": 0, "limit": 50}


def test_list_only_shows_the_caller_s_own_documents(
    client, user_token_headers, other_user_token_headers, normal_user, db_session
):
    _make_document(db_session, normal_user, filename="mine.pdf")

    mine = client.get("/api/documents", headers=user_token_headers)
    assert mine.status_code == status.HTTP_200_OK
    body = mine.json()
    assert body["total"] == 1
    assert body["documents"][0]["filename"] == "mine.pdf"

    theirs = client.get("/api/documents", headers=other_user_token_headers)
    assert theirs.json() == {"documents": [], "total": 0, "skip": 0, "limit": 50}


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


def test_get_document_detail(client, user_token_headers, normal_user, db_session):
    doc = _make_document(db_session, normal_user, filename="quarterly.docx", word_count=120)
    response = client.get(f"/api/documents/{doc.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["filename"] == "quarterly.docx"
    assert response.json()["word_count"] == 120


def test_get_document_detail_404_for_unknown_uuid(client, user_token_headers):
    response = client.get(f"/api/documents/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_get_document_detail_404_for_someone_else_s_document(
    client, other_user_token_headers, normal_user, db_session
):
    """404, not 403 — see _get_owned_document's docstring: a 403 would confirm the id
    refers to something real, which is enumerable.
    """
    doc = _make_document(db_session, normal_user)
    response = client.get(f"/api/documents/{doc.uuid}", headers=other_user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


def _make_chunk(db_session, document, **overrides) -> DocumentChunk:
    defaults = {
        "document_id": document.id,
        "chunk_index": 0,
        "text": "Quarterly figures for the finance team.",
        "char_start": 0,
        "char_end": 39,
    }
    defaults.update(overrides)
    chunk = DocumentChunk(**defaults)
    db_session.add(chunk)
    db_session.commit()
    db_session.refresh(chunk)
    return chunk


def test_get_document_chunks(client, user_token_headers, normal_user, db_session):
    doc = _make_document(db_session, normal_user)
    _make_chunk(
        db_session, doc, chunk_index=0, text="First chunk.", char_start=0, char_end=12, page=1
    )
    _make_chunk(
        db_session, doc, chunk_index=1, text="Second chunk.", char_start=12, char_end=25, page=2
    )

    response = client.get(f"/api/documents/{doc.uuid}/chunks", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert [c["chunk_index"] for c in body["chunks"]] == [0, 1]
    assert body["chunks"][0]["text"] == "First chunk."
    assert body["chunks"][0]["page"] == 1
    assert body["chunks"][1]["char_start"] == 12


def test_get_document_chunks_is_empty_before_parsing(
    client, user_token_headers, normal_user, db_session
):
    doc = _make_document(db_session, normal_user)
    response = client.get(f"/api/documents/{doc.uuid}/chunks", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"chunks": [], "total": 0}


def test_get_document_chunks_404_for_someone_else_s_document(
    client, other_user_token_headers, normal_user, db_session
):
    doc = _make_document(db_session, normal_user)
    response = client.get(f"/api/documents/{doc.uuid}/chunks", headers=other_user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not S3_LIVE, reason="download URL presigns against MinIO (SKIP_S3=False)")
def test_get_document_download_url_defaults_to_inline(
    client, user_token_headers, normal_user, db_session
):
    doc = _make_document(
        db_session, normal_user, filename="report.pdf", content_type="application/pdf"
    )
    response = client.get(f"/api/documents/{doc.uuid}/download", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["filename"] == "report.pdf"
    assert body["content_type"] == "application/pdf"
    assert "response-content-disposition" not in body["url"]


@pytest.mark.skipif(not S3_LIVE, reason="download URL presigns against MinIO (SKIP_S3=False)")
def test_get_document_download_url_with_download_forces_attachment(
    client, user_token_headers, normal_user, db_session
):
    doc = _make_document(db_session, normal_user, filename="report.docx")
    response = client.get(
        f"/api/documents/{doc.uuid}/download?download=true", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    assert "attachment" in response.json()["url"]


def test_get_document_download_url_404_for_someone_else_s_document(
    client, other_user_token_headers, normal_user, db_session
):
    doc = _make_document(db_session, normal_user)
    response = client.get(f"/api/documents/{doc.uuid}/download", headers=other_user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_document(client, user_token_headers, normal_user, db_session):
    doc = _make_document(db_session, normal_user)
    doc_uuid = doc.uuid

    response = client.delete(f"/api/documents/{doc_uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT

    assert client.get(f"/api/documents/{doc_uuid}", headers=user_token_headers).status_code == (
        status.HTTP_404_NOT_FOUND
    )
    assert db_session.query(Document).filter(Document.uuid == doc_uuid).first() is None


def test_delete_someone_else_s_document_is_404(
    client, other_user_token_headers, normal_user, db_session
):
    doc = _make_document(db_session, normal_user)
    response = client.delete(f"/api/documents/{doc.uuid}", headers=other_user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    # And it must still exist, unmodified by the attempt.
    assert db_session.query(Document).filter(Document.uuid == doc.uuid).first() is not None


# ---------------------------------------------------------------------------
# List: sort / filter (v399, #362 lane C3)
# ---------------------------------------------------------------------------


def test_list_filters_by_status(client, user_token_headers, normal_user, db_session):
    _make_document(db_session, normal_user, filename="a.html", status="completed")
    _make_document(db_session, normal_user, filename="b.html", status="error")

    response = client.get("/api/documents?status=error", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["documents"][0]["filename"] == "b.html"


def test_list_search_matches_filename_case_insensitively(
    client, user_token_headers, normal_user, db_session
):
    _make_document(db_session, normal_user, filename="Quarterly-Report.html")
    _make_document(db_session, normal_user, filename="unrelated.html")

    response = client.get("/api/documents?search=quarterly", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["documents"][0]["filename"] == "Quarterly-Report.html"


def test_list_sorts_by_filename_ascending(client, user_token_headers, normal_user, db_session):
    _make_document(db_session, normal_user, filename="zeta.html")
    _make_document(db_session, normal_user, filename="alpha.html")

    response = client.get(
        "/api/documents?sort_by=filename&sort_order=asc", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    names = [d["filename"] for d in response.json()["documents"]]
    assert names == sorted(names)
    assert names[0] == "alpha.html"


def test_list_excludes_quarantined_documents_for_the_owner(
    client, user_token_headers, normal_user, db_session
):
    """A quarantined document must not appear in the owner's own listing."""
    doc = _make_document(db_session, normal_user, filename="taken-down.html")
    doc.is_quarantined = True
    db_session.commit()

    response = client.get("/api/documents", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 0


# ---------------------------------------------------------------------------
# Reparse (v399, #362 lane C3) — a failed parse was previously a dead end
# ---------------------------------------------------------------------------


def test_reparse_resets_status_and_dispatches(client, user_token_headers, normal_user, db_session):
    doc = _make_document(
        db_session,
        normal_user,
        status="error",
        error_category="parse_error",
        last_error_message="bad zip",
    )

    with patch("app.api.endpoints.documents.dispatch_document_parse") as mock_dispatch:
        response = client.post(f"/api/documents/{doc.uuid}/reparse", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "pending"
    assert body["error_category"] is None
    assert body["last_error_message"] is None
    mock_dispatch.assert_called_once_with(doc.id)

    db_session.refresh(doc)
    assert doc.status.value == "pending"


def test_reparse_someone_else_s_document_is_404(
    client, other_user_token_headers, normal_user, db_session
):
    doc = _make_document(db_session, normal_user, status="error")
    response = client.post(f"/api/documents/{doc.uuid}/reparse", headers=other_user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Admin quarantine / release (v399, #362 lane C4)
# ---------------------------------------------------------------------------


def test_quarantine_requires_admin(client, user_token_headers, normal_user, db_session):
    doc = _make_document(db_session, normal_user)
    response = client.post(
        f"/api/documents/{doc.uuid}/quarantine",
        headers=user_token_headers,
        json={"reason": "DMCA notice #1"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_admin_quarantine_then_release_round_trip(
    client, admin_token_headers, user_token_headers, normal_user, db_session
):
    doc = _make_document(db_session, normal_user, status="completed")

    quarantine_resp = client.post(
        f"/api/documents/{doc.uuid}/quarantine",
        headers=admin_token_headers,
        json={"reason": "DMCA notice #42", "legal_hold": True},
    )
    assert quarantine_resp.status_code == status.HTTP_200_OK
    body = quarantine_resp.json()
    assert body["is_quarantined"] is True
    assert body["legal_hold"] is True

    # Hidden from the owner (404, not a distinguishable "quarantined" response).
    owner_get = client.get(f"/api/documents/{doc.uuid}", headers=user_token_headers)
    assert owner_get.status_code == status.HTTP_404_NOT_FOUND
    # Still visible to an admin.
    admin_get = client.get(f"/api/documents/{doc.uuid}", headers=admin_token_headers)
    assert admin_get.status_code == status.HTTP_200_OK
    assert admin_get.json()["is_quarantined"] is True

    # Shows up in the admin review queue.
    queue = client.get("/api/documents/admin/quarantined", headers=admin_token_headers)
    assert queue.status_code == status.HTTP_200_OK
    assert queue.json()["total"] == 1
    assert queue.json()["documents"][0]["uuid"] == str(doc.uuid)

    release_resp = client.post(
        f"/api/documents/{doc.uuid}/release",
        headers=admin_token_headers,
        json={"clear_legal_hold": True},
    )
    assert release_resp.status_code == status.HTTP_200_OK
    release_body = release_resp.json()
    assert release_body["is_quarantined"] is False
    assert release_body["legal_hold"] is False

    # Restored to the owner's own listing.
    owner_get_after = client.get(f"/api/documents/{doc.uuid}", headers=user_token_headers)
    assert owner_get_after.status_code == status.HTTP_200_OK
    assert owner_get_after.json()["status"] == "completed"


def test_release_a_non_quarantined_document_is_409(
    client, admin_token_headers, normal_user, db_session
):
    doc = _make_document(db_session, normal_user)
    response = client.post(f"/api/documents/{doc.uuid}/release", headers=admin_token_headers)
    assert response.status_code == status.HTTP_409_CONFLICT


def test_list_quarantined_requires_admin(client, user_token_headers):
    response = client.get("/api/documents/admin/quarantined", headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
