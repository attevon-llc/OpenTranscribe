"""
Media playlist processing task: expand a playlist URL into per-video downloads.

Creates placeholder MediaFile records for each playlist entry and dispatches
``process_youtube_url_task`` (in ``youtube_processing``) for each one with staggered
delays. Split out of ``youtube_processing`` so each module holds one task.
"""

import logging
from typing import TypedDict

from app.core.celery import celery_app
from app.core.queues import DownloadPriority
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.formatting_service import FormattingService
from app.services.media_download_service import MediaDownloadService
from app.tasks.youtube_processing import process_youtube_url_task
from app.utils.error_classification import categorize_error
from app.utils.task_utils import update_media_file_status
from app.utils.websocket_notify import send_ws_event_for_file

logger = logging.getLogger(__name__)


class YouTubePlaylistProcessingResult(TypedDict):
    """Result structure for YouTube playlist processing task."""

    status: str  # "success" or "error"
    message: str
    created_count: int
    skipped_count: int
    total_videos: int


def _send_playlist_notification(
    user_id: int,
    status: str,
    message: str,
    progress: int,
    extra_data: dict | None = None,
) -> None:
    """Send playlist processing notification via WebSocket.

    Args:
        user_id: User ID to send notification to.
        status: Notification status (processing, completed, error).
        message: Status message.
        progress: Progress percentage (0-100).
        extra_data: Optional additional data to include in notification.
    """
    try:
        from app.services.notification_service import send_task_notification

        send_task_notification(
            user_id,
            "playlist_processing_status",
            status=status,
            message=message,
            progress=progress,
            extra=extra_data,
        )
    except Exception as e:
        logger.error(f"Failed to send playlist notification: {e}")


def _create_playlist_error_result(message: str) -> YouTubePlaylistProcessingResult:
    """Create an error result for playlist processing.

    Args:
        message: Error message.

    Returns:
        YouTubePlaylistProcessingResult with error status.
    """
    return {
        "status": "error",
        "message": message,
        "created_count": 0,
        "skipped_count": 0,
        "total_videos": 0,
    }


def _notify_playlist_file_created(user_id: int, media_file: MediaFile) -> None:
    """Send file_created notification (differs from url_video_import's: arg order, payload keys).

    Args:
        user_id: User ID to send notification to.
        media_file: MediaFile instance to notify about.
    """
    try:
        file_data = {
            "id": str(media_file.uuid),
            "filename": str(media_file.filename),
            "status": media_file.status.value if media_file.status else "processing",
            "display_status": FormattingService.format_status(media_file.status)  # type: ignore[arg-type]
            if media_file.status
            else "Processing",
            "content_type": str(media_file.content_type),
            "file_size": int(media_file.file_size),
            "title": str(media_file.title) if media_file.title else None,
            "author": str(media_file.author) if media_file.author else None,
            "duration": float(media_file.duration) if media_file.duration else None,
            "upload_time": media_file.upload_time.isoformat() if media_file.upload_time else None,
        }

        send_ws_event_for_file(
            user_id,
            "file_created",
            {
                "file_id": str(media_file.uuid),
                "file": file_data,
            },
            file_uuid=media_file.uuid,
        )
    except Exception as e:
        logger.error(f"Failed to send file_created notification for video {media_file.id}: {e}")


def _dispatch_playlist_video_task(
    media_file: MediaFile,
    user_id: int,
    db,
    countdown: int = 0,
    collection_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    video_quality: str | None = None,
    audio_only: bool | None = None,
    audio_quality: str | None = None,
) -> bool:
    """Dispatch one playlist video (countdown, returns bool; url_video_import's raises instead).

    Args:
        media_file: MediaFile instance to process.
        user_id: User ID who initiated the request.
        db: Database session.
        countdown: Delay in seconds before task starts (default: 0).
        collection_ids: Optional collection UUIDs to assign after processing.
        tag_names: Optional tag names to apply after processing.
        video_quality: Optional video quality override for this download.
        audio_only: Optional override to download only audio.
        audio_quality: Optional audio bitrate override for this download.

    Returns:
        True if task was dispatched successfully, False otherwise.
    """
    try:
        video_url = str(media_file.source_url)

        kwargs: dict = {"collection_ids": collection_ids, "tag_names": tag_names}
        if video_quality is not None:
            kwargs["video_quality"] = video_quality
        if audio_only is not None:
            kwargs["audio_only"] = audio_only
        if audio_quality is not None:
            kwargs["audio_quality"] = audio_quality

        # Use apply_async to support countdown parameter
        process_youtube_url_task.apply_async(
            args=[video_url, user_id, str(media_file.uuid)],
            kwargs=kwargs,
            countdown=countdown,
        )

        logger.info(f"Dispatched task for '{media_file.title}' with {countdown}s delay")
        return True
    except Exception as e:
        logger.error(f"Failed to dispatch task for video {media_file.title}: {e}")
        error_msg = f"Failed to start processing: {str(e)}"
        media_file.last_error_message = error_msg  # type: ignore[assignment]
        media_file.error_category = categorize_error(error_msg).value  # type: ignore[assignment]
        db.flush()
        update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
        return False


