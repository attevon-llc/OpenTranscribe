"""
Media URL processing task for background video downloading and processing.

This module provides Celery tasks for handling media video downloads and processing
asynchronously to prevent UI blocking during video import operations. It includes
progress tracking, error handling, and automatic transcription initiation.

Supports YouTube, Vimeo, Twitter/X, TikTok, and many other platforms via yt-dlp.
"""

import logging
import uuid as uuid_lib
from typing import TypedDict

from celery.exceptions import Retry

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import DEFAULT_AUDIO_QUALITY
from app.core.constants import DEFAULT_VIDEO_QUALITY
from app.core.queues import DownloadPriority
from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.formatting_service import FormattingService
from app.services.media_download_service import MediaDownloadService
from app.tasks.transcription import dispatch_transcription_pipeline
from app.utils import benchmark_timing
from app.utils.error_classification import RETRIABLE_CATEGORIES
from app.utils.error_classification import categorize_error
from app.utils.task_utils import update_media_file_status
from app.utils.websocket_notify import send_ws_event_for_file

logger = logging.getLogger(__name__)


def _get_user_download_settings(db, user_id: int) -> dict[str, str | bool]:
    """Look up user's saved download quality preferences from the database.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        Dict with video_quality, audio_only, and audio_quality values.
    """
    from app.models.prompt import UserSetting

    rows = (
        db.query(UserSetting)
        .filter(
            UserSetting.user_id == user_id,
            UserSetting.setting_key.in_(
                ["download_video_quality", "download_audio_only", "download_audio_quality"]
            ),
        )
        .all()
    )
    settings_map = {str(row.setting_key): str(row.setting_value) for row in rows}
    return {
        "video_quality": settings_map.get("download_video_quality", DEFAULT_VIDEO_QUALITY),
        "audio_only": settings_map.get("download_audio_only", "false").lower() == "true",
        "audio_quality": settings_map.get("download_audio_quality", DEFAULT_AUDIO_QUALITY),
    }


def _resolve_download_quality(
    db,
    user_id: int,
    video_quality: str | None,
    audio_only: bool | None,
    audio_quality: str | None,
) -> tuple[str, bool, str]:
    """Resolve download quality: explicit overrides → user DB prefs → system defaults.

    Args:
        db: Database session.
        user_id: User ID.
        video_quality: Optional per-download override.
        audio_only: Optional per-download override.
        audio_quality: Optional per-download override.

    Returns:
        Tuple of (video_quality, audio_only, audio_quality) with all values resolved.
    """
    if video_quality is not None and audio_only is not None and audio_quality is not None:
        return video_quality, audio_only, audio_quality

    prefs = _get_user_download_settings(db, user_id)
    return (
        video_quality if video_quality is not None else str(prefs["video_quality"]),
        audio_only if audio_only is not None else bool(prefs["audio_only"]),
        audio_quality if audio_quality is not None else str(prefs["audio_quality"]),
    )


def send_youtube_notification_via_redis(
    user_id: int, file_id: int, status: FileStatus, message: str, progress: int = 0
) -> bool:
    """Send YouTube processing notification via WebSocket."""
    from app.services.notification_service import send_task_notification

    return send_task_notification(
        user_id,
        "youtube_processing_status",
        status=status.value,
        message=message,
        file_id=file_id,
        progress=progress,
    )


class YouTubeProcessingResult(TypedDict):
    """Result structure for YouTube processing task."""

    status: str  # "success" or "error"
    message: str
    file_id: int


