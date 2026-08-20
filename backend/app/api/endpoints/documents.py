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
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import BinaryIO
from typing import cast
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import UploadFile
from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import scope_to_context
from app.api.endpoints.auth import get_current_admin_user
from app.core import constants as C  # noqa: N812
from app.core.enums import FileStatus
from app.db.base import get_db
from app.models.document import Document
from app.models.document import DocumentChunk
from app.models.document import DocumentShare as DocumentShareModel
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.user import User
from app.schemas.document import DocumentChunkListResponse
from app.schemas.document import DocumentChunkResponse
from app.schemas.document import DocumentDownloadResponse
from app.schemas.document import DocumentListResponse
from app.schemas.document import DocumentQuarantineActionResponse
from app.schemas.document import DocumentQuarantineRequest
from app.schemas.document import DocumentReleaseRequest
from app.schemas.document import DocumentResponse
from app.schemas.document import DocumentShare as DocumentShareSchema
from app.schemas.document import DocumentShareCreate
from app.schemas.document import DocumentShareUpdate
from app.schemas.document import QuarantinedDocument
from app.schemas.document import QuarantinedDocumentsList
from app.schemas.document import display_status
from app.schemas.user import UserBrief
from app.services.documents import DOCUMENT_MIME_TYPES
from app.services.documents import LEGACY_MIME_TYPES
from app.services.documents import detect_document_mime
from app.services.imohash_service import compute_from_stream
from app.services.minio_service import delete_file
from app.services.minio_service import get_presigned_download_url
from app.services.minio_service import upload_file_tuned
from app.services.permission_service import PERMISSION_LEVELS
from app.services.permission_service import PermissionService
from app.services.system_settings_service import get_setting_int
from app.tasks.document_tasks import dispatch_document_parse
from app.tasks.search_indexing_task import update_document_access_index
from app.utils.filename import sanitize_filename
from app.utils.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()

#: Fallback for a NULL ``created_at`` — same sentinel media_collections.py uses.
_UNKNOWN_TIMESTAMP = datetime(1970, 1, 1, tzinfo=UTC)

#: Everything the parser stack can name — the fast, upload-time rejection. The parse
#: task's own DocumentUnsupportedError is the backstop for anything this check misses
#: (a format-family match that a specific tier still declines), not a duplicate of it.
_SUPPORTED_MIME_TYPES = frozenset(DOCUMENT_MIME_TYPES) | LEGACY_MIME_TYPES


def _to_response(doc: Document, *, my_permission: str | None = None) -> DocumentResponse:
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
        is_quarantined=bool(doc.is_quarantined),
        legal_hold=bool(doc.legal_hold),
        my_permission=my_permission,
    )


def _document_my_permission(db: Session, doc: Document, ctx: RequestContext) -> str | None:
    """Caller's effective permission on *doc*, in ``files/crud.py``'s ``None``-means-owner
    convention. Only called from endpoints reachable after ``_get_owned_document`` has
    already proven access, so a ``None`` return from ``PermissionService`` here (the
    "stranger" case) cannot occur in practice — it is defensive, not a real branch.
    """
    if doc.user_id == ctx.user.id:
        return None
    if ctx.user.role in ("admin", "super_admin"):
        return "owner"
    return PermissionService.get_document_permission(
        db, doc.id, ctx.user.id, organization_id=ctx.org_id
    )


