import logging
import uuid as uuid_lib
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.files.upload import create_media_file_record
from app.core.constants import TAG_SOURCE_MANUAL
from app.core.locked_settings import effective_whisper_model
from app.db.base import get_db
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.organization import OrganizationMembership
from app.models.upload_batch import UploadBatch
from app.models.user import User
from app.schemas.media import PrepareUploadRequest
from app.services.permission_service import PermissionService
from app.services.tag_service import on_tags_changed
from app.services.tag_service import resolve_or_create_tags
from app.utils import benchmark_timing
from app.utils.error_handlers import ErrorHandler
from app.utils.file_hash import check_duplicate_by_fingerprint
from app.utils.file_hash import cleanup_failed_duplicates
from app.utils.media_types import normalize_media_content_type

logger = logging.getLogger(__name__)

router = APIRouter()


class FileMetadata:
    """Lightweight class containing essential file information without the actual file content.
    Used for creating database records before the actual file upload starts.

    Attributes:
        filename: Name of the file to be uploaded
        content_type: MIME type of the file
        file_hash: Hash of the file for duplicate detection
        extracted_from_video: Optional metadata from original video if audio was extracted client-side
    """

    def __init__(
        self,
        filename: str,
        content_type: str,
        extracted_from_video: dict[str, Any] | None = None,
    ):
        self.filename = filename
        self.content_type = content_type
        self.file_hash: str | None = None
        self.extracted_from_video = extracted_from_video


def get_or_create_upload_batch(
    db: Session, batch_uuid: UUID, user_id: int, source: str = "multi_upload"
) -> UploadBatch:
    """Get an existing UploadBatch by UUID or create a new one.

    Uses the client-provided UUID as the batch identifier. If a batch with
    that UUID already exists for this user, returns it. Otherwise creates a new one.
    Raises HTTP 409 if the UUID belongs to a different user.

    Args:
        db: Database session
        batch_uuid: Client-generated UUID for the batch
        user_id: Owner user ID
        source: Upload source type (multi_upload, playlist, url_batch)

    Returns:
        UploadBatch record
    """
    # Atomic: parallel prepare requests of one multi-file upload share this uuid, so a
    # read-then-insert would let two requests both miss and the loser hit the unique index.
    # A concurrent uncommitted insert makes this statement wait for it, then do nothing.
    inserted = db.execute(
        pg_insert(UploadBatch)
        .values(uuid=batch_uuid, user_id=user_id, source=source, file_count=0)
        .on_conflict_do_nothing(index_elements=[UploadBatch.uuid])
        .returning(UploadBatch.id)
    ).first()
    batch = db.query(UploadBatch).filter(UploadBatch.uuid == batch_uuid).one()
    if batch.user_id != user_id:
        # Never link a file to a batch another user owns.
        raise HTTPException(status_code=409, detail="Upload batch id is already in use")
    if inserted is not None:
        logger.info(f"Created upload batch {batch_uuid} for user {user_id}")
    return batch  # type: ignore[return-value, no-any-return]


def increment_upload_batch_file_count(db: Session, batch_id: int) -> None:
    """Atomically bump ``file_count`` (no read-modify-write, safe under concurrency)."""
    db.execute(
        update(UploadBatch)
        .where(UploadBatch.id == batch_id)
        .values(file_count=UploadBatch.file_count + 1)
    )


