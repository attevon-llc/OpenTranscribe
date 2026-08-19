import logging

from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def get_file_metadata(file_id: int) -> dict:
    """Get basic file metadata for notifications."""
    try:
        with session_scope() as db:
            media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
            if media_file:
                return {
                    "file_uuid": str(media_file.uuid),  # Include UUID
                    "filename": media_file.filename,
                    "content_type": media_file.content_type,
                    "file_size": media_file.file_size,
                }
    except Exception as e:
        logger.warning(f"Failed to get file metadata for file {file_id}: {e}")

    # Return minimal data if query fails
    return {
        "file_uuid": None,  # Unknown UUID
        "filename": f"File {file_id}",
        "content_type": "unknown",
        "file_size": 0,
    }


def send_notification_via_redis(
    user_id: int, file_id: int, status: FileStatus, message: str, progress: int = 0
) -> bool:
    """
    Send notification via Redis pub/sub from synchronous context (like Celery worker).

    Args:
        user_id: User ID
        file_id: File ID
        status: File status
        message: Status message
        progress: Progress percentage

    Returns:
        True if notification was sent successfully, False otherwise
    """
    try:
        # Get file metadata
        file_metadata = get_file_metadata(file_id)

        # Prepare notification data
        data = {
            "file_id": file_metadata.get("file_uuid"),  # Use UUID from metadata
            "status": status.value,
            "message": message,
            "progress": progress,
            "filename": file_metadata["filename"],
            "content_type": file_metadata["content_type"],
            "file_size": file_metadata["file_size"],
        }

        result = send_ws_event(user_id, "transcription_status", data)

        # Invalidate caches so gallery / status page reflect the new state
        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_user_files(user_id)
        except Exception as cache_err:
            logger.debug(f"Cache invalidation after status change failed: {cache_err}")

        return result

    except Exception as e:
        logger.error(f"Failed to send notification via Redis for file {file_id}: {e}")
        return False


def send_notification_with_retry(
    user_id: int,
    file_id: int,
    status: FileStatus,
    message: str,
    progress: int = 0,
    max_retries: int = 3,
) -> bool:
    """
    Send notification with retry logic.

    Args:
        user_id: User ID
        file_id: File ID
        status: File status
        message: Status message
        progress: Progress percentage
        max_retries: Maximum number of retry attempts

    Returns:
        True if notification was eventually sent, False if all retries failed
    """
    for retry in range(max_retries):
        try:
            success = send_notification_via_redis(user_id, file_id, status, message, progress)
            if success:
                logger.info(
                    f"Successfully sent notification for file {file_id} on attempt {retry + 1}"
                )
                return True
            else:
                logger.warning(f"Failed to send notification (attempt {retry + 1}/{max_retries})")
        except Exception as e:
            logger.warning(f"Failed to send notification (attempt {retry + 1}/{max_retries}): {e}")

        if retry < max_retries - 1:  # Don't sleep on the last attempt
            import time

            time.sleep(0.2)  # Short delay before retry

    logger.error(f"Failed to send notification for file {file_id} after {max_retries} attempts")
    return False


def send_processing_notification(user_id: int, file_id: int) -> None:
    """Send processing started notification."""
    send_notification_with_retry(
        user_id, file_id, FileStatus.PROCESSING, "Transcription started", progress=10
    )


def send_progress_notification(user_id: int, file_id: int, progress: float, message: str) -> None:
    """Send progress update notification."""
    progress_percent = int(progress * 100)
    send_notification_with_retry(
        user_id, file_id, FileStatus.PROCESSING, message, progress=progress_percent
    )