@celery_app.task(name="download.media_playlist", bind=True, priority=DownloadPriority.PLAYLIST)
def process_youtube_playlist_task(
    self,
    url: str,
    user_id: int,
    collection_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    video_quality: str | None = None,
    audio_only: bool | None = None,
    audio_quality: str | None = None,
    organization_id: int | None = None,
    transcription_options: dict | None = None,
) -> YouTubePlaylistProcessingResult:
    """Background task to process YouTube playlist by extracting videos and dispatching individual tasks.

    This task handles asynchronous YouTube playlist processing:
    1. Extracts playlist metadata and video list
    2. Creates placeholder MediaFile records for each video
    3. Dispatches individual process_youtube_url_task for each video
    4. Sends progress notifications

    Args:
        self: Celery task instance (automatically passed when bind=True).
        url: YouTube playlist URL to process.
        user_id: ID of the user who initiated the request.
        collection_ids: Optional collection UUIDs to assign to each video.
        tag_names: Optional tag names to apply to each video.
        video_quality: Optional video quality override for playlist downloads.
        audio_only: Optional override to download only audio.
        audio_quality: Optional audio bitrate override for playlist downloads.
        organization_id: The ORIGINATING request's tenant (``ctx.org_id``),
            threaded through the task kwargs (issue #262c). Placeholders are
            stamped with this — never a first-membership guess. None =
            personal scope (always the case in the community edition).
        transcription_options: Per-file speaker range / model the user chose for the
            import (``min_speakers``, ``max_speakers``, ``num_speakers``,
            ``whisper_model``); recorded on every created file.

    Returns:
        Dict: Processing result containing status, message, and video counts.
              Format: {"status": "success|error", "message": str, "created_count": int,
                      "skipped_count": int, "total_videos": int}

    Raises:
        Exception: Any error during processing will be caught and returned in result dict.
    """
    logger.info(f"Starting YouTube playlist processing task for URL: {url}")
    _send_playlist_notification(user_id, "processing", "Extracting playlist information...", 5)

    try:
        return _process_playlist_with_db(
            url,
            user_id,
            collection_ids,
            tag_names,
            video_quality,
            audio_only,
            audio_quality,
            organization_id=organization_id,
            transcription_options=transcription_options,
        )
    except Exception as e:
        logger.error(f"Unexpected error in YouTube playlist processing task: {e}")
        _send_playlist_notification(user_id, "error", f"Unexpected error: {str(e)}", 0)
        return _create_playlist_error_result(str(e))


def _process_playlist_with_db(
    url: str,
    user_id: int,
    collection_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    video_quality: str | None = None,
    audio_only: bool | None = None,
    audio_quality: str | None = None,
    organization_id: int | None = None,
    transcription_options: dict | None = None,
) -> YouTubePlaylistProcessingResult:
    """Process playlist within a database session.

    Args:
        url: YouTube playlist URL.
        user_id: User ID.
        collection_ids: Optional collection UUIDs to assign to each video.
        tag_names: Optional tag names to apply to each video.
        video_quality: Optional video quality override for playlist downloads.
        audio_only: Optional override to download only audio.
        audio_quality: Optional audio bitrate override for playlist downloads.
        organization_id: Originating request's tenant for placeholder stamping.
        transcription_options: See ``process_youtube_playlist_task``.

    Returns:
        YouTubePlaylistProcessingResult with processing outcome.
    """
    with session_scope() as db:
        user = db.query(User).filter(User.id == user_id).first()

        if not user:
            logger.error(f"User {user_id} not found")
            return _create_playlist_error_result("User not found")

        def progress_callback(progress, message, data):
            _send_playlist_notification(user_id, "processing", message, progress, data)

        try:
            result = MediaDownloadService().process_youtube_playlist_sync(
                url=url,
                db=db,
                user=user,
                progress_callback=progress_callback,
                organization_id=organization_id,
            )
            return _handle_playlist_result(
                result,
                user_id,
                db,
                collection_ids,
                tag_names,
                video_quality,
                audio_only,
                audio_quality,
                transcription_options=transcription_options,
            )
        except Exception as e:
            logger.error(f"Error processing YouTube playlist {url}: {e}")
            _send_playlist_notification(
                user_id, "error", f"Playlist processing failed: {str(e)}", 0
            )
            return _create_playlist_error_result(str(e))