def add_file_to_collections(
    db: Session, file_id: int, user_id: int, collection_ids: list[UUID]
) -> None:
    """Add a media file to the named collections of the file's tenant.

    Only collections of the FILE's tenant qualify (issue #1051): for a personal
    file, ``user_id``'s own personal collections; for an organization file, the
    organization's collections, provided ``user_id`` is still a member. Anything
    else — another tenant's collection, a stranger's — is silently skipped, so an
    import can never put one tenant's recording into another tenant's collection.

    Two queries regardless of how many collections are named (issue #284 A2.8). This
    used to run a `Collection` lookup and a `CollectionMember` existence check per
    UUID — 2N round trips on the upload-prep path, which the SPA calls once per file
    in a multi-file upload, so a 50-file batch across 5 collections paid 500 queries.
    Bounded N is not unbounded, but it is on the hot upload path and the batched form
    is no harder to read.
    """
    if not collection_ids:
        return

    wanted = [str(coll_uuid) for coll_uuid in collection_ids]

    # The file's tenant is a correlated subquery, not a separate SELECT, so the
    # lookup stays one round trip (the batching contract above).
    file_org = select(MediaFile.organization_id).where(MediaFile.id == file_id).scalar_subquery()
    is_member = (
        select(OrganizationMembership.id)
        .where(
            OrganizationMembership.organization_id == Collection.organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .exists()
    )
    same_tenant = or_(
        and_(file_org.is_(None), PermissionService.collection_tenant_pred(user_id, None)),
        and_(Collection.organization_id == file_org, is_member),
    )
    collections = db.query(Collection).filter(Collection.uuid.in_(wanted), same_tenant).all()
    by_uuid = {str(collection.uuid): collection for collection in collections}

    for coll_uuid in wanted:
        if coll_uuid not in by_uuid:
            logger.warning(f"Collection {coll_uuid} not found for user {user_id}, skipping")

    resolved_ids = [collection.id for collection in collections]
    if not resolved_ids:
        db.flush()
        return

    already_member = {
        row[0]
        for row in db.query(CollectionMember.collection_id).filter(
            CollectionMember.collection_id.in_(resolved_ids),
            CollectionMember.media_file_id == file_id,
        )
    }

    # dict.fromkeys keeps insertion order while de-duplicating a repeated UUID —
    # the per-UUID loop relied on the just-added row being visible to the next
    # existence check, which autoflush=False sessions do not guarantee.
    for collection_id in dict.fromkeys(resolved_ids):
        if collection_id not in already_member:
            db.add(CollectionMember(collection_id=collection_id, media_file_id=file_id))

    db.flush()


def add_tags_to_file(db: Session, file_id: int, tag_names: list[str], user_id: int) -> None:
    """Add tags to a media file, creating tags if they don't exist.

    ``user_id`` owns (or, on an organization file, is credited with) any tag
    this creates. Background importers (watch sources, yt-dlp playlists) must
    pass the **file owner**, never leave it unset — a tag with neither owner nor
    tenant is a system tag and would be published to every account.
    Tags resolve in the **file's tenant** (``MediaFile.organization_id``), read
    here rather than taken from each caller, so no importer can land an org
    file's tags in someone's personal vocabulary (issue #1050). A same-named
    system tag is reused rather than forked, so applying a seeded default still
    attaches the shared row.

    Resolution (normalization, normalized-exact match, SAVEPOINT-guarded insert)
    is shared with every other tag-creation path via
    ``app/services/tag_service.py``, and stays batched — a constant number of
    SELECTs regardless of list length (issue #284 A2.8), not 2N. These names
    were typed by a person, so the fuzzy suggestion lookup is deliberately not
    consulted.
    """
    # Session.get, not a query: every caller has the row in the identity map
    # already, so this costs no SELECT (#284 A2.8 pins the count).
    media_file = db.get(MediaFile, file_id)
    organization_id = media_file.organization_id if media_file is not None else None
    tag_ids = [
        tag.id
        for tag in resolve_or_create_tags(
            db, tag_names, user_id=user_id, organization_id=organization_id
        )
    ]

    if not tag_ids:
        db.flush()
        return

    already_tagged = {
        row[0]
        for row in db.query(FileTag.tag_id).filter(
            FileTag.media_file_id == file_id, FileTag.tag_id.in_(tag_ids)
        )
    }
    for tag_id in dict.fromkeys(tag_ids):
        if tag_id not in already_tagged:
            db.add(FileTag(media_file_id=file_id, tag_id=tag_id, source=TAG_SOURCE_MANUAL))

    db.flush()

    on_tags_changed(db, [file_id], user_id=user_id)


def create_prepared_record(
    db: Session,
    request: PrepareUploadRequest,
    current_user: User,
    organization_id: int | None,
    requested_whisper_model: str | None,
) -> tuple[MediaFile, int, str, str]:
    """Create and commit the prepared ``MediaFile`` row with its batch/collection/tag links.

    Synchronous on purpose: ``prepare_upload`` runs it in the threadpool. It is a dozen
    round trips, and inline on the event loop a burst of concurrent prepares serialised
    them there, stalling every other request on the process (#1169).

    Returns:
        ``(db_file, file_id, file_uuid, storage_path)`` — the ids are read here, after the
        commit, so the caller never triggers a refresh on the event loop.
    """
    # Create file metadata object with information needed for the record
    file_metadata = FileMetadata(
        request.filename,
        request.content_type,
        extracted_from_video=request.extracted_from_video,
    )
    file_metadata.file_hash = request.file_hash

    # Create the database record
    db_file = create_media_file_record(
        db,
        file_metadata,  # type: ignore[arg-type]
        current_user,
        request.file_size,
        organization_id,
    )

    # Generate and set storage_path immediately for duplicate detection
    # This allows future uploads with the same file to recognize it as a duplicate
    from app.utils.filename import get_safe_storage_filename

    storage_path = get_safe_storage_filename(request.filename, current_user.id, db_file.id)
    db_file.storage_path = storage_path  # type: ignore[assignment]

    # Store the user's requested whisper model (if any, and if the deployment lets them pick)
    if requested_whisper_model:
        db_file.requested_whisper_model = requested_whisper_model  # type: ignore[assignment]

    db.flush()

    # If this is extracted audio, store the video metadata in metadata_important
    if request.extracted_from_video:
        db_file.metadata_important = request.extracted_from_video  # type: ignore[assignment]
        db.commit()
        logger.info(f"Stored extracted video metadata for {request.filename}")

    # Link file to upload batch if a batch UUID was provided
    if request.upload_batch_id:
        batch = get_or_create_upload_batch(
            db, request.upload_batch_id, current_user.id, source="multi_upload"
        )
        db_file.upload_batch_id = batch.id  # type: ignore[assignment]
        increment_upload_batch_file_count(db, batch.id)  # type: ignore[arg-type]
        db.flush()
        logger.info(f"Linked file {db_file.id} to upload batch {request.upload_batch_id}")

    # Assign to collections if specified
    if request.collection_ids:
        add_file_to_collections(db, db_file.id, current_user.id, request.collection_ids)

    # Apply tags if specified
    if request.tag_names:
        add_tags_to_file(db, db_file.id, request.tag_names, current_user.id)

    # Commit all assignments (batch, collections, tags)
    db.commit()
    return db_file, int(db_file.id), str(db_file.uuid), storage_path


def _discard_prepared_record(db: Session, db_file: MediaFile) -> None:
    db.delete(db_file)
    db.commit()


@router.post("/prepare", response_model=dict[str, Any])
async def prepare_upload(
    request: PrepareUploadRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Prepare for a file upload by creating a MediaFile record and returning the file ID.
    This allows the frontend to track the file ID before the actual upload begins.

    If a file hash is provided, check if a duplicate file already exists. When
    ``use_presigned`` is true, the response additionally includes an
    application-level ``task_id`` and a presigned PUT URL for the browser to
    upload bytes directly to MinIO, bypassing the API container entirely.
    """
    current_user = ctx.user
    # The capability resolver may query the database; keep it off the event loop.
    requested_whisper_model = await run_in_threadpool(
        effective_whisper_model, request.whisper_model, http_request
    )
    try:
        # If file hash is provided, check for duplicates
        duplicate_id: str | None = None
        if request.file_hash:
            # First clean up any failed files with the same hash to allow re-upload
            await run_in_threadpool(
                cleanup_failed_duplicates,
                db,
                request.file_hash,
                current_user.id,
                organization_id=ctx.org_id,
            )

            duplicate_id = await run_in_threadpool(
                check_duplicate_by_fingerprint,
                db,
                request.file_hash,
                current_user.id,
                organization_id=ctx.org_id,
            )

            if duplicate_id:
                logger.info(
                    f"Duplicate file found for {request.filename} (Duplicate ID: {duplicate_id})"
                )
                return {"file_id": duplicate_id, "is_duplicate": 1}
        else:
            # No fingerprint means this upload was never checked against the
            # library. That used to be the silent default for every file above
            # ~4 GB (issue #342); it must leave a trace on both sides of the wire.
            logger.warning(
                f"No content fingerprint supplied for {request.filename} "
                f"({request.file_size} bytes) - duplicate detection skipped"
            )

        # Enforce the max upload size (global ceiling, or a tighter one from a
        # registered upload-limits resolver) before minting a record / presigned
        # URL. A resolver may do blocking I/O, so it runs in the threadpool: inline
        # it would stall every request on this process for as long as it blocks.
        from app.api.endpoints.files.upload import validate_file_size_for_tenant

        await run_in_threadpool(validate_file_size_for_tenant, request.file_size, ctx.org_id)

        db_file, file_id, file_uuid, storage_path = await run_in_threadpool(
            create_prepared_record,
            db,
            request,
            current_user,
            ctx.org_id,
            requested_whisper_model,
        )

        response: dict[str, Any] = {"file_id": file_uuid, "is_duplicate": 0}

        # Optional: set the browser up to write bytes straight to object storage,
        # bypassing the API container. The backend picks the transport — one
        # presigned PUT, or a presigned multipart upload when the object is large
        # enough to need resume (or too large for a single PUT: AWS rejects one
        # above 5 GiB with EntityTooLarge, after the browser has already streamed
        # the whole body). The client only executes the plan; see
        # ``services/multipart_upload.build_upload_plan``. A None plan means
        # neither transport is available, and the client falls back to POST /files.
        #
        # The application task_id is minted here so every HTTP-phase marker shares
        # the benchmark:{task_id} Redis hash with the downstream pipeline.
        if request.use_presigned:
            from app.core.config import settings as app_settings
            from app.services.multipart_upload import build_upload_plan

            plan = await run_in_threadpool(
                build_upload_plan,
                storage_path,
                normalize_media_content_type(request.content_type),
                request.file_size,
            )
            if plan is None and not app_settings.API_MEDIATED_UPLOAD_ENABLED:
                # There is no fallback to hand the client to (issue #1008). Drop the
                # row this call just created and ask the client to retry, rather than
                # leaving it a PENDING row pointing at a route that will 404.
                logger.warning(
                    f"No browser-direct upload plan for {request.filename} and the "
                    "API-mediated upload is disabled; asking the client to retry"
                )
                await run_in_threadpool(_discard_prepared_record, db, db_file)
                raise HTTPException(
                    status_code=503,
                    detail="Direct upload is temporarily unavailable. Please retry shortly.",
                )
            if plan is None:
                logger.info(
                    f"No browser-direct upload plan for {request.filename} "
                    f"({request.file_size} bytes); falling back to POST /files"
                )
            else:
                task_id = str(uuid_lib.uuid4())
                benchmark_timing.mark(task_id, "prepare_upload_end")
                benchmark_timing.set_context(
                    task_id,
                    {
                        "file_size_bytes": int(request.file_size or 0),
                        "content_type": request.content_type or "",
                        "http_flow": plan["http_flow"],
                    },
                )
                response.update(plan)
                response["task_id"] = task_id
                response["storage_path"] = storage_path

        logger.info(f"Prepared upload for file {request.filename} (ID: {file_id})")
        return response

    except HTTPException:
        # Preserve intentional HTTP errors (e.g. 413 tenant size limit, 409
        # duplicate) — don't bury them in a generic 500.
        raise
    except Exception as e:
        # DB/MinIO internals (SQL text, storage paths) are logged, never returned (#859).
        logger.exception("Error preparing upload")
        raise ErrorHandler.internal_error("Could not prepare the upload.") from e
