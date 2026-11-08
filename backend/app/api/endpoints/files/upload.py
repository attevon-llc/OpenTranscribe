import contextlib
import logging
import uuid
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException
from fastapi import UploadFile
from fastapi import status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.endpoints.files.upload_dispatch import dispatch_upload_pipeline_or_mark_error
from app.api.error_handlers import ErrorHandler
from app.core.constants import UPLOAD_CHUNK_SIZE
from app.core.env import env_bool
from app.core.exceptions import DomainError
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.minio_service import upload_file_tuned
from app.utils import benchmark_timing
from app.utils.file_validation import HEADER_READ_SIZE
from app.utils.file_validation import validate_uploaded_file
from app.utils.filename import get_safe_storage_filename
from app.utils.filename import sanitize_filename
from app.utils.media_types import normalize_media_content_type

logger = logging.getLogger(__name__)


def validate_file_type(file: UploadFile) -> None:
    """
    Validate that the uploaded file is an audio or video format.

    Args:
        file: The uploaded file

    Raises:
        HTTPException: If file type is not allowed
    """
    if not file:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file was uploaded. Please select a file.",
        )

    allowed_types = ["audio/", "video/"]
    if not file.content_type or not any(file.content_type.startswith(t) for t in allowed_types):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be an audio or video format",
        )


def validate_file_size_for_tenant(file_size: int, organization_id: int | None) -> None:
    """Enforce the max upload size — the global ceiling, or a tighter per-tenant one.

    Two layers:

    1. ``settings.MAX_UPLOAD_BYTES`` — the GLOBAL ceiling, enforced for everyone.
       Until issue #284 A0.12 there was none: ``resolve_upload_limits`` returns None in
       community, so this function was a complete no-op and the only 15 GB limit in the
       product lived in the browser. Anyone could skip the UI and PUT an arbitrarily
       large object straight to the presigned URL.
    2. ``resolve_upload_limits`` — cloud-edition seam for a tighter per-tier ceiling.
       Community registers no resolver, so only layer 1 applies.

    ``file_size`` of 0/unknown is not rejected here; ``complete_upload`` re-checks against
    the size MinIO actually observed, which is the authoritative number.

    Args:
        file_size: Declared (prepare) or observed (complete) size in bytes.
        organization_id: Tenant scope for the per-tier ceiling.

    Raises:
        HTTPException: 413 when the file exceeds the applicable ceiling.
    """
    if not file_size or file_size <= 0:
        return

    from app.core.config import settings as app_settings
    from app.core.tenant_limits import resolve_upload_limits

    ceiling = app_settings.MAX_UPLOAD_BYTES
    detail = (
        f"File exceeds the maximum upload size of {ceiling / (1024**3):.1f} GB." if ceiling else ""
    )

    limits = resolve_upload_limits(organization_id)
    tier_ceiling = limits.max_file_bytes if limits is not None else None
    # A per-tier ceiling only ever tightens the global one, never loosens it.
    if tier_ceiling is not None and (ceiling is None or tier_ceiling < ceiling):
        ceiling = tier_ceiling
        detail = (
            f"File exceeds your plan's maximum upload size of "
            f"{ceiling / (1024**3):.1f} GB. Upgrade your plan to upload larger files."
        )

    if ceiling is not None and file_size > ceiling:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=detail,
        )