def _get_owned_document(
    db: Session, document_uuid: UUID, ctx: RequestContext, *, min_permission: str = "viewer"
) -> Document:
    """404 (never 403) on a document that exists but is not reachable from *ctx*.

    v400 (#362 lane C3-remainder) widened this from owner-or-admin-only to also admit a
    ``DocumentShare`` sharee at or above *min_permission* — read endpoints (detail,
    chunks, download) ask for the default ``viewer``; owner-sensitive endpoints (delete,
    reparse, share management) ask for ``owner`` explicitly. The tag plane's
    `_writable_tag_ids` reasoning for 404-not-403 (`app/api/CLAUDE.md`) still applies: a
    403 confirms the id refers to something real, which is enumerable, so an
    insufficient-permission sharee gets the same 404 as a stranger.

    The tenant check runs FIRST and applies to admins too: a global admin reviewing
    documents does so from within a tenant scope (personal, or a specific org context),
    never across one — matching org-admin surfaces elsewhere in this API
    (`require_org_admin`), rather than `get_file_by_uuid_with_permission`'s unconditional
    admin bypass, which documents deliberately do not inherit.

    A quarantined document (v399) is hidden from every non-admin the same way
    ``takedown_service.is_hidden_for`` hides a taken-down ``MediaFile`` — 404, not a
    distinguishable "this exists but is quarantined" response, so a non-admin cannot
    even confirm a takedown happened by probing the id. Admins pass through so they can
    review it (matches ``GET /documents/admin/quarantined`` being the only listing that
    shows it at all).
    """
    doc = get_by_uuid(db, Document, document_uuid, error_message="Document not found")
    in_scope = (
        doc.organization_id == ctx.org_id if ctx.is_org_context else doc.organization_id is None
    )
    if not in_scope:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    is_admin = ctx.user.role in ("admin", "super_admin")
    if doc.user_id != ctx.user.id and not is_admin:
        permission = PermissionService.get_document_permission(
            db, doc.id, ctx.user.id, organization_id=ctx.org_id
        )
        if permission is None or PERMISSION_LEVELS[permission] < PERMISSION_LEVELS[min_permission]:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if bool(doc.is_quarantined) and not is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


#: `GET /documents` sort fields — mirrors `files/__init__.py::list_media_files`'s
#: `sort_field_mapping` convention (own dict local to the endpoint, not a shared module,
#: since the two field sets do not overlap: a document has no `duration`). Typed `Any`
#: rather than the precise `InstrumentedAttribute` union — the values are a mix of
#: `Mapped[str]`/`Mapped[int]`/`Mapped[datetime | None]` columns, and the sort call
#: below only needs `.asc()`/`.desc()`, which every column expression provides.
_SORT_FIELDS: dict[str, Any] = {
    "created_at": Document.created_at,
    "filename": Document.filename,
    "file_size": Document.file_size,
    "word_count": Document.word_count,
    "parsed_at": Document.parsed_at,
}


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
    search: str | None = Query(None, description="Case-insensitive filename search"),
    status: list[str] | None = Query(  # noqa: A002 - matches list_media_files' own shadow
        None, description="Filter by status: pending, processing, completed, error"
    ),
    sort_by: str = Query(
        "created_at",
        description="Field to sort by: created_at, filename, file_size, word_count, parsed_at",
    ),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    """List the documents reachable from the active tenant scope.

    In an org context this stays ``scope_to_context``'s plain org-wide listing (every org
    member already sees every org document, same as every other org-aware listing in this
    API). In personal scope (v400, #362 lane C3-remainder) this now also includes
    documents shared directly or via a group — the personal-scope half of
    ``scope_to_context`` was ``user_id == ctx.user.id`` only, which is exactly the "no
    sharing to account for" gap :func:`_get_owned_document` used to have too. Quarantined
    documents (v399) are excluded here for every caller, admin included — the dedicated
    review queue (``GET /documents/admin/quarantined``) is the only place they are ever
    listed, matching ``exclude_quarantined``'s media-gallery precedent of excluding on
    this surface and showing them only on the admin queue.

    ``skip``/``limit`` stay unbounded on ``skip`` (the pre-existing shape) — the "hard
    200 cap" a document past position 200 was invisible behind was never the backend's
    ceiling on ONE page (already raisable via ``skip``); it was the frontend never
    asking for a second page at all. See ``routes/documents/+page.svelte``.
    """
    if ctx.is_org_context:
        query = scope_to_context(db.query(Document), Document, ctx)
    else:
        accessible = PermissionService.get_accessible_document_ids_subquery(
            db, ctx.user.id, organization_id=ctx.org_id
        )
        query = db.query(Document).filter(
            Document.organization_id.is_(None), Document.id.in_(select(accessible))
        )
    query = query.filter(Document.is_quarantined.is_(False))

    if search:
        query = query.filter(Document.filename.ilike(f"%{search}%"))
    if status:
        query = query.filter(Document.status.in_(status))

    total = query.count()

    sort_field = _SORT_FIELDS.get(sort_by, Document.created_at)
    query = query.order_by(sort_field.asc() if sort_order.lower() == "asc" else sort_field.desc())

    documents = query.offset(skip).limit(limit).all()
    return DocumentListResponse(
        documents=[_to_response(d) for d in documents], total=total, skip=skip, limit=limit
    )


