"""Single-video URL import: metadata extraction, dedup, placeholder row, dispatch, notify."""

import logging
from typing import Any

from fastapi import HTTPException
from fastapi import status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import DomainError
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.formatting_service import FormattingService
from app.services.media_download_service import MediaDownloadService
from app.tasks.youtube_processing import process_youtube_url_task
from app.utils.db_helpers import org_stamp_is

logger = logging.getLogger(__name__)


def _extract_video_info(
    media_service: MediaDownloadService,
    normalized_url: str,
    media_username: str | None = None,
    media_password: str | None = None,
    user_id: int | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Extract video ID and title from a media URL.

    Args:
        media_service: MediaDownloadService instance.
        normalized_url: Normalized media URL.
        user_id: Optional user ID for per-user credential resolution.

    Returns:
        Tuple of (video_id, video_title, video_info).

    Raises:
        HTTPException: If video info extraction fails.
    """
    try:
        video_info = media_service.extract_video_info(
            normalized_url,
            media_username=media_username,
            media_password=media_password,
            user_id=user_id,
        )
        video_id = video_info.get("id")
        video_title = video_info.get("title", "Media Video")
    except (HTTPException, DomainError):
        # Re-raise HTTPExceptions (they already have proper error messages)
        raise
    except Exception as e:
        # create_user_friendly_error is NOT a sanitizer despite the name — it only
        # strips four literal yt-dlp prefixes on the non-auth-error branch, so the
        # raw message (which can carry cookie/config paths or the URL itself) used
        # to reach the response verbatim (#859). Log the real error; return a
        # fixed, caller-safe literal.
        logger.exception(f"Error extracting video info from {normalized_url}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not read information for that media URL.",
        ) from e

    if not video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not extract video ID from URL",
        )

    return video_id, video_title, video_info


def _check_duplicate_video(
    db: Session, user_id: int, video_id: str, normalized_url: str, organization_id: int | None
) -> None:
    """Check if the video already exists for this user in the active tenant.

    Args:
        db: Database session.
        user_id: User ID to check against.
        video_id: Video ID from the platform.
        normalized_url: Normalized media URL.
        organization_id: The caller's active tenant (None = personal). The same
            user's copy in another tenant is not a duplicate of this one.

    Raises:
        HTTPException: If a duplicate video is found (409 Conflict).
    """
    base = db.query(MediaFile).filter(
        MediaFile.user_id == user_id,
        org_stamp_is(MediaFile.organization_id, organization_id),
    )

    # Check by source_url first (works for all platforms)
    existing_video = base.filter(MediaFile.source_url == normalized_url).first()

    # Also check by metadata_raw video_id (for backward compatibility with YouTube)
    if not existing_video:
        existing_video = (
            base.filter(text("metadata_raw->>'video_id' = :video_id"))
            .params(video_id=video_id)
            .first()
        )

    # Check by youtube_id for backward compatibility
    if not existing_video:
        existing_video = (
            base.filter(text("metadata_raw->>'youtube_id' = :youtube_id"))
            .params(youtube_id=video_id)
            .first()
        )

    if not existing_video:
        return

    logger.info(
        f"Found existing video with ID {video_id} for user {user_id}: "
        f"MediaFile ID {existing_video.id}, status: {existing_video.status}"
    )

    _raise_duplicate_error(existing_video)


def _raise_duplicate_error(existing_video: MediaFile) -> None:
    """Raise appropriate HTTPException for a duplicate video.

    Args:
        existing_video: The existing MediaFile record.

    Raises:
        HTTPException: 409 Conflict with status-specific message.
    """
    if existing_video.status == FileStatus.ERROR:
        # The stored column can still hold a download's raw yt-dlp text, so it goes
        # through the same sanitizer as every other read edge (#786/#959).
        reason = (
            ErrorCategorizationService.user_message_for(existing_video.last_error_message)
            or "Processing failed for this file."
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This video already exists in your library but could not be processed: "
            f"{reason} Please delete it first if you want to re-process it.",
        )

    if existing_video.status in [FileStatus.PENDING, FileStatus.PROCESSING]:
        video_name = existing_video.title or existing_video.filename
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This video is already being processed: {video_name}",
        )

    video_name = existing_video.title or existing_video.filename
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"This video already exists in your library: {video_name}",
    )


def _create_media_file_record(
    db: Session,
    user_id: int,
    normalized_url: str,
    video_id: str,
    video_title: str,
    video_info: dict[str, Any],
    organization_id: int | None = None,
) -> MediaFile:
    """Create a placeholder MediaFile record for the media video.

    Args:
        db: Database session.
        user_id: User ID.
        normalized_url: Normalized media URL.
        video_id: Video ID from the platform.
        video_title: Video title.
        video_info: Full video info dict from yt-dlp or custom extractor.
        organization_id: Active organization id (cloud org-scoped ingest) or
            None for personal scope. Always None in the community edition.

    Returns:
        Created MediaFile instance.
    """
    # Get source platform from extractor
    source = video_info.get("extractor", "unknown").lower()

    placeholder_metadata = {
        "video_id": video_id,
        "source": source,
        "source_url": normalized_url,
        "title": video_title,
        "processing": True,
    }

    # Add YouTube-specific fields for backward compatibility
    if "youtube" in source:
        placeholder_metadata["youtube_id"] = video_id
        placeholder_metadata["youtube_url"] = normalized_url

    media_file = MediaFile(
        user_id=user_id,
        organization_id=organization_id,
        filename=video_title[:255],
        storage_path="",
        file_size=0,
        content_type="video/mp4",
        duration=video_info.get("duration"),
        status=FileStatus.PROCESSING,
        title=video_title,
        author=video_info.get("uploader"),
        description=video_info.get("description"),
        source_url=normalized_url,
        metadata_raw=placeholder_metadata,
        metadata_important=placeholder_metadata,
    )

    db.add(media_file)
    db.commit()
    db.refresh(media_file)

    return media_file


def _dispatch_url_video_task(
    db: Session,
    media_file: MediaFile,
    normalized_url: str,
    user_id: int,
    media_username: str | None = None,
    media_password: str | None = None,
    collection_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    video_quality: str | None = None,
    audio_only: bool | None = None,
    audio_quality: str | None = None,
) -> None:
    """Dispatch the video processing background task.

    Args:
        db: Database session.
        media_file: MediaFile record to process.
        normalized_url: Normalized media URL.
        user_id: User ID.
        media_username: Optional username for authenticated downloads.
        media_password: Optional password for authenticated downloads.
        collection_ids: Optional collection UUIDs to assign after processing.
        tag_names: Optional tag names to apply after processing.
        video_quality: Optional video quality override for this download.
        audio_only: Optional override to download only audio.
        audio_quality: Optional audio bitrate override for this download.

    Raises:
        HTTPException: If task dispatch fails.
    """
    try:
        task_result = process_youtube_url_task.delay(
            url=normalized_url,
            user_id=user_id,
            file_uuid=str(media_file.uuid),
            media_username=media_username,
            media_password=media_password,
            collection_ids=collection_ids,
            tag_names=tag_names,
            video_quality=video_quality,
            audio_only=audio_only,
            audio_quality=audio_quality,
        )
        logger.info(
            f"Dispatched media processing task {task_result.id} for MediaFile {media_file.id}"
        )
        # Product metric: media accepted via URL ingestion (API process). The
        # download worker later runs dispatch_upload_pipeline, but that registry
        # is never scraped, so the source="url" count is recorded here.
        from app.core.metrics import files_uploaded_total

        files_uploaded_total.labels(source="url").inc()
    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Failed to dispatch media processing task: {e}")
        db.delete(media_file)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start video processing. Please try again.",
        ) from e


def _notify_url_file_created(media_file: MediaFile, user_id: int) -> None:
    """Send WebSocket notification for newly created file.

    Routed through Redis pub/sub rather than the in-process ConnectionManager
    (issue #284 A1.14). ``send_notification`` only reaches sockets held by THIS
    process, so behind a load balancer a user connected to a different replica never
    saw the new file appear — the gallery just looked stuck until a manual refresh.
    ``send_ws_event`` publishes on the ``websocket_notifications`` channel, which every
    replica's subscriber consumes, so the owning process delivers it wherever it is.

    Synchronous on purpose: ``send_ws_event`` is a blocking Redis PUBLISH, so declaring
    this ``async`` only made the blocking call happen *on the event loop* (issue #284
    A2.4). It is now called from a sync request handler, i.e. Starlette's threadpool.

    Args:
        media_file: The created MediaFile.
        user_id: User ID to notify.
    """
    try:
        from app.utils.websocket_notify import send_ws_event_for_file

        send_ws_event_for_file(
            user_id=user_id,
            notification_type="file_created",
            file_uuid=media_file.uuid,
            data={
                "file_id": str(media_file.uuid),
                "file": {
                    "uuid": str(media_file.uuid),
                    "filename": media_file.filename,
                    "status": media_file.status.value,
                    "display_status": FormattingService.format_status(
                        FileStatus(media_file.status)
                    ),
                    "content_type": media_file.content_type,
                    "file_size": media_file.file_size,
                    "title": media_file.title,
                    "author": media_file.author,
                    "duration": media_file.duration,
                    "upload_time": media_file.upload_time.isoformat()
                    if media_file.upload_time
                    else None,
                },
            },
        )
    except Exception as e:
        logger.warning(f"Failed to send file_created notification: {e}")
