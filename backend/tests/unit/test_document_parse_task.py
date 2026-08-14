"""``documents.parse`` — the Celery task that turns a stored upload into chunks (#362).

Real round trips against the dev stack's Postgres + MinIO (``SKIP_S3``, auto-detected by
conftest — see ``test_multipart_upload.py`` for the same gate). The task function is
called directly, bypassing the Celery broker, the same way this repo tests every other
task body (``speaker_attribute_task``'s ``_detect_speaker_attributes``,
``ingest_artifacts_task``'s inner call) — what is asserted is the DB/storage side effect,
not Celery's dispatch machinery.

``_patched_session_scope`` is required, not incidental: ``_parse_document`` opens its own
``session_scope()`` (by design — the whole point of the 3-phase split is that phase 1 and
phase 3 use SHORT, INDEPENDENT sessions). ``db_session`` isolates a test on a savepoint over
ONE connection; a task that opens a second, real connection via ``SessionLocal()`` cannot
see this test's uncommitted setup rows under READ COMMITTED. ``test_chat_endpoints.py``
documents the identical trap and the identical fix: monkeypatch the module's
``session_scope`` name to yield the test's own session instead of a fresh one, so every
phase of the task operates on the one connection this test controls.

Every test uploads under a ``tests/documents/`` MinIO prefix and deletes what it creates.
"""

from __future__ import annotations

import contextlib
import io
import os
import uuid as uuid_pkg

import pytest
from sqlalchemy import text

from app.models.document import Document
from app.models.document import DocumentChunk
from app.models.media import FileStatus

S3_LIVE = os.environ.get("SKIP_S3", "True").lower() != "true"

pytestmark = [
    pytest.mark.xdist_group("storage_backend"),
    pytest.mark.skipif(not S3_LIVE, reason="requires the dev stack's MinIO"),
]


@pytest.fixture(autouse=True)
def _patched_session_scope(monkeypatch, db_session):
    @contextlib.contextmanager
    def fake_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.tasks.document_tasks.session_scope", fake_scope)


_HTML_WITH_CONTENT = (
    b"<html><body><h1>Quarterly report</h1>"
    b"<p>Revenue grew by twelve percent this quarter, driven mainly by the new "
    b"logistics contracts signed in March.</p>"
    b"<table><tr><th>Region</th><th>Revenue</th></tr>"
    b"<tr><td>North</td><td>120000</td></tr>"
    b"<tr><td>South</td><td>98000</td></tr></table>"
    b"</body></html>"
)


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"docparse_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _object_name(suffix: str) -> str:
    return f"tests/documents/{uuid_pkg.uuid4().hex}_{suffix}"


def _upload(object_name: str, data: bytes, content_type: str) -> None:
    from app.services.minio_service import upload_file

    upload_file(io.BytesIO(data), len(data), object_name, content_type)


def _delete(object_name: str) -> None:
    from app.core.config import settings
    from app.services.minio_service import minio_client

    try:
        minio_client.remove_object(settings.MEDIA_BUCKET_NAME, object_name)
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


@pytest.fixture
def user_id(db_session):
    uid = _new_user(db_session.connection())
    db_session.commit()
    return uid


def _new_document(
    db_session,
    user_id: int,
    *,
    storage_path: str,
    content_type: str,
    filename: str = "report.html",
    file_size: int = len(_HTML_WITH_CONTENT),
) -> int:
    doc = Document(
        user_id=user_id,
        filename=filename,
        storage_path=storage_path,
        file_size=file_size,
        content_type=content_type,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return int(doc.id)


def test_a_document_with_real_content_parses_chunks_and_completes(db_session, user_id):
    from app.tasks.document_tasks import _parse_document

    object_name = _object_name("report.html")
    _upload(object_name, _HTML_WITH_CONTENT, "text/html")
    document_id = _new_document(
        db_session, user_id, storage_path=object_name, content_type="text/html"
    )

    try:
        result = _parse_document(document_id)
        assert result["status"] == "success", result

        db_session.expire_all()
        doc = db_session.get(Document, document_id)
        assert doc.status == FileStatus.COMPLETED
        assert doc.error_category is None
        assert doc.last_error_message is None
        assert doc.parser is not None
        assert doc.parse_version is not None
        assert doc.word_count > 0
        assert doc.chunk_count > 0
        assert doc.parsed_at is not None

        chunks = (
            db_session.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
            .all()
        )
        assert len(chunks) == doc.chunk_count
        assert any("twelve percent" in c.text for c in chunks), (
            "the paragraph's real text did not survive the parse"
        )
        assert any(c.char_end > c.char_start for c in chunks)
    finally:
        _delete(object_name)


def test_reparsing_the_same_document_does_not_duplicate_chunks(db_session, user_id):
    """The idempotency requirement from the handoff: a retried parse must not duplicate
    chunks. ``UniqueConstraint(document_id, chunk_index)`` is the backstop; the delete-
    then-insert in the write phase is the actual mechanism, and this exercises it twice.
    """
    from app.tasks.document_tasks import _parse_document

    object_name = _object_name("report.html")
    _upload(object_name, _HTML_WITH_CONTENT, "text/html")
    document_id = _new_document(
        db_session, user_id, storage_path=object_name, content_type="text/html"
    )

    try:
        first = _parse_document(document_id)
        assert first["status"] == "success"
        second = _parse_document(document_id)
        assert second["status"] == "success"
        assert first["chunks"] == second["chunks"]

        db_session.expire_all()
        count = (
            db_session.query(DocumentChunk).filter(DocumentChunk.document_id == document_id).count()
        )
        assert count == second["chunks"]
    finally:
        _delete(object_name)


def test_a_missing_document_row_returns_not_found_without_raising():
    from app.tasks.document_tasks import _parse_document

    result = _parse_document(document_id=-1)
    assert result == {"status": "error", "reason": "not_found"}


def test_a_document_whose_storage_object_is_gone_is_marked_error(db_session, user_id):
    from app.tasks.document_tasks import _parse_document

    object_name = _object_name("never_uploaded.html")
    document_id = _new_document(
        db_session, user_id, storage_path=object_name, content_type="text/html"
    )

    result = _parse_document(document_id)
    assert result == {"status": "error", "reason": "missing_object"}

    db_session.expire_all()
    doc = db_session.get(Document, document_id)
    assert doc.status == FileStatus.ERROR
    assert doc.error_category == "processing_error"
    assert doc.last_error_message


def test_an_unsupported_content_type_is_a_permanent_format_error(db_session, user_id):
    """No configured tier claims this mime **and** the bytes have no recognisable magic
    signature, so ``detect_document_mime`` falls through to the stored (bogus)
    content_type and ``get_parser_for`` raises ``DocumentUnsupportedError`` — a
    ``format_issue``, not a retryable ``processing_error``.
    """
    from app.tasks.document_tasks import _parse_document

    object_name = _object_name("mystery.bin")
    data = b"not a real document, no magic bytes match anything"
    _upload(object_name, data, "application/x-bogus-unsupported-format")
    document_id = _new_document(
        db_session,
        user_id,
        storage_path=object_name,
        content_type="application/x-bogus-unsupported-format",
        filename="mystery.bin",
        file_size=len(data),
    )

    try:
        result = _parse_document(document_id)
        assert result["status"] == "error"
        assert result["reason"] == "format_issue"

        db_session.expire_all()
        doc = db_session.get(Document, document_id)
        assert doc.status == FileStatus.ERROR
        assert doc.error_category == "format_issue"
    finally:
        _delete(object_name)