def create_media_file_record(
    db: Session,
    file: UploadFile,
    current_user: User,
    file_size: int,
    organization_id: int | None = None,
) -> MediaFile:
    """
    Create a MediaFile database record.

    Args:
        db: Database session
        file: Uploaded file
        current_user: Current user
        file_size: Size of the file in bytes
        organization_id: Active organization id (cloud org-scoped upload) or
            None for personal scope. Always None in the community edition.

    Returns:
        Created MediaFile object
    """
    try:
        if not hasattr(FileStatus, "PENDING"):
            raise ValueError("FileStatus enum is not properly defined or imported")

        # Check if file has file_hash attribute (from FileMetadata object)
        file_hash = getattr(file, "file_hash", None)

        # Create MediaFile record with essential metadata
        # Sanitize filename to prevent issues with special characters
        sanitized_filename = sanitize_filename(file.filename or "unknown")

        # Extract original title from filename (without extension) for display
        # This preserves characters like apostrophes that get sanitized in the filename
        original_filename = file.filename or "unknown"
        original_title = Path(original_filename).stem

        db_file = MediaFile(
            filename=sanitized_filename,
            title=original_title,
            user_id=current_user.id,
            organization_id=organization_id,
            storage_path="",  # Will be updated after upload
            file_size=file_size,
            content_type=normalize_media_content_type(file.content_type),
            status=FileStatus.PENDING,
            is_public=False,
            duration=None,
            language=None,
            summary_data=None,
            translated_text=None,
            file_hash=file_hash,
            thumbnail_path=None,
        )

        # Flush (not commit) so we get the DB-assigned id + uuid without
        # persisting the row yet. Callers commit the outer transaction
        # after they finish their own updates — eliminates one of the
        # two round-trips the legacy upload flow was paying per file
        # (Phase 2 PR #7, item E18). prepare_upload's own db.commit()
        # later on now covers the INSERT and its follow-on mutations in
        # a single transaction.
        db.add(db_file)
        db.flush()
        db.refresh(db_file)

        return db_file

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        # The real error (often SQL text) is logged, never returned (#859).
        logger.exception("Error creating MediaFile")
        raise ErrorHandler.internal_error("Could not create the media file record.") from e


def upload_file_to_storage(
    file_content: BinaryIO, file_size: int, storage_path: str, content_type: str
) -> None:
    """
    Upload file content to MinIO storage.

    Args:
        file_content: Seekable binary stream positioned at 0 (a spooled temp file).
        file_size: Size of the file
        storage_path: Storage path in MinIO
        content_type: MIME type of the file
    """
    if not env_bool("SKIP_S3"):
        file_content.seek(0)
        upload_file_tuned(
            file_content=file_content,
            file_size=file_size,
            object_name=storage_path,
            content_type=content_type,
        )
    else:
        logger.info("Skipping S3 upload in test environment")


def _get_or_create_file_record(
    db: Session,
    file: UploadFile,
    current_user: User,
    file_size: int,
    existing_file_uuid: str | None,
    organization_id: int | None = None,
) -> MediaFile:
    """
    Get an existing file record or create a new one.

    Args:
        db: Database session
        file: Uploaded file
        current_user: Current user
        file_size: Size of the file in bytes
        existing_file_uuid: Optional UUID of existing file record
        organization_id: Active organization id (cloud) or None for personal scope

    Returns:
        MediaFile database record
    """
    if not existing_file_uuid:
        db_file = create_media_file_record(db, file, current_user, file_size, organization_id)
        logger.info(f"Created new file record with ID: {db_file.id}")
        return db_file

    # Look up file by UUID
    db_file_result = (
        db.query(MediaFile)
        .filter(
            MediaFile.uuid == existing_file_uuid,
            MediaFile.user_id == current_user.id,
        )
        .first()
    )

    if not db_file_result:
        logger.warning(
            f"Existing file UUID {existing_file_uuid} not found for user {current_user.id}"
        )
        return create_media_file_record(db, file, current_user, file_size, organization_id)

    logger.info(f"Using existing file record with UUID={existing_file_uuid}")
    db_file_result.status = FileStatus.PENDING
    # Commit deferred to the outer transaction (Phase 2 PR #7, item E18).
    db.flush()
    return db_file_result


async def _read_first_chunk(file: UploadFile) -> bytes:
    """Read the first chunk so magic-byte validation can run fail-fast.

    Large uploads (multi-GB videos) with the wrong MIME type are rejected after one
    chunk rather than after the whole object has been moved to storage.
    """
    await file.seek(0)
    first_chunk = await file.read(UPLOAD_CHUNK_SIZE)
    await file.seek(0)
    return first_chunk or b""