def _handle_playlist_result(
    result: dict,
    user_id: int,
    db,
    collection_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    video_quality: str | None = None,
    audio_only: bool | None = None,
    audio_quality: str | None = None,
    transcription_options: dict | None = None,
) -> YouTubePlaylistProcessingResult:
    """Handle successful playlist extraction result.

    Args:
        result: Result dict from YouTubeService.process_youtube_playlist_sync.
        user_id: User ID.
        db: Database session.
        collection_ids: Optional collection UUIDs to assign to each video.
        tag_names: Optional tag names to apply to each video.
        video_quality: Optional video quality override for playlist downloads.
        audio_only: Optional override to download only audio.
        audio_quality: Optional audio bitrate override for playlist downloads.
        transcription_options: See ``process_youtube_playlist_task``.

    Returns:
        YouTubePlaylistProcessingResult with success outcome.
    """
    created_media_files = result.get("media_files", [])
    playlist_info = result.get("playlist_info", {})
    created_count = result.get("created_count", 0)
    skipped_count = result.get("skipped_count", 0)
    total_videos = result.get("total_videos", 0)

    playlist_title = playlist_info.get("playlist_title", "Unknown Playlist")
    logger.info(
        f"Playlist '{playlist_title}' extraction complete: {created_count} videos to process, "
        f"{skipped_count} skipped"
    )

    if transcription_options:
        from app.tasks.transcription.requested_options import apply_requested_options

        for media_file in created_media_files:
            apply_requested_options(media_file, transcription_options)
        db.commit()

    # Apply collections and tags to each playlist video
    if collection_ids or tag_names:
        from app.api.endpoints.files.prepare_upload import add_file_to_collections
        from app.api.endpoints.files.prepare_upload import add_tags_to_file

        if collection_ids:
            from uuid import UUID as _UUID

            parsed_ids = [_UUID(c) for c in collection_ids]

        for media_file in created_media_files:
            if collection_ids:
                add_file_to_collections(db, int(media_file.id), user_id, parsed_ids)
            if tag_names:
                # Background task: the tag belongs to the file owner.
                add_tags_to_file(db, int(media_file.id), tag_names, user_id)
        db.commit()

    # Send file_created notifications for each video
    for media_file in created_media_files:
        _notify_playlist_file_created(user_id, media_file)

    # Calculate staggered delays for playlist videos
    from app.services.youtube_rate_limiter import calculate_playlist_delays

    delays = calculate_playlist_delays(len(created_media_files))

    # Dispatch individual processing tasks with progressive delays
    dispatched_count = 0
    for idx, media_file in enumerate(created_media_files):
        countdown = delays[idx]

        if _dispatch_playlist_video_task(
            media_file,
            user_id,
            db,
            countdown=countdown,
            collection_ids=collection_ids,
            tag_names=tag_names,
            video_quality=video_quality,
            audio_only=audio_only,
            audio_quality=audio_quality,
        ):
            dispatched_count += 1
            logger.info(
                f"Video {idx + 1}/{len(created_media_files)}: "
                f"'{media_file.title}' scheduled (+{countdown}s)"
            )

    # Build completion message
    completion_message = (
        f"Playlist '{playlist_title}': {created_count} of {total_videos} videos queued for download"
    )
    if skipped_count > 0:
        completion_message += f" ({skipped_count} already in library)"

    # Send completion notification
    _send_playlist_notification(
        user_id,
        "completed",
        completion_message,
        100,
        {
            "playlist_title": playlist_title,
            "playlist_id": playlist_info.get("playlist_id"),
            "created_count": created_count,
            "skipped_count": skipped_count,
            "total_videos": total_videos,
            "dispatched_count": dispatched_count,
        },
    )

    return {
        "status": "success",
        "message": completion_message,
        "created_count": created_count,
        "skipped_count": skipped_count,
        "total_videos": total_videos,
    }
