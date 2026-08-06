import contextlib
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO
from typing import cast

from fastapi import HTTPException
from fastapi import UploadFile
from fastapi import status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.constants import UPLOAD_CHUNK_SIZE
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.minio_service import upload_file_tuned
from app.utils import benchmark_timing
from app.utils.file_validation import validate_uploaded_file
from app.utils.filename import get_safe_storage_filename
from app.utils.filename import sanitize_filename

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
            content_type=file.content_type,
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

    except Exception as e:
        logger.exception(f"Error creating MediaFile: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating media file record: {str(e)}",
        ) from e


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
    if os.environ.get("SKIP_S3", "False").lower() != "true":
        file_content.seek(0)
        upload_file_tuned(
            file_content=file_content,
            file_size=file_size,
            object_name=storage_path,
            content_type=content_type,
        )
    else:
        logger.info("Skipping S3 upload in test environment")


def start_transcription_task(
    file_id: int,
    file_uuid: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    whisper_model: str | None = None,
    disable_diarization: bool | None = None,
    task_id: str | None = None,
) -> str | None:
    """
    Start the background transcription and waveform generation tasks in parallel.

    The transcription pipeline auto-resolves the correct queue based on the user's
    active ASR provider (cloud-asr for cloud providers, gpu for local).

    Args:
        file_id: Database ID of the media file
        file_uuid: UUID of the media file to transcribe
        min_speakers: Optional minimum number of speakers for diarization
        max_speakers: Optional maximum number of speakers for diarization
        num_speakers: Optional fixed number of speakers for diarization
        whisper_model: Optional Whisper model override for this transcription
        task_id: Optional pre-generated application task_id. When provided,
            it's threaded through dispatch_transcription_pipeline and
            generate_waveform_task so HTTP-ingress benchmark markers share
            the benchmark:{task_id} Redis hash with the pipeline markers.

    Returns:
        The application-level task_id that was dispatched, or None when
        running with SKIP_CELERY=true.
    """
    if os.environ.get("SKIP_CELERY", "False").lower() != "true":
        # Dispatch 3-stage pipeline chain: CPU preprocess → GPU transcribe → CPU postprocess
        # Queue routing is auto-resolved inside dispatch_transcription_pipeline
        from app.tasks.transcription import dispatch_transcription_pipeline

        dispatched_task_id = dispatch_transcription_pipeline(
            file_uuid=file_uuid,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            disable_diarization=disable_diarization,
            whisper_model=whisper_model,
            task_id=task_id,
        )
        # Waveform generation is dispatched from the preprocess task once the
        # 16 kHz WAV is staged in MinIO temp (see Phase 2 PR #3: eliminates
        # the second full download of the original file).
        logger.info(f"Dispatched pipeline chain for file {file_id} (task_id={dispatched_task_id})")
        return dispatched_task_id
    else:
        logger.info("Skipping Celery task in test environment")
        return None


def dispatch_thumbnail_for_video(db_file: MediaFile, user_id: int) -> None:
    """Dispatch background WebP thumbnail generation for video files.

    Shared by the legacy multipart upload and the presigned ``/complete``
    path so every video gets a thumbnail — and the gallery's live
    ``file_updated`` refresh — regardless of ingest route. No-op for audio.
    Reads ``content_type`` off the row (set by both ingest paths) so the
    two upload routes can't drift on this again.
    """
    content_type = getattr(db_file, "content_type", None)
    if not (content_type and str(content_type).startswith("video/")):
        return
    try:
        from app.tasks.thumbnail import generate_thumbnail_task

        generate_thumbnail_task.delay(
            file_id=db_file.id,
            user_id=int(user_id),
            storage_path=str(db_file.storage_path),
        )
        logger.info(f"Dispatched thumbnail generation for video file {db_file.id}")
    except Exception as thumb_err:
        logger.warning(f"Thumbnail dispatch failed for {db_file.id} (non-fatal): {thumb_err}")


