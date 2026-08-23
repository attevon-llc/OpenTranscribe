"""Watch-source auto-import routes documents, not just media (#362 Stage 6e).

Real round trips against the dev stack's MinIO (``SKIP_S3``, same gate
``test_document_parse_task.py`` uses) — ``finalize_document_ingest`` uploads for real,
with no ``SKIP_S3`` short-circuit of its own (unlike the manual-upload API endpoint).
Every test uploads under ``tests/documents/`` and deletes what it creates.

``ingest_prepared_file`` is exercised directly (not through ``import_single_file`` /
Celery) — same convention ``test_document_parse_task.py`` uses for calling a task body
directly: what is asserted is the DB/storage side effect of the ingest logic, not the
scan scheduling or transfer machinery around it.
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg

import pytest

from app.models.document import Document
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.services.watch_sources import document_ingest
from app.services.watch_sources import processing

S3_LIVE = os.environ.get("SKIP_S3", "True").lower() != "true"

pytestmark = [
    pytest.mark.xdist_group("storage_backend"),
    pytest.mark.skipif(not S3_LIVE, reason="requires the dev stack's MinIO"),
]

_HTML_DOC = (
    b"<html><body><h1>Board minutes</h1>"
    b"<p>The committee approved the annual budget without amendment.</p>"
    b"</body></html>"
)


def _delete(object_name: str) -> None:
    from app.core.config import settings
    from app.services.minio_service import minio_client

    with contextlib.suppress(Exception):
        minio_client.remove_object(settings.MEDIA_BUCKET_NAME, object_name)


def _make_source(db_session, owner, **overrides) -> WatchSource:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "user_id": owner.id,
        "created_by": owner.id,
        "name": f"watch-{uuid_pkg.uuid4().hex[:8]}",
        "source_type": "local",
        "is_enabled": True,
        "local_path": ".",
        "auto_transcribe": True,
    }
    defaults.update(overrides)
    ws = WatchSource(**defaults)
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


def _make_row(db_session, source, **overrides) -> WatchSourceFile:
    defaults = {
        "uuid": uuid_pkg.uuid4(),
        "watch_source_id": source.id,
        "remote_path": f"/watch/{uuid_pkg.uuid4().hex}.html",
        "filename": "report.html",
        "status": "importing",
    }
    defaults.update(overrides)
    row = WatchSourceFile(**defaults)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# guess_document_mime
# ---------------------------------------------------------------------------


def test_guess_document_mime_detects_html(tmp_path):
    path = _write(tmp_path, "report.html", _HTML_DOC)
    assert document_ingest.guess_document_mime(str(path), "report.html") == "text/html"


def test_guess_document_mime_returns_none_for_unrecognized_bytes(tmp_path):
    path = _write(tmp_path, "mystery.bin", b"\x00\x01\x02\x03not a real document format at all")
    assert document_ingest.guess_document_mime(str(path), "mystery.bin") is None


# ---------------------------------------------------------------------------
# finalize_document_ingest
# ---------------------------------------------------------------------------


def test_finalize_document_ingest_creates_document_and_uploads(db_session, normal_user, tmp_path):
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source)
    path = _write(tmp_path, "report.html", _HTML_DOC)

    result = document_ingest.finalize_document_ingest(
        db_session,
        source,
        row,
        str(path),
        filename="report.html",
        file_size=len(_HTML_DOC),
        content_type="text/html",
    )

    try:
        assert result.status == "imported"
        assert result.document_id is not None

        doc = db_session.get(Document, result.document_id)
        assert doc.user_id == normal_user.id
        assert doc.filename == "report.html"
        assert doc.content_type == "text/html"
        assert doc.file_size == len(_HTML_DOC)
        assert doc.storage_path == f"documents/user_{normal_user.id}/document_{doc.id}/report.html"

        from app.core.config import settings
        from app.services.minio_service import minio_client

        stat = minio_client.stat_object(settings.MEDIA_BUCKET_NAME, doc.storage_path)
        assert stat.size == len(_HTML_DOC)
    finally:
        db_session.expire_all()
        doc = db_session.get(Document, result.document_id) if result.document_id else None
        if doc:
            _delete(str(doc.storage_path))


def test_finalize_document_ingest_rejects_a_file_over_the_size_cap(
    db_session, normal_user, tmp_path
):
    """The size gate reads ``file_size`` before touching the filesystem, so an
    oversized claim is rejected without a matching on-disk file or a real upload —
    the ``too_large`` skip must never reach ``open()``.
    """
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source)
    path = _write(tmp_path, "huge.html", _HTML_DOC)

    result = document_ingest.finalize_document_ingest(
        db_session,
        source,
        row,
        str(path),
        filename="huge.html",
        file_size=300 * 1024 * 1024,  # over the 256 MB default
        content_type="text/html",
    )

    assert result.status == "skipped_too_large"
    assert result.skip_reason == "too_large"
    assert result.document_id is None


def test_finalize_document_ingest_does_not_dispatch_parsing_when_auto_transcribe_is_off(
    db_session, normal_user, tmp_path, monkeypatch
):
    source = _make_source(db_session, normal_user, auto_transcribe=False)
    row = _make_row(db_session, source)
    path = _write(tmp_path, "report.html", _HTML_DOC)

    dispatched = []
    monkeypatch.setattr(
        "app.services.watch_sources.document_ingest.dispatch_document_parse",
        lambda document_id: dispatched.append(document_id),
    )

    result = document_ingest.finalize_document_ingest(
        db_session,
        source,
        row,
        str(path),
        filename="report.html",
        file_size=len(_HTML_DOC),
        content_type="text/html",
    )

    try:
        assert result.status == "imported"
        assert dispatched == []
    finally:
        db_session.expire_all()
        doc = db_session.get(Document, result.document_id)
        _delete(str(doc.storage_path))


# ---------------------------------------------------------------------------
# ingest_prepared_file — the type-detection branch
# ---------------------------------------------------------------------------


def test_ingest_prepared_file_routes_a_non_media_file_to_the_document_path(
    db_session, normal_user, tmp_path
):
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source)
    path = _write(tmp_path, "report.html", _HTML_DOC)

    result = processing.ingest_prepared_file(
        db_session, source, str(path), filename="report.html", row=row
    )

    try:
        assert result.status == "imported"
        assert result.document_id is not None
        assert result.media_file_id is None
    finally:
        db_session.expire_all()
        doc = db_session.get(Document, result.document_id)
        _delete(str(doc.storage_path))


def test_ingest_prepared_file_skips_a_file_that_is_neither_media_nor_document(
    db_session, normal_user, tmp_path
):
    source = _make_source(db_session, normal_user)
    row = _make_row(db_session, source, filename="mystery.bin")
    path = _write(tmp_path, "mystery.bin", b"\x00\x01\x02\x03garbage bytes, no signature")

    result = processing.ingest_prepared_file(
        db_session, source, str(path), filename="mystery.bin", row=row
    )

    assert result.status == "skipped_invalid"
    assert result.document_id is None
    assert result.media_file_id is None


def test_ingest_prepared_file_dedupes_a_document_against_another_source(
    db_session, normal_user, tmp_path, monkeypatch
):
    """Layer 2 (cross-source ``WatchSourceFile.imohash``) applies to documents just
    like media — the second source's identical file is skipped, not re-imported,
    and the skip carries the ORIGINAL document's id, not the media field.
    """
    other_doc = Document(
        user_id=normal_user.id,
        filename="report.html",
        storage_path="documents/user_x/document_x/report.html",
        file_size=len(_HTML_DOC),
        content_type="text/html",
    )
    db_session.add(other_doc)
    db_session.commit()
    db_session.refresh(other_doc)

    source_a = _make_source(db_session, normal_user)
    imported_row = _make_row(
        db_session,
        source_a,
        status="imported",
        document_id=other_doc.id,
        imohash="deadbeef" * 8,
    )
    assert imported_row.status == "imported"

    source_b = _make_source(db_session, normal_user)
    row_b = _make_row(db_session, source_b)
    path = _write(tmp_path, "report.html", _HTML_DOC)

    monkeypatch.setattr(
        "app.services.watch_sources.processing.compute_from_path", lambda _p: "deadbeef" * 8
    )
    result = processing.ingest_prepared_file(
        db_session, source_b, str(path), filename="report.html", row=row_b
    )

    assert result.status == "skipped_duplicate"
    assert result.document_id == other_doc.id
    assert result.media_file_id is None
