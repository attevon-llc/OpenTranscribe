"""Watch-source document finalization — the ``Document``-row counterpart to
``processing.py``'s ``MediaFile`` tail (#362 Stage 6e).

Split out rather than grown inline: ``processing.ingest_prepared_file`` does type
detection and the fingerprint/dedup steps shared by both media and documents, then
delegates here for anything document-specific. Mirrors the manual upload endpoint
(``api/endpoints/documents.py``) — same size gate, same storage-path shape, same
``documents.parse`` dispatch — so a file behaves identically whether it arrived by
watch-source scan or by hand.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812
from app.models.document import Document
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.services.documents import DOCUMENT_MIME_TYPES
from app.services.documents import LEGACY_MIME_TYPES
from app.services.documents import detect_document_mime
from app.services.imohash_service import compute_from_path
from app.services.system_settings_service import get_setting_int
from app.tasks.document_tasks import dispatch_document_parse
from app.utils.filename import sanitize_filename

_SUPPORTED_MIME_TYPES = frozenset(DOCUMENT_MIME_TYPES) | LEGACY_MIME_TYPES


def guess_document_mime(local_path: str, filename: str) -> str | None:
    """Best-effort document MIME from magic bytes, or ``None`` if not a supported format.

    Mirrors ``api/endpoints/documents.py``'s upload validation: read a header sample,
    let :func:`detect_document_mime` do magic-byte-first detection (falling back to
    the filename extension for the formats with no magic bytes), then reject anything
    outside the supported set rather than trusting an unrecognized MIME through.
    """
    with open(local_path, "rb") as fp:
        header = fp.read(512)
    mime = detect_document_mime(filename, header, None)
    return mime if mime in _SUPPORTED_MIME_TYPES else None


def finalize_document_ingest(
    db: Session,
    source: WatchSource,
    row: WatchSourceFile,
    local_path: str,
    *,
    filename: str,
    file_size: int,
    content_type: str,
) -> WatchSourceFile:
    """Size gate, ``Document`` row, MinIO upload, dispatch ``documents.parse``,
    finalize the tracking row. The document counterpart of
    ``processing._finalize_media_ingest``.
    """
    owner_id = int(source.user_id)

    max_bytes = get_setting_int(
        db, "documents.max_upload_bytes", C.DEFAULT_DOCUMENT_MAX_UPLOAD_BYTES
    )
    if file_size > max_bytes:
        row.status = "skipped_too_large"
        row.skip_reason = "too_large"
        row.processed_at = datetime.now(UTC)
        db.commit()
        return row

    # Tenant scope was captured on the source at CREATION time, same rule
    # media imports follow (issue #262c) — never inferred from the owner's
    # memberships at import time.
    organization_id = int(source.organization_id) if source.organization_id else None
    sanitized = sanitize_filename(filename)
    # imohash, computed from the local file already on disk — same fingerprint
    # convention as MediaFile.imohash / watch-source media dedup
    # (services/imohash_service.py), and the manual-upload endpoint's own
    # compute_from_stream call for the same column.
    file_hash = compute_from_path(local_path)
    doc = Document(
        user_id=owner_id,
        organization_id=organization_id,
        filename=sanitized,
        storage_path="",
        file_size=file_size,
        content_type=content_type,
        file_hash=file_hash,
    )
    db.add(doc)
    db.flush()
    db.refresh(doc)

    # Upload to MinIO. Same known residual session hold as the media path
    # (see processing.ingest_prepared_file's docstring): the object key is
    # derived from doc.id, so the row must exist first. Documents are capped
    # at documents.max_upload_bytes (256 MB default) rather than the 15 GB
    # media ceiling, so this window is short enough that the idle-in-
    # transaction exemption the media path takes is not needed here.
    storage_path = f"documents/user_{owner_id}/document_{doc.id}/{sanitized}"
    from app.services.minio_service import upload_file_tuned

    with open(local_path, "rb") as fp:
        upload_file_tuned(
            file_content=fp,
            file_size=file_size,
            object_name=storage_path,
            content_type=content_type,
        )
    doc.storage_path = storage_path

    # Finalize tracking row.
    row.status = "imported"
    row.document_id = int(doc.id)
    row.processed_at = datetime.now(UTC)
    source.total_files_imported = (source.total_files_imported or 0) + 1

    db.commit()
    db.refresh(doc)

    # Documents share auto_transcribe with media: it means "start the
    # pipeline without a human clicking anything," and a discovered document
    # is auto-parsed under the same toggle a discovered recording is
    # auto-transcribed under. See the migration docstring (v395) for why
    # there is no separate document-specific toggle.
    if source.auto_transcribe:
        dispatch_document_parse(doc.id)

    return row