def dispatch_upload_pipeline(
    db_file: MediaFile,
    *,
    user_id: int,
    whisper_model: str | None,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    task_id: str | None,
) -> str | None:
    """Shared post-commit dispatch tail for BOTH upload ingest paths.

    Fires the background thumbnail and dispatches the transcription pipeline.
    The legacy multipart and presigned ``/complete`` routes both call this so
    the "what happens after the bytes land" logic lives in ONE place — the
    thumbnail/validation gaps came from these two tails being hand-copied and
    drifting. Call AFTER the row is committed. ``whisper_model`` falls back to
    the per-file requested model when not explicitly provided.
    """
    if not whisper_model and db_file.requested_whisper_model:
        whisper_model = str(db_file.requested_whisper_model)
    dispatch_thumbnail_for_video(db_file, user_id)
    return start_transcription_task(
        db_file.id,
        str(db_file.uuid),
        min_speakers,
        max_speakers,
        num_speakers,
        whisper_model=whisper_model,
        task_id=task_id,
    )


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
    db_file_result.status = FileStatus.PENDING  # type: ignore[assignment]
    # Commit deferred to the outer transaction (Phase 2 PR #7, item E18).
    db.flush()
    return db_file_result  # type: ignore[no-any-return]


async def _read_first_chunk(file: UploadFile) -> tuple[bytearray, int]:
    """Read the first chunk so magic-byte validation can run fail-fast.

    Large uploads (multi-GB videos) with the wrong MIME type should be
    rejected before we pay to read the remaining bytes. Returns the
    first chunk's bytes and the number of bytes read.
    """
    first_chunk = await file.read(UPLOAD_CHUNK_SIZE)
    buf = bytearray(first_chunk) if first_chunk else bytearray()
    return buf, len(buf)


#: Spool uploads larger than this to disk instead of RAM. Small uploads (the common
#: case) stay in memory and pay nothing; only genuinely large ones touch the filesystem.
UPLOAD_SPOOL_MAX_MEMORY = 32 * 1024 * 1024


