"""Document API — upload, list, detail, delete (#362 Stage 6d).

Direct multipart upload, not the presigned/TUS path ``files/upload.py`` uses for audio
and video: that machinery exists because a recording can be 15 GB, and
``documents.max_upload_bytes`` (256 MB default, ``core/constants.py``) makes a document
small enough that reading the whole thing into a spooled temp file and doing one
``upload_file_tuned`` PUT is the honest cost, not a corner cut. It still reuses the same
underlying storage call (``services/minio_service.upload_file_tuned``) rather than a
second storage abstraction.

Path params are UUIDs, never DB integers, matching every other resource in this API
(``app/api/CLAUDE.md``).
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import BinaryIO
from typing import cast
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import scope_to_context
from app.core import constants as C  # noqa: N812
from app.db.base import get_db
from app.models.document import Document
from app.models.document import DocumentChunk
from app.schemas.document import DocumentChunkListResponse
from app.schemas.document import DocumentChunkResponse
from app.schemas.document import DocumentDownloadResponse
from app.schemas.document import DocumentListResponse
from app.schemas.document import DocumentResponse
from app.schemas.document import display_status
from app.services.documents import DOCUMENT_MIME_TYPES
from app.services.documents import LEGACY_MIME_TYPES
from app.services.documents import detect_document_mime
from app.services.imohash_service import compute_from_stream
from app.services.minio_service import delete_file
from app.services.minio_service import get_presigned_download_url
from app.services.minio_service import upload_file_tuned
from app.services.system_settings_service import get_setting_int
from app.tasks.document_tasks import dispatch_document_parse
from app.utils.filename import sanitize_filename
from app.utils.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()

#: Everything the parser stack can name — the fast, upload-time rejection. The parse
#: task's own DocumentUnsupportedError is the backstop for anything this check misses
#: (a format-family match that a specific tier still declines), not a duplicate of it.
_SUPPORTED_MIME_TYPES = frozenset(DOCUMENT_MIME_TYPES) | LEGACY_MIME_TYPES


def _to_response(doc: Document) -> DocumentResponse:
    return DocumentResponse(
        uuid=doc.uuid,
        filename=doc.filename,
        file_size=doc.file_size,
        content_type=doc.content_type,
        status=doc.status.value if hasattr(doc.status, "value") else str(doc.status),
        display_status=display_status(
            doc.status.value if hasattr(doc.status, "value") else str(doc.status)
        ),
        error_category=doc.error_category,
        last_error_message=doc.last_error_message,
        parser=doc.parser,
        page_count=doc.page_count,
        word_count=doc.word_count,
        chunk_count=doc.chunk_count,
        language=doc.language,
        has_embedded_text=doc.has_embedded_text,
        ocr_applied=doc.ocr_applied,
        parse_warnings=list(doc.parse_warnings or []),
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        parsed_at=doc.parsed_at,
    )


def _get_owned_document(db: Session, document_uuid: UUID, ctx: RequestContext) -> Document:
    """404 (never 403) on a document that exists but is not reachable from *ctx*.

    Documents have no sharing/collections yet (v1 scope) — within one tenant scope every
    document is owner-only or admin-visible, so there is no "exists but you can't see it"
    case worth distinguishing from "does not exist"; the tag plane's `_writable_tag_ids`
    reasoning for 404-not-403 (`app/api/CLAUDE.md`) applies for the same reason: a 403
    confirms the id refers to something real, which is enumerable.

    The tenant check runs FIRST and applies to admins too: a global admin reviewing
    documents does so from within a tenant scope (personal, or a specific org context),
    never across one — matching org-admin surfaces elsewhere in this API
    (`require_org_admin`), rather than `get_file_by_uuid_with_permission`'s unconditional
    admin bypass, which documents deliberately do not inherit.
    """
    doc = get_by_uuid(db, Document, document_uuid, error_message="Document not found")
    in_scope = (
        doc.organization_id == ctx.org_id if ctx.is_org_context else doc.organization_id is None
    )
    if not in_scope:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    is_admin = ctx.user.role in ("admin", "super_admin")
    if doc.user_id != ctx.user.id and not is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    """Upload a document. Returns immediately with ``status: pending``; parsing and
    indexing happen in the background (``documents.parse`` -> ``documents.index``).
    """
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No file was uploaded")

    # Not a `with`: the spool's lifetime spans the `db.close()` release point below, and a
    # `with` is a compound statement the session-lifetime audit deliberately does not walk
    # into (a close it can't see is a close it can't credit) — see that script's own
    # comment on the interprocedural rule. Closed explicitly on every exit path instead.
    spooled = cast("BinaryIO", tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024))  # noqa: SIM115
    try:
        size = 0
        while chunk := await file.read(1024 * 1024):
            spooled.write(chunk)
            size += len(chunk)
        spooled.seek(0)

        max_bytes = get_setting_int(
            db, "documents.max_upload_bytes", C.DEFAULT_DOCUMENT_MAX_UPLOAD_BYTES
        )
        if size == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File is empty")
        if size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Document exceeds the maximum upload size of {max_bytes / (1024 * 1024):.0f} MB",
            )

        header = spooled.read(512)
        spooled.seek(0)
        mime = detect_document_mime(file.filename, header, None) or file.content_type
        if not mime or mime not in _SUPPORTED_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported document format: {file.content_type or 'unknown'}",
            )

        # imohash, not SHA-256 — same fingerprint convention as MediaFile.file_hash /
        # watch-source dedup (services/imohash_service.py), computed here rather than
        # deferred to a recompute pass because the whole spooled file is already
        # in hand. `compute_from_stream` seeks the stream itself; re-seek to 0
        # afterward so the MinIO PUT below still uploads from the start.
        file_hash = compute_from_stream(spooled, size)
        spooled.seek(0)

        sanitized = sanitize_filename(file.filename)
        doc = Document(
            user_id=ctx.user.id,
            organization_id=ctx.org_id,
            filename=sanitized,
            storage_path="",
            file_size=size,
            content_type=mime,
            file_hash=file_hash,
        )
        db.add(doc)
        # commit(), not flush(): flush() writes the INSERT inside the open transaction
        # but does not persist it, and db.close() below rolls back an uncommitted
        # transaction — silently discarding the row the later UPDATE then can't find
        # (sqlalchemy.orm.exc.StaleDataError, "expected to update 1 row; 0 matched").
        # Committing here matches files/upload.py's precedent one comment down: the row
        # exists (with an empty storage_path) before the slow storage write, and a
        # failed upload leaves a best-effort orphan rather than losing the insert.
        db.commit()
        db.refresh(doc)
    except Exception:
        db.rollback()
        spooled.close()
        raise

    storage_path = f"documents/user_{ctx.user.id}/document_{doc.id}/{sanitized}"
    # Release the connection before the MinIO PUT: a multi-second network call has no
    # business holding a request-scoped session open (the same read/close/slow/write shape
    # `app/tasks/CLAUDE.md` requires of Celery tasks, applied here to `Depends(get_db)`).
    db.close()

    try:
        # Matches files/upload.py's upload_file_to_storage: the test environment forces
        # SKIP_S3 so unit/API tests never need a real MinIO, and the row still gets a
        # storage_path even though no object exists — the delete path already tolerates
        # a missing object (best-effort purge).
        if os.environ.get("SKIP_S3", "False").lower() != "true":
            upload_file_tuned(
                file_content=spooled, file_size=size, object_name=storage_path, content_type=mime
            )
    finally:
        spooled.close()

    # Reopen: `db` auto-begins a fresh transaction on next use after `close()`. Nested in a
    # `try` on purpose — a bare top-level `db.add`/`db.refresh` here would re-arm the
    # audit's release credit for the *whole* function, retroactively flagging the upload
    # above as running with a transaction held (it didn't).
    try:
        doc.storage_path = storage_path
        db.add(doc)
        db.commit()
        db.refresh(doc)
    except Exception:
        db.rollback()
        raise

    dispatch_document_parse(doc.id)
    return _to_response(doc)


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    """List the documents reachable from the active tenant scope, newest first.

    Owner-scoped within that scope — see :func:`_get_owned_document` for why there is no
    sharing to account for yet. ``scope_to_context`` is the same default-deny helper every
    other org-aware listing in this API uses: org context -> the org's documents,
    otherwise the caller's own personal-scope documents.
    """
    query = scope_to_context(db.query(Document), Document, ctx)
    total = query.count()
    documents = query.order_by(Document.created_at.desc()).offset(skip).limit(limit).all()
    return DocumentListResponse(
        documents=[_to_response(d) for d in documents], total=total, skip=skip, limit=limit
    )


@router.get("/{document_uuid}", response_model=DocumentResponse)
async def get_document(
    document_uuid: UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    doc = _get_owned_document(db, document_uuid, ctx)
    return _to_response(doc)


@router.get("/{document_uuid}/chunks", response_model=DocumentChunkListResponse)
async def get_document_chunks(
    document_uuid: UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentChunkListResponse:
    """The document's chunk evidence, ordered — what a detail view cites from or jumps
    to (``page``/``char_start``/``char_end`` anchor a viewer, ``section_path`` labels a
    breadcrumb). Empty for a document that has not finished parsing yet, not an error.
    """
    doc = _get_owned_document(db, document_uuid, ctx)
    rows = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.document_id == doc.id)
        .order_by(DocumentChunk.chunk_index)
        .all()
    )
    chunks = [DocumentChunkResponse.model_validate(row) for row in rows]
    return DocumentChunkListResponse(chunks=chunks, total=len(chunks))


@router.get("/{document_uuid}/download", response_model=DocumentDownloadResponse)
async def get_document_download_url(
    document_uuid: UUID,
    download: bool = Query(
        False, description="Force a browser download instead of inline rendering."
    ),
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentDownloadResponse:
    """A presigned URL to the original file. ``download=false`` (default) is safe to
    point an ``<iframe>`` at for PDFs — no Content-Disposition override, so the browser
    renders it. ``download=true`` forces ``attachment``, for a format nothing in-browser
    can render (DOCX/PPTX/XLSX and friends).
    """
    doc = _get_owned_document(db, document_uuid, ctx)
    if not doc.storage_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document has no stored file"
        )
    storage_path, filename, content_type = doc.storage_path, doc.filename, doc.content_type
    db.close()  # release before the presign call — see upload_document's comment above
    url = get_presigned_download_url(
        storage_path,
        download_filename=filename if download else None,
        content_type=content_type,
    )
    return DocumentDownloadResponse(url=url, filename=filename, content_type=content_type)


@router.delete("/{document_uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_uuid: UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> None:
    """Delete a document: the row (chunks cascade), the OpenSearch documents, the
    storage object. Storage/index failures are logged, not raised — the row is gone
    either way, and an orphaned object/index entry is a cleanup-sweep concern, not a
    reason to tell the user their delete failed when it mostly succeeded.
    """
    doc = _get_owned_document(db, document_uuid, ctx)
    doc_uuid_str = str(doc.uuid)
    storage_path = doc.storage_path

    db.delete(doc)
    db.commit()

    try:
        from app.services.search.indexing_service import TranscriptIndexingService

        TranscriptIndexingService().delete_transcript_chunks(doc_uuid_str)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not remove OpenSearch documents for %s: %s", doc_uuid_str, exc)

    if storage_path:
        try:
            delete_file(storage_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not remove storage object %s: %s", storage_path, exc)