def send_transcript_ready_notification(user_id: int, file_id: int) -> None:
    """Tell the client the transcript is durable and readable, ahead of completion.

    Segments are committed and the read endpoints ungated well before the job finishes —
    speaker matching, indexing and enrichment all still follow. Announcing it here lets the
    reader open the transcript at that moment rather than waiting out the rest, with speaker
    labels attaching in place afterwards through the existing ``speaker_updated`` events.

    Deliberately its own event type: ``transcription_status`` carries the job lifecycle, and a
    client treating this as "finished" would stop the progress UI early. Best effort — a
    transcript that fails to announce itself is still shown at completion.
    """
    try:
        from app.utils import benchmark_timing

        if benchmark_timing.benchmark_enabled():
            with session_scope() as db:
                mf = db.query(MediaFile).filter(MediaFile.id == file_id).first()
                active_task_id = str(mf.active_task_id) if mf and mf.active_task_id else None
            benchmark_timing.mark(active_task_id, "transcript_ready")
    except Exception as e:
        logger.debug(f"transcript_ready mark failed for file {file_id}: {e}")

    try:
        file_metadata = get_file_metadata(file_id)
        send_ws_event(
            user_id,
            "transcript_ready",
            {
                "file_id": file_metadata.get("file_uuid"),
                "filename": file_metadata["filename"],
            },
        )
    except Exception as e:
        logger.debug(f"transcript_ready notification failed for file {file_id}: {e}")


def send_completion_notification(user_id: int, file_id: int) -> None:
    """Send transcription completed notification.

    Marks ``completion_notified`` in the benchmark hash immediately before
    the WebSocket event fires so the user-perceived "done" timestamp is
    captured accurately. We resolve the active task_id from the MediaFile
    row rather than threading it through every caller — that's the single
    source of truth for the task currently driving this file.
    """
    # Best-effort benchmark marker (no-op when ENABLE_BENCHMARK_TIMING=false).
    try:
        from app.utils import benchmark_timing

        if benchmark_timing.benchmark_enabled():
            with session_scope() as db:
                mf = db.query(MediaFile).filter(MediaFile.id == file_id).first()
                active_task_id = str(mf.active_task_id) if mf and mf.active_task_id else None
            benchmark_timing.mark(active_task_id, "completion_notified")
    except Exception as e:
        logger.debug(f"completion_notified mark failed for file {file_id}: {e}")

    send_notification_with_retry(
        user_id,
        file_id,
        FileStatus.COMPLETED,
        "Transcription completed successfully",
        progress=100,
    )

    # Also send a file_updated notification to refresh the gallery item
    try:
        # Get updated file data for gallery
        with session_scope() as db:
            media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
            if media_file:
                # Import formatting service to include display_status
                from app.services.formatting_service import FormattingService

                # Create file data for gallery update with all formatted fields
                file_data = {
                    "id": str(media_file.uuid),  # Use UUID for frontend
                    "filename": media_file.filename,
                    "status": media_file.status.value if media_file.status else "completed",
                    "content_type": media_file.content_type,
                    "file_size": media_file.file_size,
                    "title": media_file.title,
                    "author": media_file.author,
                    "duration": media_file.duration,
                    "thumbnail_url": f"/api/files/{media_file.uuid}/thumbnail"  # Use UUID in URL
                    if media_file.thumbnail_path
                    else None,
                    "upload_time": media_file.upload_time.isoformat()
                    if media_file.upload_time
                    else None,
                    # ASR provider metadata — allows the frontend to display
                    # "Transcribed by Deepgram Nova-3" without a follow-up API call.
                    "asr_provider": media_file.asr_provider,
                    "asr_model": media_file.asr_model,
                    # Add formatted fields that the frontend expects
                    "formatted_duration": FormattingService.format_duration(media_file.duration),
                    "formatted_upload_date": FormattingService.format_upload_date(
                        media_file.upload_time
                    ),
                    "formatted_file_age": FormattingService.format_file_age(media_file.upload_time),
                    "formatted_file_size": FormattingService.format_bytes_detailed(
                        media_file.file_size
                    ),
                    "display_status": FormattingService.format_status(media_file.status),
                    "status_badge_class": FormattingService.get_status_badge_class(
                        media_file.status.value
                    ),
                }

                # Send file_updated notification
                send_ws_event(
                    user_id,
                    "file_updated",
                    {
                        "file_id": str(media_file.uuid),  # Use UUID
                        "file": file_data,
                        "status": "completed",
                        "message": "File processing completed",
                    },
                )
                logger.info(f"Sent file_updated notification for completed file {file_id}")

    except Exception as e:
        logger.error(f"Failed to send file_updated notification for file {file_id}: {e}")


def send_error_notification(user_id: int, file_id: int, error_message: str) -> None:
    """Send transcription error notification."""
    send_notification_with_retry(
        user_id,
        file_id,
        FileStatus.ERROR,
        f"Transcription failed: {error_message}",
        progress=0,
    )