@celery_app.task(
    name="download.media_url",
    bind=True,
    priority=DownloadPriority.SINGLE_URL,
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    rate_limit=settings.YOUTUBE_DOWNLOAD_RATE_LIMIT or None,
)
def process_youtube_url_task(
    self,
    url: str,
    user_id: int,
    file_uuid: str,
    media_username: str | None = None,
    media_password: str | None = None,
    collection_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    video_quality: str | None = None,
    audio_only: bool | None = None,
    audio_quality: str | None = None,
) -> YouTubeProcessingResult:
    """Background task to process YouTube URL by downloading and creating media file.

    This task handles asynchronous YouTube video processing to prevent UI blocking.
    It downloads the video, updates the media file record, and starts transcription.

    Quality settings are resolved in this order:
    1. Explicit per-download overrides (if provided by the frontend)
    2. User's saved preferences from the database
    3. System defaults (best quality)

    Args:
        self: Celery task instance (automatically passed when bind=True).
        url: YouTube URL to process.
        user_id: ID of the user who initiated the request.
        file_uuid: UUID of the MediaFile record to update.
        media_username: Optional username for authenticated media downloads.
        media_password: Optional password for authenticated media downloads.
        video_quality: Optional video quality override (e.g., "best", "1080p", "720p").
            If None, uses user's saved preference from the database.
        audio_only: Optional override to download only audio (no video stream).
            If None, uses user's saved preference from the database.
        audio_quality: Optional audio quality override (e.g., "best", "192", "128").
            If None, uses user's saved preference from the database.

    Returns:
        Dict: Processing result containing status, message, and file_id.
              Format: {"status": "success|error", "message": str, "file_id": int}

    Raises:
        Exception: Any error during processing will be caught and returned in result dict.
    """
    from app.services.uuid_helpers import get_file_by_uuid

    file_id = None  # Initialize to handle exceptions before file_id is assigned

    # Mint the application task_id at task ingress so every subsequent stage
    # (download, thumbnail, MinIO put, DB commit, preprocess, GPU, postprocess)
    # writes into a single benchmark:{task_id} Redis hash. Phase 2 PR #9: URL
    # ingress parity with the direct-upload path.
    task_id = str(uuid_lib.uuid4())
    benchmark_timing.mark(task_id, "http_request_received")
    benchmark_timing.set_context(
        task_id,
        {
            "http_flow": "url",
            "asr_provider": "yt-dlp",
        },
    )

    try:
        logger.info(f"Starting YouTube processing task for URL: {url}, file_uuid: {file_uuid}")

        # Apply pre-download jitter (random delay to avoid bot detection)
        if settings.YOUTUBE_PRE_DOWNLOAD_JITTER_ENABLED:
            import random
            import time

            jitter_seconds = random.uniform(  # noqa: S311  # nosec B311
                settings.YOUTUBE_PRE_DOWNLOAD_JITTER_MIN_SECONDS,
                settings.YOUTUBE_PRE_DOWNLOAD_JITTER_MAX_SECONDS,
            )

            logger.info(f"Applying pre-download jitter: {jitter_seconds:.1f}s")
            time.sleep(jitter_seconds)

        # Resolve UUID to internal integer ID in a short-lived session.
        # A separate session is used intentionally: the notification below
        # requires file_id, and the second session fetches fresh state
        # (including any changes committed by other workers in between).
        with session_scope() as db:
            media_file = get_file_by_uuid(db, file_uuid)
            file_id = int(media_file.id)

        # Send initial processing notification (needs file_id from above)
        send_youtube_notification_via_redis(
            user_id=user_id,
            file_id=file_id,
            status=FileStatus.DOWNLOADING,
            message="Starting YouTube video download...",
            progress=10,
        )

        # Open a new session with fresh state for the main processing work
        with session_scope() as db:
            # Get the user and media file records
            user = db.query(User).filter(User.id == user_id).first()
            media_file = get_refreshed_object(db, MediaFile, file_id)

            # Resolve download quality: explicit overrides → user DB prefs → defaults
            video_quality, audio_only, audio_quality = _resolve_download_quality(
                db,
                user_id,
                video_quality,
                audio_only,
                audio_quality,
            )

            if not user or not media_file:
                logger.error(f"User {user_id} or MediaFile {file_id} not found")
                send_youtube_notification_via_redis(
                    user_id=user_id,
                    file_id=file_id,
                    status=FileStatus.ERROR,
                    message="User or file record not found",
                    progress=0,
                )
                return {
                    "status": "error",
                    "message": "User or file record not found",
                    "file_id": file_id if file_id is not None else 0,
                }

            # Update database status to DOWNLOADING (was QUEUED for playlist placeholders)
            media_file.status = FileStatus.DOWNLOADING  # type: ignore[assignment]
            db.commit()

            media_service = MediaDownloadService()

            # Process the YouTube URL
            try:
                # Create progress callback
                def progress_callback(progress, message):
                    send_youtube_notification_via_redis(
                        user_id=user_id,
                        file_id=file_id,
                        status=FileStatus.DOWNLOADING,
                        message=message,
                        progress=progress,
                    )

                # Process using synchronous version
                updated_media_file = media_service.process_media_url_sync(
                    url=url,
                    db=db,
                    user=user,
                    media_file=media_file,
                    progress_callback=progress_callback,
                    media_username=media_username,
                    media_password=media_password,
                    video_quality=video_quality,
                    audio_only=audio_only,
                    audio_quality=audio_quality,
                    benchmark_task_id=task_id,
                )

                # Update status to pending for transcription
                updated_media_file.status = FileStatus.PENDING  # type: ignore[assignment]
                with benchmark_timing.stage(task_id, "db_commit"):
                    db.commit()

                # Apply collections and tags if specified
                if collection_ids or tag_names:
                    from app.api.endpoints.files.prepare_upload import add_file_to_collections
                    from app.api.endpoints.files.prepare_upload import add_tags_to_file

                    if collection_ids:
                        from uuid import UUID as _UUID

                        add_file_to_collections(
                            db, file_id, user_id, [_UUID(c) for c in collection_ids]
                        )
                    if tag_names:
                        # Background task: the tag belongs to the file owner.
                        add_tags_to_file(db, file_id, tag_names, user_id)
                    db.commit()

                # Send completion notification
                send_youtube_notification_via_redis(
                    user_id=user_id,
                    file_id=file_id,
                    status=FileStatus.PENDING,
                    message="YouTube download complete, starting transcription...",
                    progress=100,
                )

                # Also send file_updated notification to refresh the gallery with thumbnail
                try:
                    # Get updated file data for gallery
                    updated_media_file_refreshed = get_refreshed_object(db, MediaFile, file_id)
                    if updated_media_file_refreshed:
                        # Create file data for gallery update
                        file_data = {
                            "id": str(updated_media_file_refreshed.uuid),  # Use UUID for frontend
                            "filename": str(updated_media_file_refreshed.filename),
                            "status": updated_media_file_refreshed.status.value
                            if updated_media_file_refreshed.status
                            else "pending",
                            "display_status": FormattingService.format_status(
                                updated_media_file_refreshed.status  # type: ignore[arg-type]
                            )
                            if updated_media_file_refreshed.status
                            else "Pending",
                            "content_type": str(updated_media_file_refreshed.content_type),
                            "file_size": int(updated_media_file_refreshed.file_size),
                            "title": str(updated_media_file_refreshed.title)
                            if updated_media_file_refreshed.title
                            else None,
                            "author": str(updated_media_file_refreshed.author)
                            if updated_media_file_refreshed.author
                            else None,
                            "duration": float(updated_media_file_refreshed.duration)
                            if updated_media_file_refreshed.duration
                            else None,
                            "thumbnail_url": f"/api/files/{updated_media_file_refreshed.uuid}/thumbnail"  # Use UUID in URL
                            if updated_media_file_refreshed.thumbnail_path
                            else None,
                            "upload_time": updated_media_file_refreshed.upload_time.isoformat()
                            if updated_media_file_refreshed.upload_time
                            else None,
                        }

                        # Send file_updated notification
                        send_ws_event_for_file(
                            user_id,
                            "file_updated",
                            {
                                "file_id": str(updated_media_file_refreshed.uuid),  # Use UUID
                                "file": file_data,
                                "status": updated_media_file_refreshed.status.value
                                if updated_media_file_refreshed.status
                                else "pending",
                                "display_status": FormattingService.format_status(
                                    updated_media_file_refreshed.status  # type: ignore[arg-type]
                                )
                                if updated_media_file_refreshed.status
                                else "Pending",
                                "message": "YouTube processing completed",
                            },
                            file_id=file_id,
                        )
                        logger.info(
                            f"Sent file_updated notification for YouTube completion: {file_id}"
                        )

                except Exception as e:
                    logger.error(
                        f"Failed to send file_updated notification for YouTube completion {file_id}: {e}"
                    )

                # Dispatch the 3-stage pipeline, threading our ingress task_id
                # through so every downstream stage writes into the same
                # benchmark:{task_id} Redis hash we've been populating.
                # Waveform generation fires from the preprocess stage once the
                # 16 kHz WAV is staged in MinIO temp — avoids re-downloading
                # the original media (Phase 2 PR #3).
                try:
                    benchmark_timing.mark(task_id, "http_response_end")
                    dispatch_transcription_pipeline(
                        file_uuid=file_uuid, task_id=task_id, reuse_requested_options=True
                    )
                    logger.info(
                        f"Dispatched pipeline chain for MediaFile {file_id} (task_id={task_id})"
                    )
                except Exception as e:
                    # Download DID succeed, but nothing will ever transcribe this file
                    # (dispatch marks it ERROR — #865/#906) — reporting "success" here
                    # contradicts the row this task just wrote.
                    logger.error(
                        f"Downloaded {file_id} but could not start transcription: {e}",
                        exc_info=True,
                    )
                    return {
                        "status": "error",
                        "message": f"Download complete, but transcription could not be started: {e}",
                        "file_id": file_id if file_id is not None else 0,
                    }

                return {
                    "status": "success",
                    "message": "YouTube processing completed",
                    "file_id": file_id if file_id is not None else 0,
                }

            except Exception as e:
                error_msg = str(e)
                error_category = categorize_error(error_msg)

                # Check if this is a retriable error using the canonical set
                is_retriable = error_category in RETRIABLE_CATEGORIES

                if (
                    is_retriable
                    and self.request.retries < self.max_retries
                    and settings.YOUTUBE_AUTO_RETRY_ENABLED
                ):
                    # Calculate backoff: 30s, 120s, 480s
                    countdown = 30 * (2**self.request.retries)
                    logger.warning(
                        f"Download attempt {self.request.retries + 1}/{self.max_retries} "
                        f"failed for {url}: {error_msg}. "
                        f"Retrying in {countdown}s..."
                    )

                    # Update status to show retry is pending
                    media_file.last_error_message = (  # type: ignore[assignment]
                        f"Retry {self.request.retries + 1}/{self.max_retries}: {error_msg}"
                    )
                    db.flush()

                    send_youtube_notification_via_redis(
                        user_id=user_id,
                        file_id=file_id,
                        status=FileStatus.PROCESSING,
                        message=f"Download failed, retrying in {countdown}s...",
                        progress=5,
                    )

                    # Raise to trigger Celery retry (non-blocking, frees worker)
                    raise self.retry(countdown=countdown, exc=e) from None

                # Non-retriable error or last attempt failed
                logger.error(
                    f"Download failed after {self.request.retries + 1} attempts "
                    f"for {url}: {error_msg}"
                )

                media_file.last_error_message = error_msg  # type: ignore[assignment]
                media_file.error_category = error_category.value  # type: ignore[assignment]
                db.flush()
                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)

                # Send error notification
                send_youtube_notification_via_redis(
                    user_id=user_id,
                    file_id=file_id,
                    status=FileStatus.ERROR,
                    message=f"YouTube processing failed: {error_msg}",
                    progress=0,
                )

                return {
                    "status": "error",
                    "message": error_msg,
                    "file_id": file_id if file_id is not None else 0,
                }

    except Retry:
        # self.retry() above raises celery.exceptions.Retry to ask the worker
        # to reschedule this task. It is an Exception subclass, so it must be
        # re-raised here before the generic handler below — otherwise this
        # outer except swallows it, converts it into an ordinary error
        # response, and Celery's tracer never sees the retry request at all.
        raise
    except Exception as e:
        logger.error(f"Unexpected error in YouTube processing task: {e}")

        # Send error notification only if file_id was assigned
        if file_id is not None:
            send_youtube_notification_via_redis(
                user_id=user_id,
                file_id=file_id,
                status=FileStatus.ERROR,
                message=f"Unexpected error: {str(e)}",
                progress=0,
            )

        return {
            "status": "error",
            "message": str(e),
            "file_id": file_id if file_id is not None else 0,
        }