def _spooled_size(spool: BinaryIO) -> int:
    """Byte count of a seekable spool, leaving it positioned at 0."""
    spool.seek(0, 2)
    size = spool.tell()
    spool.seek(0)
    return size


async def _uploaded_bytes(file: UploadFile) -> tuple[BinaryIO, int]:
    """The upload's bytes and their true size, read from Starlette's own spool.

    Starlette has already written the whole multipart part to ``UploadFile.file`` (a
    ``SpooledTemporaryFile`` that rolls over to disk) before the handler runs, and the
    ``upload_limit`` middleware has already bounded it. Copying it into a second spool,
    as this used to, wrote every large upload to disk twice. The content-length header
    is advisory, so the size is the spool's own.
    """
    if file.size is not None:
        await file.seek(0)
        return file.file, int(file.size)
    size = await run_in_threadpool(_spooled_size, file.file)
    return file.file, size


def _update_file_hash(db_file: MediaFile, client_file_hash: str | None, filename: str) -> None:
    """
    Record the client-declared content fingerprint on the database record.

    Client-declared and therefore untrusted: the authoritative fingerprint is
    ``MediaFile.imohash``, recomputed server-side from the stored object. This
    column exists so a *future* upload can be short-circuited before its bytes
    move, and so client-extracted audio can carry its source video's fingerprint
    (see ``utils/file_hash``).

    Args:
        db_file: MediaFile database record
        client_file_hash: Optional content fingerprint from client
        filename: Original filename for logging
    """
    if client_file_hash:
        # Remove 0x prefix if present for database consistency
        if client_file_hash.startswith("0x"):
            client_file_hash = client_file_hash[2:]
        db_file.file_hash = client_file_hash
    elif not db_file.file_hash:
        logger.warning(f"No file hash provided for {filename} - duplicate detection may not work")


def _open_upload_record(
    db: Session,
    file: UploadFile,
    current_user: User,
    file_size: int,
    existing_file_uuid: str | None,
    organization_id: int | None,
) -> int:
    """First short transaction: commit the row as ``pending`` and release the connection.

    The bytes move to object storage after this returns, with no transaction open. Holding
    the flushed row across that transfer kept a connection idle in a transaction for as
    long as the object store took (minutes for a large file, past the server's idle
    timeout), and on the existing-row path kept that row locked the whole time.

    Returns:
        The row's id, which also names its storage object.
    """
    db_file = _get_or_create_file_record(
        db, file, current_user, file_size, existing_file_uuid, organization_id
    )
    db.commit()
    file_id = int(db_file.id)
    db.close()
    return file_id


def _finish_upload_record(
    db: Session,
    file_id: int,
    *,
    storage_path: str,
    file_size: int,
    client_file_hash: str | None,
    filename: str,
    imohash: str | None,
    skip_summary: bool,
) -> MediaFile:
    """Second short transaction: record where the bytes landed.

    Raises:
        HTTPException: 404 when the row was deleted while its bytes were moving.
    """
    db_file = db.get(MediaFile, file_id)
    if db_file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    _update_file_hash(db_file, client_file_hash, filename)
    if imohash:
        db_file.imohash = imohash
    db_file.storage_path = storage_path
    db_file.file_size = file_size
    # Per-file skip summary: mark as disabled before the pipeline starts.
    if skip_summary:
        db_file.summary_status = "disabled"
    db.commit()
    db.refresh(db_file)
    return db_file


def _discard_upload(db: Session, file_id: int, storage_path: str | None) -> None:
    """Compensate a failed upload: delete its row and any object already stored.

    The row was committed before the bytes moved, so a failure can no longer simply roll
    back. Without this a rejected or failed upload would leave a ``pending`` row with no
    object behind it. Each step is best-effort and logged, so one failing cannot stop
    the other, and the request still reports the original error.
    """
    if storage_path is not None and not env_bool("SKIP_S3"):
        from app.core.config import settings
        from app.services.minio_service import remove_object_reported

        removal = remove_object_reported(settings.MEDIA_BUCKET_NAME, storage_path)
        if removal.outcome == "failed":
            logger.warning("Could not remove the object of failed upload %s", file_id)
    try:
        db.rollback()
        db_file = db.get(MediaFile, file_id)
        if db_file is not None:
            db.delete(db_file)
            db.commit()
    except SQLAlchemyError:
        logger.exception("Could not delete the record of failed upload %s", file_id)
    finally:
        db.close()


