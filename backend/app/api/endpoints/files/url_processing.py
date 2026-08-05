"""
URL processing endpoints for handling media URLs from various platforms.

This module provides API endpoints for processing external URLs from YouTube, Vimeo,
Twitter/X, TikTok, and 1800+ other platforms supported by yt-dlp.
"""

import logging
import re
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings
from app.core.constants import VALID_AUDIO_QUALITIES
from app.core.constants import VALID_VIDEO_QUALITIES
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.media import MediaFile as MediaFileSchema
from app.services.formatting_service import FormattingService
from app.services.media_download_service import MediaDownloadService
from app.services.media_download_service import create_user_friendly_error
from app.tasks.youtube_processing import process_youtube_playlist_task
from app.tasks.youtube_processing import process_youtube_url_task

logger = logging.getLogger(__name__)

router = APIRouter()


# Request model for URL processing
class URLProcessingRequest(BaseModel):
    """Request model for processing media URLs (YouTube, Vimeo, Twitter, and many more)."""

    url: str = Field(
        description="Media URL from YouTube, Vimeo, Twitter/X, TikTok, and 1800+ other sites",
        examples=[
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/playlist?list=PLClBBDzHMVijJfoN2EY_3OpmHfRIv4eGO",
            "https://vimeo.com/123456789",
            "https://twitter.com/user/status/123456789",
        ],
        min_length=1,
    )

    media_username: str | None = Field(
        default=None,
        description=(
            "Optional username for protected media sources. "
            "Used only for authenticated downloads of protected media URLs."
        ),
    )
    media_password: str | None = Field(
        default=None,
        description=(
            "Optional password for protected media sources "
            "(never stored, only used for this request)"
        ),
    )
    collection_ids: list[str] | None = Field(
        default=None,
        description="Collection UUIDs to add the file to after creation",
    )
    tag_names: list[str] | None = Field(
        default=None,
        description="Tag names to apply to the file after creation",
    )
    skip_summary: bool = Field(
        default=False,
        description="Skip AI summary generation for this download",
    )

    video_quality: str | None = Field(
        default=None,
        description="Video quality override for this download (e.g., 'best', '1080p', '720p')",
    )
    audio_only: bool | None = Field(
        default=None,
        description="Override to download only audio (no video) for this request",
    )
    audio_quality: str | None = Field(
        default=None,
        description="Audio bitrate override for this download (e.g., 'best', '320', '192', '128')",
    )

    @field_validator("video_quality")
    @classmethod
    def validate_video_quality(cls, v: str | None) -> str | None:
        """Validate video_quality against allowed options."""
        if v is not None and v not in VALID_VIDEO_QUALITIES:
            raise ValueError(f"video_quality must be one of: {VALID_VIDEO_QUALITIES}")
        return v

    @field_validator("audio_quality")
    @classmethod
    def validate_audio_quality(cls, v: str | None) -> str | None:
        """Validate audio_quality against allowed options."""
        if v is not None and v not in VALID_AUDIO_QUALITIES:
            raise ValueError(f"audio_quality must be one of: {VALID_AUDIO_QUALITIES}")
        return v


# Response models for URL processing
class PlaylistProcessingResponse(BaseModel):
    """Response model for YouTube playlist processing requests."""

    type: str = Field(
        default="playlist", description="Response type indicator", examples=["playlist"]
    )
    status: str = Field(
        default="processing",
        description="Processing status",
        examples=["processing", "completed", "error"],
    )
    message: str = Field(
        description="Human-readable status message",
        examples=["Playlist processing started. Videos will appear as they are extracted."],
    )
    url: str = Field(
        description="Original playlist URL",
        examples=["https://youtube.com/playlist?list=PLClBBDzHMVijJfoN2EY_3OpmHfRIv4eGO"],
    )


# Union type for endpoint response - can be either a MediaFile or PlaylistProcessingResponse
URLProcessingResponse = MediaFileSchema | PlaylistProcessingResponse


# Generic URL pattern for any HTTP/HTTPS URL
GENERIC_URL_PATTERN = re.compile(r"^https?://.+$")

