"""Post-commit dispatch tail shared by every upload ingest path.

Fires the background thumbnail, dispatches the transcription pipeline, and — when
that dispatch fails after the bytes are stored — leaves a visible ERROR row instead
of a deleted one. Used by the legacy multipart route (``upload.py``), the presigned
``/complete`` route, watch-source import, storage recovery and MinIO re-ingest.
"""

import contextlib
import logging

from sqlalchemy.orm import Session

from app.core.env import env_bool
from app.core.exceptions import DomainError
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.utils.websocket_notify import send_ws_event_for_file

logger = logging.getLogger(__name__)


def start_transcription_task(
    file_id: int,
    file_uuid: str,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    whisper_model: str | None = None,
    disable_diarization: bool | None = None,
    task_id: str | None = None,
    reuse_requested_options: bool = False,
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
    if not env_bool("SKIP_CELERY"):
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
            reuse_requested_options=reuse_requested_options,
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
    disable_diarization: bool | None = None,
) -> str | None:
    """Shared post-commit dispatch tail for BOTH upload ingest paths.

    Fires the background thumbnail and dispatches the transcription pipeline.
    The legacy multipart and presigned ``/complete`` routes both call this so
    the "what happens after the bytes land" logic lives in ONE place — the
    thumbnail/validation gaps came from these two tails being hand-copied and
    drifting. Call AFTER the row is committed. Any option the caller leaves ``None`` falls
    back to what ``/prepare`` recorded on the row (model, speaker range, skipped
    diarization), and a model stored before the deployment locked model choice is dropped.
    """
    dispatch_thumbnail_for_video(db_file, user_id)
    return start_transcription_task(
        db_file.id,
        str(db_file.uuid),
        min_speakers,
        max_speakers,
        num_speakers,
        whisper_model=whisper_model,
        disable_diarization=disable_diarization,
        task_id=task_id,
        reuse_requested_options=True,
    )


def dispatch_upload_pipeline_or_mark_error(
    db: Session,
    db_file: MediaFile,
    *,
    user_id: int,
    whisper_model: str | None,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    task_id: str | None,
    disable_diarization: bool | None = None,
) -> str | None:
    """``dispatch_upload_pipeline``, but a failure leaves a VISIBLE row (issue #905).

    Call only AFTER the row is committed with its ``storage_path`` — at that point
    the bytes are in object storage, so a dispatch failure is a PROCESSING
    failure, surfaced like every other one in this app (ERROR + ``last_error_message``)
    instead of the row being deleted out from under an upload that actually landed.
    Re-raises unchanged so ``ASRConfigurationError`` still reaches ``main.py``'s 503
    handler with the real message.
    """
    file_id = int(db_file.id)
    try:
        return dispatch_upload_pipeline(
            db_file,
            user_id=user_id,
            whisper_model=whisper_model,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            num_speakers=num_speakers,
            task_id=task_id,
            disable_diarization=disable_diarization,
        )
    except Exception as exc:
        _mark_upload_dispatch_failed(db, file_id, user_id, exc)
        raise


def _mark_upload_dispatch_failed(db: Session, file_id: int, user_id: int, exc: Exception) -> None:
    """Persist ERROR + ``last_error_message`` on a stored-but-undispatchable upload.

    Best-effort, never raises: the caller re-raises the real failure either way.
    """
    from app.utils.task_utils import update_media_file_status

    text = str(exc) if isinstance(exc, DomainError) else getattr(exc, "message", None)
    message = text or str(exc) or exc.__class__.__name__
    try:
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if media_file is None:
            return
        media_file.last_error_message = message  # type: ignore[assignment]
        update_media_file_status(db, file_id, FileStatus.ERROR)  # takedown-aware writer, #824
    except Exception:
        logger.exception("Could not mark file %s ERROR after a dispatch failure", file_id)
        with contextlib.suppress(Exception):
            db.rollback()
        return

    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_user_files(user_id)
    except Exception as cache_err:
        logger.debug(f"Cache invalidation after a dispatch failure failed: {cache_err}")

    try:
        _send_dispatch_failed_ws_event(media_file, user_id, message)
    except Exception as ws_err:
        logger.debug(f"WS notification after a dispatch failure failed: {ws_err}")


def _send_dispatch_failed_ws_event(media_file: MediaFile, user_id: int, message: str) -> None:
    """Best-effort LIVE gallery push for a post-storage dispatch failure (issue #911).

    ``_mark_upload_dispatch_failed``'s Redis cache invalidation is enough for a page
    refresh/poll to eventually show the row at ERROR, but a client with an open
    WebSocket connection would otherwise wait on its own poll schedule instead of
    updating instantly, unlike every other status transition in the app.

    Shape matches the ``file_updated`` event ``send_completion_notification``
    (``tasks/transcription/notifications.py``) sends on the success path, so the
    SPA's existing gallery handler needs no new branch — same field set, an ERROR
    status and the sanitized error fields (``error_reason``/``user_message``/
    ``error_suggestions``/``is_retryable``) in place of a completed one.

    ⚠️ Never put the raw ``message`` on this payload — it is the only path by which
    raw error text has ever reached the gallery (issue #786): this event's ``file``
    is spread wholesale into the gallery's file object client-side.
    """
    from app.services.error_categorization_service import ErrorCategorizationService
    from app.services.formatting_service import FormattingService

    error_info = ErrorCategorizationService.get_error_info(message)
    file_data = {
        "id": str(media_file.uuid),
        "filename": media_file.filename,
        "status": FileStatus.ERROR.value,
        "content_type": media_file.content_type,
        "file_size": media_file.file_size,
        "error_reason": error_info["category"],
        "user_message": error_info["user_message"],
        "error_suggestions": error_info["suggestions"],
        "is_retryable": error_info["is_retryable"],
        "formatted_duration": FormattingService.format_duration(media_file.duration),
        "formatted_upload_date": FormattingService.format_upload_date(media_file.upload_time),
        "formatted_file_age": FormattingService.format_file_age(media_file.upload_time),
        "formatted_file_size": FormattingService.format_bytes_detailed(media_file.file_size),
        "display_status": FormattingService.format_status(media_file.status),
        "status_badge_class": FormattingService.get_status_badge_class(FileStatus.ERROR.value),
    }
    send_ws_event_for_file(
        user_id,
        "file_updated",
        {
            "file_id": str(media_file.uuid),
            "file": file_data,
            "status": FileStatus.ERROR.value,
            "message": message,
        },
        file_id=int(media_file.id),
    )