async def process_file_upload(
    file: UploadFile,
    db: Session,
    current_user: User,
    existing_file_uuid: str | None = None,
    client_file_hash: str | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    skip_summary: bool = False,
    whisper_model: str | None = None,
    organization_id: int | None = None,
) -> MediaFile:
    """
    Complete file upload processing pipeline with chunked upload support for large files.
    Includes file hash calculation for duplicate detection and thumbnail generation for video files.

    Args:
        file: Uploaded file
        db: Database session
        current_user: Current user
        existing_file_uuid: Optional UUID of existing file record from prepare_upload
        client_file_hash: Optional file hash calculated by the client (preferred method)
        min_speakers: Optional minimum number of speakers for diarization
        max_speakers: Optional maximum number of speakers for diarization
        num_speakers: Optional fixed number of speakers for diarization
        whisper_model: Optional Whisper model override for this transcription
        organization_id: Active organization id (cloud org-scoped upload) or
            None for personal scope. Threaded onto the created MediaFile.

    Returns:
        Created MediaFile object with storage path updated

    Raises:
        HTTPException: If there's an error during file processing
    """
    # Generate the application task_id at ingress so every HTTP-phase marker
    # lands in the same benchmark:{task_id} Redis hash as the downstream
    # pipeline stages. Threaded through start_transcription_task.
    task_id = str(uuid.uuid4())
    benchmark_timing.mark(task_id, "http_request_received")

    logger.info(f"File upload request received from user: {current_user.email}")
    # Read now: the first transaction's commit expires ``current_user`` and its close
    # detaches it, so it cannot be read after the bytes move.
    user_id = int(current_user.id)

    # Validate file type first
    validate_file_type(file)
    logger.info(f"Processing file - filename: {file.filename}, content_type: {file.content_type}")
    content_type = normalize_media_content_type(file.content_type)

    # Duplicate short-circuit (Phase 2 PR #7, item E20): if the client sent a
    # file hash and this is a direct legacy POST (no existing_file_uuid),
    # check for a matching file and 409 out before reading bytes.
    # prepare_upload already does this check for the presigned flow.
    if client_file_hash and not existing_file_uuid:
        from app.utils.file_hash import check_duplicate_by_fingerprint

        duplicate_uuid = await run_in_threadpool(
            check_duplicate_by_fingerprint,
            db,
            client_file_hash,
            current_user.id,
            organization_id=organization_id,
        )
        if duplicate_uuid:
            logger.info(
                f"Duplicate upload rejected for {file.filename} "
                f"(hash={client_file_hash[:12]}…, duplicate_uuid={duplicate_uuid})"
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "A file with this content already exists.",
                    "duplicate_file_uuid": duplicate_uuid,
                },
            )

    # Get file size from content-length header if available
    file_size = 0
    with contextlib.suppress(ValueError, TypeError):
        file_size = int(file.headers.get("content-length", 0))

    # First short transaction: the row is committed as pending and the connection is
    # released before any bytes move.
    file_id = await run_in_threadpool(
        _open_upload_record,
        db,
        file,
        current_user,
        file_size,
        existing_file_uuid,
        organization_id,
    )

    # Generate storage path with sanitized filename
    storage_path = get_safe_storage_filename(file.filename or "unknown", user_id, file_id)

    stored = False
    try:
        # Read and validate the first chunk before moving the rest (Phase 2 PR #7,
        # item E19): a wrong-type upload is rejected after one chunk.
        first_chunk = await _read_first_chunk(file)
        is_valid, validation_result = validate_uploaded_file(
            first_chunk[:HEADER_READ_SIZE], content_type, file.filename
        )
        benchmark_timing.mark(task_id, "http_validation_end")
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=validation_result,
            )
        logger.info(f"File validated: {file.filename} (detected: {validation_result})")

        file_content, file_size = await _uploaded_bytes(file)
        benchmark_timing.mark(task_id, "http_read_complete")

        # Enforce the max upload size now that the true byte count is known (the
        # content-length header is advisory). A registered resolver may block, so
        # it runs off the event loop (#1169). The cleanup path below 413s out.
        await run_in_threadpool(validate_file_size_for_tenant, file_size, organization_id)

        # imohash samples 3x128KiB regardless of size, for server-side dedup and
        # artifact caching. Best-effort: a failure never breaks the upload.
        imohash: str | None = None
        try:
            from app.services.imohash_service import compute_from_stream

            benchmark_timing.mark(task_id, "imohash_start")
            file_content.seek(0)
            imohash = compute_from_stream(file_content, size=file_size)
            file_content.seek(0)
            benchmark_timing.mark(task_id, "imohash_end")
        except Exception as im_err:
            logger.debug(f"imohash compute failed (non-fatal): {im_err}")

        # Upload to storage. No transaction is open here.
        with benchmark_timing.stage(task_id, "minio_put"):
            await run_in_threadpool(
                upload_file_to_storage,
                file_content,
                file_size,
                storage_path,
                content_type or "application/octet-stream",
            )
        stored = True

        # Thumbnail generation is deferred to generate_thumbnail_task, dispatched
        # after the commit below (Phase 2 PR #5); thumbnail_path lands on the row
        # when that task finishes.

        # Second short transaction: record where the bytes are.
        with benchmark_timing.stage(task_id, "db_commit"):
            db_file = await run_in_threadpool(
                _finish_upload_record,
                db,
                file_id,
                storage_path=storage_path,
                file_size=file_size,
                client_file_hash=client_file_hash,
                filename=file.filename or "unknown",
                imohash=imohash,
                skip_summary=skip_summary,
            )

    except (HTTPException, DomainError):
        # Re-raise HTTP exceptions after cleanup. Everything below this try/except
        # only runs once the row has committed with its storage_path, i.e. the
        # bytes are already in object storage — nothing past this point may
        # re-enter this delete-the-row handling (issue #905).
        await run_in_threadpool(_discard_upload, db, file_id, storage_path if stored else None)
        raise
    except Exception as e:
        # Clean up on failure. Container paths and storage errors are logged,
        # never returned to the caller (#859).
        await run_in_threadpool(_discard_upload, db, file_id, storage_path if stored else None)
        logger.exception("Upload failed")
        raise ErrorHandler.internal_error("The file upload could not be completed.") from e

    # Capture file context for later reporting (persists until flushed
    # into file_pipeline_timing by finalize_transcription).
    benchmark_timing.set_context(
        task_id,
        {
            "file_size_bytes": int(file_size),
            "content_type": content_type or "",
            "http_flow": "legacy",
        },
    )

    # Fire the thumbnail + transcription pipeline via the shared dispatch tail so
    # this route stays in lockstep with the presigned /complete one. The row is
    # committed with its storage_path by this point — a dispatch failure here is
    # a PROCESSING failure (ERROR + last_error_message), never a deleted row
    # (issue #905): the bytes are already stored, so deleting the row would leak
    # the MinIO object and hide a real failure behind a generic 500.
    await run_in_threadpool(
        dispatch_upload_pipeline_or_mark_error,
        db,
        db_file,
        user_id=user_id,
        whisper_model=whisper_model,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        num_speakers=num_speakers,
        task_id=task_id,
    )

    # Product metric: file accepted via direct (legacy) upload (API process).
    from app.core.metrics import files_uploaded_total

    files_uploaded_total.labels(source="upload").inc()

    benchmark_timing.mark(task_id, "http_response_end")
    logger.info(f"File processed: {file.filename} (ID: {db_file.id})")
    return db_file