async def _continue_read_after_first_chunk(
    file: UploadFile, buffered: bytearray
) -> tuple[BinaryIO, int]:
    """Spool the upload to a rolling temp file and return it positioned at 0.

    Used after ``_read_first_chunk`` passes magic-byte validation.

    Previously this extended a bytearray to EOF, holding the ENTIRE file in RAM
    (issue #284 A1.20). A multi-GB legacy upload therefore OOMKilled the process — and
    because it is the API process, that drops every other in-flight request, WebSocket,
    and SSE stream with it. One user's large upload could take out everyone's session.

    ``SpooledTemporaryFile`` keeps the first ``UPLOAD_SPOOL_MAX_MEMORY`` in memory and
    transparently rolls over to disk beyond it, so the common small upload is unchanged
    while a large one is bounded by disk rather than RAM.

    Returns:
        ``(spooled_file, total_bytes)`` — the file is seeked to 0, ready to read.
    """
    # Not a context manager on purpose: the spool is RETURNED and must outlive this
    # function. The caller owns it and closes it in its finally (which also unlinks
    # the on-disk backing file once it has rolled over).
    spool = cast(
        "BinaryIO",
        tempfile.SpooledTemporaryFile(  # noqa: SIM115 - caller-owned lifetime
            max_size=UPLOAD_SPOOL_MAX_MEMORY
        ),
    )
    total_read = len(buffered)
    if buffered:
        spool.write(bytes(buffered))
    while True:
        chunk = await file.read(UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        spool.write(chunk)
        total_read += len(chunk)
    spool.seek(0)
    return spool, total_read


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
        db_file.file_hash = client_file_hash  # type: ignore[assignment]
    elif not db_file.file_hash:
        logger.warning(f"No file hash provided for {filename} - duplicate detection may not work")


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

    # Validate file type first
    validate_file_type(file)
    logger.info(f"Processing file - filename: {file.filename}, content_type: {file.content_type}")

    # Duplicate short-circuit (Phase 2 PR #7, item E20): if the client sent a
    # file hash and this is a direct legacy POST (no existing_file_uuid),
    # check for a matching file and 409 out before reading bytes.
    # prepare_upload already does this check for the presigned flow.
    if client_file_hash and not existing_file_uuid:
        from app.utils.file_hash import check_duplicate_by_fingerprint

        duplicate_uuid = await run_in_threadpool(
            check_duplicate_by_fingerprint, db, client_file_hash, current_user.id
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

    # Get or create file record
    db_file = _get_or_create_file_record(
        db, file, current_user, file_size, existing_file_uuid, organization_id
    )

    # Generate storage path with sanitized filename
    storage_path = get_safe_storage_filename(
        file.filename or "unknown", current_user.id, db_file.id
    )

    spooled_upload: BinaryIO | None = None
    try:
        # Read and validate the first chunk BEFORE committing to the full read
        # (Phase 2 PR #7, item E19). Streaming magic-byte validation lets us
        # reject wrong-type uploads after a single chunk instead of buffering
        # 50 GB of garbage to learn the MIME is wrong.
        first_chunk, _first_bytes = await _read_first_chunk(file)
        is_valid, validation_result = validate_uploaded_file(
            bytes(first_chunk[:64]), file.content_type, file.filename
        )
        benchmark_timing.mark(task_id, "http_validation_end")
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=validation_result,
            )
        logger.info(f"File validated: {file.filename} (detected: {validation_result})")

        # Spool the remainder to a rolling temp file (RAM up to a threshold, then
        # disk). Closed in the finally below — which also unlinks the backing file if
        # it rolled over, so a failed upload cannot leave one behind.
        file_content, file_size = await _continue_read_after_first_chunk(file, first_chunk)
        spooled_upload = file_content
        benchmark_timing.mark(task_id, "http_read_complete")

        # Cloud-edition seam: enforce the tenant's per-tier max upload size now
        # that the true byte count is known (the content-length header is
        # advisory). No-op in community. The cleanup path below 413s out.
        validate_file_size_for_tenant(file_size, organization_id)

        # Update file hash
        _update_file_hash(db_file, client_file_hash, file.filename or "unknown")

        # Compute imohash from the in-memory buffer (cheap: 3x 128KiB samples
        # regardless of file size). Used for server-side dedup + artifact
        # caching. Best-effort — failures never break uploads.
        try:
            from app.services.imohash_service import compute_from_stream

            benchmark_timing.mark(task_id, "imohash_start")
            # Stream-based: imohash samples 3x128KiB regardless of size, so this never
            # materializes the file (which is the whole point of spooling it).
            file_content.seek(0)
            db_file.imohash = compute_from_stream(  # type: ignore[assignment]
                file_content, size=file_size
            )
            file_content.seek(0)
            benchmark_timing.mark(task_id, "imohash_end")
        except Exception as im_err:
            logger.debug(f"imohash compute failed (non-fatal): {im_err}")

        # Upload to storage
        with benchmark_timing.stage(task_id, "minio_put"):
            await run_in_threadpool(
                upload_file_to_storage,
                file_content,
                file_size,
                storage_path,
                file.content_type or "application/octet-stream",
            )

        # Thumbnail generation was previously inline here (3-8s FFmpeg on
        # the buffered video). Now deferred to generate_thumbnail_task,
        # dispatched after the commit below — keeps the HTTP response
        # fast (Phase 2 PR #5). thumbnail_path will land on the row when
        # the task finishes and the frontend refreshes on the
        # file_updated WebSocket event.

        # Update storage path, file size, and thumbnail path in database
        db_file.storage_path = storage_path  # type: ignore[assignment]
        db_file.file_size = file_size  # type: ignore[assignment]

        # Per-file skip summary: mark as disabled before pipeline starts
        if skip_summary:
            db_file.summary_status = "disabled"  # type: ignore[assignment]

        with benchmark_timing.stage(task_id, "db_commit"):
            db.commit()
            db.refresh(db_file)

        # Capture file context for later reporting (persists until flushed
        # into file_pipeline_timing by finalize_transcription).
        benchmark_timing.set_context(
            task_id,
            {
                "file_size_bytes": int(file_size),
                "content_type": file.content_type or "",
                "http_flow": "legacy",
            },
        )

        # Fire the thumbnail + transcription pipeline via the shared dispatch
        # tail so this route stays in lockstep with the presigned /complete one.
        dispatch_upload_pipeline(
            db_file,
            user_id=current_user.id,
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

    except HTTPException:
        # Re-raise HTTP exceptions after cleanup
        db.delete(db_file)
        db.commit()
        raise
    except Exception as e:
        # Clean up on failure
        db.delete(db_file)
        db.commit()
        logger.error(f"Upload failed: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error during file upload: {str(e)}",
        ) from e
    finally:
        # Close the spool on every path. Once it has rolled over to disk this also
        # unlinks the backing file, so a failed multi-GB upload cannot leave one
        # behind (issue #284 A1.20).
        if spooled_upload is not None:
            with contextlib.suppress(Exception):
                spooled_upload.close()
