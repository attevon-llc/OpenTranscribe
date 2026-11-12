"""
Media playlist processing task: expand a playlist URL into per-video downloads.

Creates placeholder MediaFile records for each playlist entry and dispatches
``process_youtube_url_task`` (in ``youtube_processing``) for each one with staggered
delays. Split out of ``youtube_processing`` so each module holds one task.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING
from typing import Any
from typing import TypedDict

from kombu.exceptions import KombuError
from sqlalchemy import BigInteger
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError

from app.core.celery import celery_app
from app.core.queues import DownloadPriority
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.formatting_service import FormattingService
from app.services.media_download_playlist import DOWNLOAD_DISPATCHED_KEY
from app.services.media_download_playlist import DOWNLOAD_DUE_AT_KEY
from app.services.media_download_playlist import DOWNLOAD_TASK_ID_KEY
from app.services.media_download_service import MediaDownloadService
from app.tasks.youtube_processing import process_youtube_url_task
from app.utils.error_classification import categorize_error
from app.utils.task_utils import update_media_file_status
from app.utils.websocket_notify import send_ws_event_for_file

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

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
        logger.exception(f"Failed to send playlist notification: {e}")


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
            "display_status": FormattingService.format_status(media_file.status)
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
        logger.exception(f"Failed to send file_created notification for video {media_file.id}: {e}")


def _claim_playlist_dispatch(
    db: Session,
    media_file_id: int,
    *,
    countdown: int,
    task_id: str,
    previous_due_at: int | None = None,
) -> bool:
    """Claim a placeholder's download for sending, unless a run already did; True if this run did.

    One conditional UPDATE, committed BEFORE the download is sent (issue #1159). A replay of
    the playlist task that overlaps the original run, or follows a loss after the send, finds
    the claim and sends nothing. The row lock the UPDATE takes serialises two concurrent runs;
    the second sees the first's claim once it commits. The claim records the Celery task id
    every send of this download carries and when it is due, so a loss between this commit and
    the send is found by the health check (``identify_stranded_playlist_dispatches``), which
    takes the claim over (``previous_due_at``: only the claim it found, only while the file is
    still QUEUED) and sends again under the SAME id. If the first message was only late, the
    download task starts one of the two (``youtube_processing._starts_claimed_download``).
    """
    current = func.coalesce(MediaFile.metadata_raw, literal({}, JSONB))
    if previous_due_at is None:
        unclaimed = ~current.has_key(DOWNLOAD_DISPATCHED_KEY)
    else:
        unclaimed = and_(
            MediaFile.status == FileStatus.QUEUED,
            MediaFile.metadata_raw[DOWNLOAD_DUE_AT_KEY].astext.cast(BigInteger) == previous_due_at,
        )
    stamp = {
        DOWNLOAD_DISPATCHED_KEY: True,
        DOWNLOAD_TASK_ID_KEY: task_id,
        DOWNLOAD_DUE_AT_KEY: int(time.time()) + countdown,
    }
    claim = (
        update(MediaFile)
        .where(MediaFile.id == media_file_id, unclaimed)
        .values(metadata_raw=current.op("||", return_type=JSONB)(literal(stamp, JSONB)))
        .returning(MediaFile.id)
        .execution_options(synchronize_session=False)
    )
    claimed = db.execute(claim).scalar_one_or_none() is not None
    db.commit()
    return claimed


def resend_stranded_playlist_download(db: Session, media_file: MediaFile) -> bool:
    """Send a claimed playlist download again, under its claimed task id; True if sent.

    For the health check's stranded-claim recovery. The import's per-download overrides (quality,
    collections, tags) are not kept: collections and tags were applied when the placeholder was
    created, and the download falls back to the owner's saved quality.
    """
    meta = media_file.metadata_raw or {}
    task_id = str(meta[DOWNLOAD_TASK_ID_KEY])
    args = [str(media_file.source_url), int(media_file.user_id), str(media_file.uuid)]
    if not _claim_playlist_dispatch(
        db,
        int(media_file.id),
        countdown=0,
        task_id=task_id,
        previous_due_at=int(meta[DOWNLOAD_DUE_AT_KEY]),
    ):
        return False
    process_youtube_url_task.apply_async(args=args, task_id=task_id)
    return True


def resend_stranded_playlist_downloads(db: Session, stranded_files: list[MediaFile]) -> int:
    """The health check's step for claims a lost worker never sent; returns how many were re-sent.

    Lives in the tasks layer (services may not import tasks): ``system.health_check`` finds the
    claims (``identify_stranded_playlist_dispatches``) and calls this. Each re-send goes through
    the same claim as the first send, so two sweeps cannot both send one.
    """
    resent = 0
    for media_file in stranded_files:
        try:
            if resend_stranded_playlist_download(db, media_file):
                resent += 1
                logger.warning(f"Re-sent stranded playlist download for file {media_file.id}")
        except (SQLAlchemyError, KombuError, OSError) as e:  # one file's failure skips only it
            logger.exception(f"Error re-sending playlist download {media_file.id}: {e}")
            db.rollback()
    return resent


def _dispatch_playlist_video_task(
    media_file: MediaFile,
    user_id: int,
    db: Session,
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
    # Read before the claim commits: a commit expires the instance, and these are all the
    # send and the log need.
    video_url = str(media_file.source_url)
    file_uuid = str(media_file.uuid)
    title = media_file.title
    try:
        kwargs: dict = {"collection_ids": collection_ids, "tag_names": tag_names}
        if video_quality is not None:
            kwargs["video_quality"] = video_quality
        if audio_only is not None:
            kwargs["audio_only"] = audio_only
        if audio_quality is not None:
            kwargs["audio_quality"] = audio_quality

        task_id = str(uuid.uuid4())
        if not _claim_playlist_dispatch(
            db, int(media_file.id), countdown=countdown, task_id=task_id
        ):
            logger.info(f"Download for '{title}' was already dispatched by another run")
            return False

        # Use apply_async to support countdown parameter
        process_youtube_url_task.apply_async(
            args=[video_url, user_id, file_uuid],
            kwargs=kwargs,
            countdown=countdown,
            task_id=task_id,
        )

        logger.info(f"Dispatched task for '{title}' with {countdown}s delay")
        return True
    except Exception as e:
        logger.exception(f"Failed to dispatch task for video {title}: {e}")
        # Nothing was sent: the claim either failed or committed before a send that raised.
        db.rollback()
        error_msg = f"Failed to start processing: {str(e)}"
        media_file.last_error_message = error_msg
        media_file.error_category = categorize_error(error_msg).value
        db.flush()
        update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
        return False


# Replayable (issue #1159, ``core/task_replay.py``): placeholders already created are adopted by
# the replay (``media_download_playlist._is_unstarted_playlist_placeholder``) and a download is
# claimed once per placeholder before it is sent (``_claim_playlist_dispatch``).
@celery_app.task(name="download.media_playlist", bind=True, priority=DownloadPriority.PLAYLIST)
def process_youtube_playlist_task(
    self: Any,
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
    from app.legal.processing_gate import check_processing_allowed

    logger.info(f"Starting YouTube playlist processing task for URL: {url}")
    # The legal processing gate, before any placeholder is created or anything downloaded.
    # The requester owns every file this import creates, in the originating tenant.
    with session_scope() as db:
        refusal = check_processing_allowed(
            db, owner_user_id=user_id, organization_id=organization_id
        )
    if refusal is not None:
        logger.info(f"Playlist import for user {user_id} refused: {refusal.message}")
        _send_playlist_notification(user_id, "error", refusal.message, 0)
        return _create_playlist_error_result(refusal.message)
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
        logger.exception(f"Unexpected error in YouTube playlist processing task: {e}")
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

        def progress_callback(progress: int, message: str, data: dict) -> None:
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
            logger.exception(f"Error processing YouTube playlist {url}: {e}")
            _send_playlist_notification(
                user_id, "error", f"Playlist processing failed: {str(e)}", 0
            )
            return _create_playlist_error_result(str(e))


def _handle_playlist_result(
    result: dict,
    user_id: int,
    db: Session,
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
        from app.services.upload_prepare_queries import add_file_to_collections
        from app.services.upload_prepare_queries import add_tags_to_file

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