# YouTube URL validation and normalization (kept for playlist/YouTube-specific handling)
YOUTUBE_URL_PATTERN = re.compile(
    r"^https?://(www\.)?(youtube\.com/(watch\?v=|embed/|v/)|youtu\.be/)[\w\-_]+.*$"
)


def normalize_media_url(url: str) -> str:
    """Normalize media URL for duplicate detection.

    For YouTube URLs, extracts the video ID or playlist ID and converts them
    to a canonical format. For other platforms, returns the URL stripped of whitespace.

    Args:
        url: Media URL from any supported platform.

    Returns:
        str: Normalized URL for duplicate detection.
             YouTube videos: "https://www.youtube.com/watch?v={video_id}"
             YouTube playlists: "https://www.youtube.com/playlist?list={playlist_id}"
             Other platforms: Original URL stripped of whitespace

    Example:
        >>> normalize_media_url("https://youtu.be/dQw4w9WgXcQ")
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        >>> normalize_media_url("https://vimeo.com/123456789")
        "https://vimeo.com/123456789"
    """
    url = url.strip()

    # Check if it's a YouTube playlist URL first
    playlist_match = re.search(r"[?&]list=([\w\-_]+)", url)
    if playlist_match and "youtube.com/playlist" in url:
        playlist_id = playlist_match.group(1)
        return f"https://www.youtube.com/playlist?list={playlist_id}"

    # Extract video ID from various YouTube URL formats
    video_id_patterns = [
        r"youtube\.com/watch\?v=([^&\n]+)",
        r"youtube\.com/embed/([^?\n]+)",
        r"youtube\.com/v/([^?\n]+)",
        r"youtu\.be/([^?\n]+)",
    ]

    for pattern in video_id_patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            return f"https://www.youtube.com/watch?v={video_id}"

    # For non-YouTube URLs, return as-is (stripped)
    return url


# Backward compatibility alias
normalize_youtube_url = normalize_media_url


def _validate_media_url(url: str) -> tuple[str, MediaDownloadService]:
    """Validate and normalize a media URL.

    Args:
        url: Raw media URL from the request.

    Returns:
        Tuple of (normalized_url, media_service).

    Raises:
        HTTPException: If URL is missing or invalid.
    """
    if not url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="URL is required")

    normalized_url = normalize_media_url(url)
    media_service = MediaDownloadService()

    if not media_service.is_valid_media_url(normalized_url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid URL. Please enter a valid HTTP or HTTPS URL.",
        )

    return normalized_url, media_service


# Backward compatibility alias
def _validate_youtube_url(url: str) -> tuple[str, MediaDownloadService]:
    """Backward compatibility alias for _validate_media_url."""
    return _validate_media_url(url)


def _handle_playlist_processing(
    normalized_url: str,
    user_id: int,
    collection_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    video_quality: str | None = None,
    audio_only: bool | None = None,
    audio_quality: str | None = None,
    organization_id: int | None = None,
) -> PlaylistProcessingResponse:
    """Handle playlist URL processing by dispatching a background task.

    Args:
        normalized_url: Normalized YouTube playlist URL.
        user_id: ID of the user requesting processing.
        collection_ids: Optional collection UUIDs to assign to each video.
        tag_names: Optional tag names to apply to each video.
        video_quality: Optional video quality override for playlist downloads.
        audio_only: Optional override to download only audio.
        audio_quality: Optional audio bitrate override for playlist downloads.
        organization_id: The originating request's tenant (``ctx.org_id``) —
            threaded through the task kwargs so placeholders are stamped with
            the org the user was acting in, never a membership guess (#262c).

    Returns:
        PlaylistProcessingResponse with processing status.

    Raises:
        HTTPException: If task dispatch fails.
    """
    logger.info(f"Detected playlist URL: {normalized_url}")

    try:
        task_result = process_youtube_playlist_task.delay(
            url=normalized_url,
            user_id=user_id,
            collection_ids=collection_ids,
            tag_names=tag_names,
            video_quality=video_quality,
            audio_only=audio_only,
            audio_quality=audio_quality,
            organization_id=organization_id,
        )
        logger.info(
            f"Dispatched YouTube playlist processing task {task_result.id} for user {user_id}"
        )

        return PlaylistProcessingResponse(
            type="playlist",
            status="processing",
            message="Playlist processing started. Videos will appear as they are extracted.",
            url=normalized_url,
        )

    except Exception as e:
        logger.error(f"Failed to dispatch YouTube playlist processing task: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start playlist processing. Please try again.",
        ) from e


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
    except HTTPException:
        # Re-raise HTTPExceptions (they already have proper error messages)
        raise
    except Exception as e:
        logger.error(f"Error extracting video info from {normalized_url}: {e}")
        # Create user-friendly error message
        user_friendly_error = create_user_friendly_error(str(e), normalized_url)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to extract video information: {user_friendly_error}",
        ) from e

    if not video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not extract video ID from URL",
        )

    return video_id, video_title, video_info


