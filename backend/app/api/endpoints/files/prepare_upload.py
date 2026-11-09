import logging
import uuid as uuid_lib
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.files.upload import create_media_file_record
from app.api.error_handlers import ErrorHandler
from app.core.exceptions import DomainError
from app.core.locked_settings import effective_whisper_model
from app.db.base import get_db
from app.legal.processing_gate import require_processing_allowed
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.processing_request import PrepareUploadRequest
from app.services.privacy import file_consent
from app.services.request_context import RequestContext
from app.services.upload_prepare_queries import UploadBatchInUseError
from app.services.upload_prepare_queries import add_file_to_collections
from app.services.upload_prepare_queries import add_tags_to_file
from app.services.upload_prepare_queries import bump_upload_batch_file_count
from app.services.upload_prepare_queries import get_or_create_upload_batch
from app.utils import benchmark_timing
from app.utils.file_hash import check_duplicate_by_fingerprint
from app.utils.file_hash import cleanup_failed_duplicates
from app.utils.media_types import normalize_media_content_type
from app.utils.whisper_model_choice import require_servable_whisper_model

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
        file_metadata,
        current_user,
        request.file_size,
        organization_id,
    )

    # Generate and set storage_path immediately for duplicate detection
    # This allows future uploads with the same file to recognize it as a duplicate
    from app.utils.filename import get_safe_storage_filename

    storage_path = get_safe_storage_filename(request.filename, current_user.id, db_file.id)
    db_file.storage_path = storage_path

    # Store the user's requested whisper model (if any, and if the deployment lets them pick)
    if requested_whisper_model:
        db_file.requested_whisper_model = requested_whisper_model

    # The rest of the per-file request. /complete (or the legacy upload) reads these back
    # for anything it does not repeat, and a retry replays them.
    db_file.requested_min_speakers = request.min_speakers
    db_file.requested_max_speakers = request.max_speakers
    db_file.requested_num_speakers = request.num_speakers
    db_file.requested_disable_diarization = True if request.disable_diarization else None
    # The track to transcribe in a multi-track file (issue #1263); None = the first.
    db_file.audio_stream_index = request.audio_stream_index

    # The uploader's stated voiceprint consent (issue #1356), recorded with the row and who said it.
    file_consent.stamp_new_file(db_file, request.voiceprint_consent, int(current_user.id))

    db.flush()

    # If this is extracted audio, store the video metadata in metadata_important
    if request.extracted_from_video:
        db_file.metadata_important = request.extracted_from_video
        db.commit()
        logger.info(f"Stored extracted video metadata for {request.filename}")

    # Link file to upload batch if a batch UUID was provided
    if request.upload_batch_id:
        try:
            batch = get_or_create_upload_batch(
                db, request.upload_batch_id, current_user.id, source="multi_upload"
            )
        except UploadBatchInUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db_file.upload_batch_id = batch.id
        bump_upload_batch_file_count(db, batch.id)
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


@router.post(
    "/prepare", response_model=dict[str, Any], dependencies=[Depends(refuse_under_support_grant)]
)
async def prepare_upload(
    request: PrepareUploadRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """
    Prepare for a file upload by creating a MediaFile record and returning the file ID.
    This allows the frontend to track the file ID before the actual upload begins.

    If a file hash is provided, check if a duplicate file already exists. When
    ``use_presigned`` is true, the response additionally includes an
    application-level ``task_id`` and a presigned PUT URL for the browser to
    upload bytes directly to MinIO, bypassing the API container entirely.
    """
    current_user = ctx.user
    # The legal processing gate, before any row or presigned URL exists. The caller will own
    # the file, in the request's tenant.
    await run_in_threadpool(
        lambda: require_processing_allowed(
            db, owner_user_id=int(current_user.id), organization_id=ctx.org_id
        )
    )
    # The capability resolver may query the database; keep it off the event loop.
    requested_whisper_model = await run_in_threadpool(
        lambda: require_servable_whisper_model(
            effective_whisper_model(request.whisper_model, http_request)
        )
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

    except (HTTPException, DomainError):
        # Preserve intentional HTTP errors (e.g. 413 tenant size limit, 409
        # duplicate) — don't bury them in a generic 500.
        raise
    except Exception as e:
        # DB/MinIO internals (SQL text, storage paths) are logged, never returned (#859).
        logger.exception("Error preparing upload")
        raise ErrorHandler.internal_error("Could not prepare the upload.") from e