# =============================================================================
# ADMIN: abuse/DMCA takedown (v399, #362 lane C4) — the document counterpart of
# admin.py's media quarantine trio. MUST be registered before `/{document_uuid}` below:
# a literal path segment ("admin") has to win the route-matching order, or it is parsed
# as an attempted UUID and 422s instead of matching this router (app/api/CLAUDE.md's
# "static routes before /{id} routes" rule, from the tags-package precedent).
# =============================================================================


@router.get("/admin/quarantined", response_model=QuarantinedDocumentsList)
def list_quarantined_documents(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> QuarantinedDocumentsList:
    """List taken-down documents for admin review — the only surface that shows them
    at all, since :func:`list_documents` excludes them for every caller, admin included.
    """
    base = db.query(Document).filter(Document.is_quarantined.is_(True))
    total = base.count()
    rows = (
        base.order_by(Document.quarantined_at.desc().nullslast()).offset(offset).limit(limit).all()
    )
    documents = [
        QuarantinedDocument(
            uuid=d.uuid,
            filename=d.filename,
            quarantine_reason=d.quarantine_reason,
            quarantined_at=d.quarantined_at.isoformat() if d.quarantined_at else None,
            legal_hold=bool(d.legal_hold),
        )
        for d in rows
    ]
    return QuarantinedDocumentsList(documents=documents, total=total)


@router.post("/{document_uuid}/quarantine", response_model=DocumentQuarantineActionResponse)
def quarantine_document(
    document_uuid: UUID,
    request_body: DocumentQuarantineRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> DocumentQuarantineActionResponse:
    """Take a document down (abuse/DMCA). Hides it from every read surface for
    non-admins; the row, chunks, and storage object are NOT deleted — reversible via
    ``/release``, matching ``admin.py``'s media takedown exactly.
    """
    doc = get_by_uuid(db, Document, document_uuid, error_message="Document not found")
    doc.is_quarantined = True
    doc.quarantine_reason = request_body.reason
    doc.quarantined_at = datetime.now(UTC)
    doc.quarantined_by = current_user.id
    if doc.status != FileStatus.ERROR:
        doc.pre_quarantine_status = (
            doc.status.value if hasattr(doc.status, "value") else str(doc.status)
        )
    if request_body.legal_hold:
        doc.legal_hold = True
    db.commit()
    db.refresh(doc)

    if request_body.legal_hold and doc.storage_path:
        try:
            from app.services.minio_service import set_object_legal_hold

            set_object_legal_hold(str(doc.storage_path), True)
        except Exception as exc:  # noqa: BLE001 — advisory; never break the takedown
            logger.warning("Storage legal-hold enable failed for document %s: %s", doc.id, exc)

    logger.info(
        "Document %s (%s) quarantined by admin %s: %s",
        doc.id,
        doc.uuid,
        current_user.id,
        request_body.reason,
    )
    return DocumentQuarantineActionResponse(
        uuid=doc.uuid,
        is_quarantined=bool(doc.is_quarantined),
        legal_hold=bool(doc.legal_hold),
        status=str(doc.status.value if hasattr(doc.status, "value") else doc.status),
    )


@router.post("/{document_uuid}/release", response_model=DocumentQuarantineActionResponse)
def release_document(
    document_uuid: UUID,
    request: Request,
    request_body: DocumentReleaseRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> DocumentQuarantineActionResponse:
    """Release a quarantined document: restore access, optionally lift the legal-hold."""
    doc = get_by_uuid(db, Document, document_uuid, error_message="Document not found")
    if not doc.is_quarantined:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Document is not quarantined"
        )
    clear_hold = request_body.clear_legal_hold if request_body is not None else True

    doc.is_quarantined = False
    doc.quarantine_reason = None
    doc.quarantined_at = None
    doc.quarantined_by = None
    if doc.pre_quarantine_status:
        try:
            doc.status = FileStatus(doc.pre_quarantine_status)
        except ValueError:
            doc.status = FileStatus.COMPLETED
    doc.pre_quarantine_status = None
    if clear_hold:
        doc.legal_hold = False
    db.commit()
    db.refresh(doc)

    if clear_hold and doc.storage_path:
        try:
            from app.services.minio_service import set_object_legal_hold

            set_object_legal_hold(str(doc.storage_path), False)
        except Exception as exc:  # noqa: BLE001 — advisory; never break the release
            logger.warning("Storage legal-hold disable failed for document %s: %s", doc.id, exc)

    logger.info(
        "Document %s (%s) released from quarantine by admin %s", doc.id, doc.uuid, current_user.id
    )
    return DocumentQuarantineActionResponse(
        uuid=doc.uuid,
        is_quarantined=bool(doc.is_quarantined),
        legal_hold=bool(doc.legal_hold),
        status=str(doc.status.value if hasattr(doc.status, "value") else doc.status),
    )


@router.get("/{document_uuid}", response_model=DocumentResponse)
async def get_document(
    document_uuid: UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    doc = _get_owned_document(db, document_uuid, ctx)
    return _to_response(doc, my_permission=_document_my_permission(db, doc, ctx))


@router.get("/{document_uuid}/chunks", response_model=DocumentChunkListResponse)
async def get_document_chunks(
    document_uuid: UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentChunkListResponse:
    """The document's chunk evidence, ordered — what a detail view cites from or jumps
    to (``page``/``char_start``/``char_end`` anchor a viewer, ``section_path`` labels a
    breadcrumb). Empty for a document that has not finished parsing yet, not an error.

    v400 (#362 lane C5): masks each chunk's ``text`` at read time using its cached
    ``redactions`` spans (v396), the same read-time-transform contract
    ``services/redaction/spans.apply_redactions`` gives ``TranscriptSegment.text`` —
    this endpoint previously served every chunk's text raw regardless of the caller's
    redaction policy, the exact "unmasked read surface" root ``CLAUDE.md``'s retrieval
    trap warns about for a different plane. Fails closed (503) if the policy cannot be
    resolved, and withholds (409) while the file's scan is still pending — mirroring
    ``files/crud.py``'s transcript-read contract, minus that endpoint's owner
    ``?redact=false`` reveal (not yet built for documents; every category the caller's
    policy enables stays masked here with no bypass).
    """
    doc = _get_owned_document(db, document_uuid, ctx)
    rows = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.document_id == doc.id)
        .order_by(DocumentChunk.chunk_index)
        .all()
    )

    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.export_policy import export_masking_is_pending
    from app.services.redaction.spans import apply_redactions

    try:
        cfg = resolve_effective_config(db, ctx.user.id)
    except Exception as exc:  # noqa: BLE001 — fail closed, see docstring
        logger.exception(
            "Failed to resolve redaction config for document %s; refusing the chunk read", doc.id
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redaction policy is temporarily unavailable; document text withheld.",
        ) from exc

    if export_masking_is_pending(cfg, doc.redaction_status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document redaction scan is still in progress; chunks withheld.",
        )

    chunks = []
    for row in rows:
        chunk_response = DocumentChunkResponse.model_validate(row)
        if cfg.enabled and row.redactions:
            masked_text, _ = apply_redactions(
                row.text, row.redactions, style=cfg.style, enabled_categories=cfg.enabled_categories
            )
            chunk_response.text = masked_text
        chunks.append(chunk_response)
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


@router.post("/{document_uuid}/reparse", response_model=DocumentResponse)
async def reparse_document(
    document_uuid: UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    """Retry parsing a failed (or stuck) document — owner or admin.

    A failed parse was previously a dead end: no button, no endpoint, nothing short of
    re-uploading the file under a new row. This resets the document back to PENDING
    (clearing the prior error) and re-dispatches ``documents.parse`` exactly the way
    the original upload did — same task, same idempotent delete-then-insert chunk
    write, so a reparse is safe to run any number of times. Not restricted to
    ``error`` status: a document wedged in PROCESSING with no live task (stuck-task
    recovery has no document-specific arm yet, see ``app/tasks/CLAUDE.md``) can also
    be kicked back to PENDING from here. Requires ``editor``+ (owner, admin, or an
    editor-permission sharee) — a viewer-permission sharee cannot trigger reprocessing.
    """
    doc = _get_owned_document(db, document_uuid, ctx, min_permission="editor")
    doc.status = FileStatus.PENDING
    doc.error_category = None
    doc.last_error_message = None
    db.commit()
    db.refresh(doc)

    dispatch_document_parse(doc.id)
    logger.info("Document %s (%s) reparse dispatched by user %s", doc.id, doc.uuid, ctx.user.id)
    return _to_response(doc)


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

    ``owner``-only (or admin) — matches the collection model, where only the owner
    deletes the collection regardless of an editor share's write permission. Deleting
    also removes every ``document_share`` row via the FK's ``ON DELETE CASCADE``.
    """
    doc = _get_owned_document(db, document_uuid, ctx, min_permission="owner")
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


# =============================================================================
# Sharing (v400, #362 lane C3-remainder) — the direct-share counterpart of
# media_collections.py's collection-share trio, re-scoped to one document at a
# time since a document has no collection concept. Every mutation dispatches
# update_document_access_index so the OpenSearch accessible_user_ids grant list
# (services/search/indexing_service.index_document_chunks) stays in step —
# without it a granted share would never be retrievable via search or chat.
# =============================================================================


def _build_document_share_response(db: Session, share: DocumentShareModel) -> DocumentShareSchema:
    """Build a DocumentShare response — mirrors media_collections._build_share_response."""
    shared_by_brief = UserBrief(
        uuid=share.shared_by.uuid,
        full_name=share.shared_by.full_name,
        email=share.shared_by.email,
    )

    if share.target_type == "user" and share.target_user:
        target_uuid = share.target_user.uuid
        target_name = share.target_user.full_name or share.target_user.email
        target_email = share.target_user.email
        member_count = None
    elif share.target_type == "group" and share.target_group:
        target_uuid = share.target_group.uuid
        target_name = share.target_group.name
        target_email = None
        member_count = (
            db.query(UserGroupMember)
            .filter(UserGroupMember.group_id == share.target_group.id)
            .count()
        )
    else:
        raise HTTPException(status_code=500, detail="Invalid share target")

    return DocumentShareSchema(
        uuid=share.uuid,
        target_type=share.target_type,
        target_uuid=target_uuid,
        target_name=target_name,
        target_email=target_email,
        member_count=member_count,
        permission=share.permission,
        shared_by=shared_by_brief,
        created_at=share.created_at or _UNKNOWN_TIMESTAMP,
    )


@router.get("/{document_uuid}/shares", response_model=list[DocumentShareSchema])
def list_document_shares(
    document_uuid: UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> list[DocumentShareSchema]:
    """List all shares on a document. Requires direct ownership (or admin)."""
    doc = _get_owned_document(db, document_uuid, ctx, min_permission="owner")
    shares = (
        db.query(DocumentShareModel)
        .options(
            joinedload(DocumentShareModel.shared_by),
            joinedload(DocumentShareModel.target_user),
            joinedload(DocumentShareModel.target_group),
        )
        .filter(DocumentShareModel.document_id == doc.id)
        .order_by(DocumentShareModel.created_at.desc())
        .all()
    )
    return [_build_document_share_response(db, share) for share in shares]


@router.post(
    "/{document_uuid}/shares",
    response_model=DocumentShareSchema,
    status_code=status.HTTP_201_CREATED,
)
def create_document_share(
    document_uuid: UUID,
    share_in: DocumentShareCreate,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentShareSchema:
    """Share a document with a user or group. Requires direct ownership (or admin)."""
    doc = _get_owned_document(db, document_uuid, ctx, min_permission="owner")

    target_user_id: int | None = None
    target_group_id: int | None = None

    if share_in.target_type == "user":
        target_user = get_by_uuid(db, User, str(share_in.target_uuid), "User not found")
        if target_user.id == ctx.user.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot share a document with yourself",
            )
        if doc.organization_id is not None:
            from app.models.organization import OrganizationMembership

            membership = (
                db.query(OrganizationMembership)
                .filter(
                    OrganizationMembership.organization_id == doc.organization_id,
                    OrganizationMembership.user_id == target_user.id,
                )
                .first()
            )
            if membership is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Cannot share an organization document with a user who is "
                        "not a member of that organization"
                    ),
                )
        existing = (
            db.query(DocumentShareModel)
            .filter(
                DocumentShareModel.document_id == doc.id,
                DocumentShareModel.target_user_id == target_user.id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Document is already shared with this user",
            )
        target_user_id = target_user.id
    else:
        target_group = get_by_uuid(db, UserGroup, str(share_in.target_uuid), "Group not found")
        is_member = (
            db.query(UserGroupMember)
            .filter(
                UserGroupMember.group_id == target_group.id,
                UserGroupMember.user_id == ctx.user.id,
            )
            .first()
        )
        if not is_member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You must be a member of the group to share with it",
            )
        existing = (
            db.query(DocumentShareModel)
            .filter(
                DocumentShareModel.document_id == doc.id,
                DocumentShareModel.target_group_id == target_group.id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Document is already shared with this group",
            )
        target_group_id = target_group.id

    share = DocumentShareModel(
        document_id=doc.id,
        shared_by_id=ctx.user.id,
        target_type=share_in.target_type,
        target_user_id=target_user_id,
        target_group_id=target_group_id,
        permission=share_in.permission,
    )
    db.add(share)
    db.commit()
    db.refresh(share)

    reloaded = (
        db.query(DocumentShareModel)
        .options(
            joinedload(DocumentShareModel.shared_by),
            joinedload(DocumentShareModel.target_user),
            joinedload(DocumentShareModel.target_group),
        )
        .filter(DocumentShareModel.id == share.id)
        .first()
    )
    assert reloaded is not None  # just committed above

    update_document_access_index.delay([doc.id])
    logger.info(
        "Document %s (%s) shared by user %s: target_type=%s permission=%s",
        doc.id,
        doc.uuid,
        ctx.user.id,
        share_in.target_type,
        share_in.permission,
    )
    return _build_document_share_response(db, reloaded)


@router.put("/{document_uuid}/shares/{share_uuid}", response_model=DocumentShareSchema)
def update_document_share(
    document_uuid: UUID,
    share_uuid: UUID,
    share_update: DocumentShareUpdate,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> DocumentShareSchema:
    """Update a share's permission level. Requires direct ownership (or admin)."""
    doc = _get_owned_document(db, document_uuid, ctx, min_permission="owner")
    share = get_by_uuid(db, DocumentShareModel, str(share_uuid), "Share not found")
    if share.document_id != doc.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")

    share.permission = share_update.permission
    db.commit()
    db.refresh(share)

    reloaded = (
        db.query(DocumentShareModel)
        .options(
            joinedload(DocumentShareModel.shared_by),
            joinedload(DocumentShareModel.target_user),
            joinedload(DocumentShareModel.target_group),
        )
        .filter(DocumentShareModel.id == share.id)
        .first()
    )
    assert reloaded is not None  # just committed above

    update_document_access_index.delay([doc.id])
    return _build_document_share_response(db, reloaded)


@router.delete("/{document_uuid}/shares/{share_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document_share(
    document_uuid: UUID,
    share_uuid: UUID,
    ctx: RequestContext = Depends(get_current_context),
    db: Session = Depends(get_db),
) -> None:
    """Revoke a share. Requires direct ownership (or admin)."""
    doc = _get_owned_document(db, document_uuid, ctx, min_permission="owner")
    share = get_by_uuid(db, DocumentShareModel, str(share_uuid), "Share not found")
    if share.document_id != doc.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")

    db.delete(share)
    db.commit()

    update_document_access_index.delay([doc.id])
    logger.info(
        "Document %s (%s) share %s revoked by user %s", doc.id, doc.uuid, share_uuid, ctx.user.id
    )