def _check_duplicate_video(db: Session, user_id: int, video_id: str, normalized_url: str) -> None:
    """Check if the video already exists for this user.

    Args:
        db: Database session.
        user_id: User ID to check against.
        video_id: Video ID from the platform.
        normalized_url: Normalized media URL.

    Raises:
        HTTPException: If a duplicate video is found (409 Conflict).
    """
    existing_video = None

    # Check by source_url first (works for all platforms)
    existing_video = (
        db.query(MediaFile)
        .filter(
            MediaFile.user_id == user_id,
            MediaFile.source_url == normalized_url,
        )
        .first()
    )

    # Also check by metadata_raw video_id (for backward compatibility with YouTube)
    if not existing_video:
        existing_video = (
            db.query(MediaFile)
            .filter(
                MediaFile.user_id == user_id,
                text("metadata_raw->>'video_id' = :video_id"),
            )
            .params(video_id=video_id)
            .first()
        )

    # Check by youtube_id for backward compatibility
    if not existing_video:
        existing_video = (
            db.query(MediaFile)
            .filter(
                MediaFile.user_id == user_id,
                text("metadata_raw->>'youtube_id' = :youtube_id"),
            )
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
        error_msg = existing_video.last_error_message or "processing failed"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This video already exists in your library but {error_msg}. "
            f"Please delete it first if you want to re-process it.",
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


def _dispatch_video_task(
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
    except Exception as e:
        logger.error(f"Failed to dispatch media processing task: {e}")
        db.delete(media_file)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start video processing. Please try again.",
        ) from e


async def _send_file_created_notification(media_file: MediaFile, user_id: int) -> None:
    """Send WebSocket notification for newly created file.

    Routed through Redis pub/sub rather than the in-process ConnectionManager
    (issue #284 A1.14). ``send_notification`` only reaches sockets held by THIS
    process, so behind a load balancer a user connected to a different replica never
    saw the new file appear — the gallery just looked stuck until a manual refresh.
    ``send_ws_event`` publishes on the ``websocket_notifications`` channel, which every
    replica's subscriber consumes, so the owning process delivers it wherever it is.

    Args:
        media_file: The created MediaFile.
        user_id: User ID to notify.
    """
    try:
        from app.utils.websocket_notify import send_ws_event

        send_ws_event(
            user_id=user_id,
            notification_type="file_created",
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


@router.post("/process-url", response_model=URLProcessingResponse)
async def process_media_url(
    request_data: URLProcessingRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> URLProcessingResponse:
    """
    Process a media URL by dispatching background tasks.

    Supports YouTube, Vimeo, Twitter/X, TikTok, and 1800+ other platforms via yt-dlp.
    YouTube playlists are also supported.

    Args:
        request_data: URLProcessingRequest containing the media URL
        db: Database session
        current_user: Authenticated user

    Returns:
        Union[MediaFileSchema, PlaylistProcessingResponse]:
            - For single videos: MediaFile object with pending status
            - For playlists: PlaylistProcessingResponse with processing details

    Raises:
        HTTPException:
            - 400 if URL is missing, invalid, or unsupported
            - 409 if URL already exists for user (single videos only)
            - 401 if user is not authenticated
            - 500 for server errors

    Examples:
        YouTube video:
            POST /process-url
            {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}

        YouTube playlist:
            POST /process-url
            {"url": "https://youtube.com/playlist?list=PLClBBDzHMVijJfoN2EY_3OpmHfRIv4eGO"}

        Vimeo video:
            POST /process-url
            {"url": "https://vimeo.com/123456789"}
    """
    try:
        # Check rate limit before processing
        from app.services.youtube_rate_limiter import youtube_rate_limiter

        if settings.YOUTUBE_USER_RATE_LIMIT_ENABLED:
            allowed, reason = youtube_rate_limiter.check_rate_limit(current_user.id)
            if not allowed:
                raise HTTPException(status_code=429, detail=reason)

        # Validate and normalize URL
        normalized_url, media_service = _validate_media_url(request_data.url)

        # Record download attempt after rate check passes
        if settings.YOUTUBE_USER_RATE_LIMIT_ENABLED:
            youtube_rate_limiter.record_download(current_user.id)

        # Handle playlist processing (early return) - currently YouTube only
        if media_service.is_playlist_url(normalized_url):
            return _handle_playlist_processing(
                normalized_url,
                current_user.id,
                collection_ids=request_data.collection_ids,
                tag_names=request_data.tag_names,
                video_quality=request_data.video_quality,
                audio_only=request_data.audio_only,
                audio_quality=request_data.audio_quality,
                organization_id=ctx.org_id,
            )

        # Extract video info
        video_id, video_title, video_info = _extract_video_info(
            media_service,
            normalized_url,
            media_username=request_data.media_username,
            media_password=request_data.media_password,
            user_id=current_user.id,
        )

        # Check for duplicate video
        _check_duplicate_video(db, current_user.id, video_id, normalized_url)

        # Create placeholder MediaFile record
        media_file = _create_media_file_record(
            db,
            current_user.id,
            normalized_url,
            video_id,
            video_title,
            video_info,
            ctx.org_id,
        )

        # Per-file skip summary: mark as disabled before pipeline starts
        if request_data.skip_summary:
            media_file.summary_status = "disabled"  # type: ignore[assignment]
            db.commit()

        # Assign to collections and apply tags immediately for single videos
        if request_data.collection_ids or request_data.tag_names:
            from app.api.endpoints.files.prepare_upload import add_file_to_collections
            from app.api.endpoints.files.prepare_upload import add_tags_to_file

            if request_data.collection_ids:
                from uuid import UUID as _UUID

                add_file_to_collections(
                    db,
                    media_file.id,
                    current_user.id,
                    [_UUID(c) for c in request_data.collection_ids],
                )
            if request_data.tag_names:
                add_tags_to_file(db, media_file.id, request_data.tag_names, current_user.id)
            db.commit()

        # Dispatch background task (pass credentials only for this processing request)
        _dispatch_video_task(
            db,
            media_file,
            normalized_url,
            current_user.id,
            media_username=request_data.media_username,
            media_password=request_data.media_password,
            collection_ids=request_data.collection_ids,
            tag_names=request_data.tag_names,
            video_quality=request_data.video_quality,
            audio_only=request_data.audio_only,
            audio_quality=request_data.audio_quality,
        )

        # Send WebSocket notification
        await _send_file_created_notification(media_file, current_user.id)

        logger.info(f"Created placeholder MediaFile {media_file.id} for media URL processing")
        # ORM MediaFile serialized to the schema via response_model (from_attributes)
        return media_file  # type: ignore[return-value]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error processing media URL: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error processing media URL",
        ) from e


@router.get("/youtube/quota")
async def get_youtube_download_quota(
    current_user: User = Depends(get_current_active_user),
):
    """Get user's remaining YouTube download quota.

    Returns:
        dict: Quota information with hourly/daily remaining and limits.
            - hourly_remaining: Downloads left this hour (-1 if unlimited)
            - daily_remaining: Downloads left today (-1 if unlimited)
            - hourly_limit: Max downloads per hour (-1 if unlimited)
            - daily_limit: Max downloads per day (-1 if unlimited)
    """
    from app.services.youtube_rate_limiter import youtube_rate_limiter

    quota = youtube_rate_limiter.get_remaining_quota(current_user.id)
    return quota


# Backward compatibility alias
process_youtube_url = process_media_url
