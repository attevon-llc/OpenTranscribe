"""
URL processing endpoints for handling media URLs from various platforms.

This module provides API endpoints for processing external URLs from YouTube, Vimeo,
Twitter/X, TikTok, and 1800+ other platforms supported by yt-dlp.
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.files.url_request import PlaylistProcessingResponse
from app.api.endpoints.files.url_request import URLProcessingRequest
from app.api.endpoints.files.url_request import URLProcessingResponse
from app.api.endpoints.files.url_request import _validate_media_url
from app.api.endpoints.files.url_video_import import _check_duplicate_video
from app.api.endpoints.files.url_video_import import _create_media_file_record
from app.api.endpoints.files.url_video_import import _dispatch_url_video_task
from app.api.endpoints.files.url_video_import import _extract_video_info
from app.api.endpoints.files.url_video_import import _notify_url_file_created
from app.core.config import settings
from app.core.exceptions import DomainError
from app.core.locked_settings import effective_whisper_model
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.user import User
from app.services.request_context import RequestContext
from app.tasks.media_playlist_processing import process_youtube_playlist_task
from app.tasks.transcription.requested_options import apply_requested_options
from app.utils.whisper_model_choice import require_servable_whisper_model

logger = logging.getLogger(__name__)

router = APIRouter()


def _handle_playlist_processing(
    normalized_url: str,
    user_id: int,
    collection_ids: list[str] | None = None,
    tag_names: list[str] | None = None,
    video_quality: str | None = None,
    audio_only: bool | None = None,
    audio_quality: str | None = None,
    organization_id: int | None = None,
    transcription_options: dict | None = None,
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
        transcription_options: Per-file speaker range / model for every video.

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
            transcription_options=transcription_options,
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

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Failed to dispatch YouTube playlist processing task: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start playlist processing. Please try again.",
        ) from e


def _build_transcription_options(
    request_data: URLProcessingRequest, http_request: Request
) -> dict[str, Any]:
    """Collect the per-file transcription options for a URL import.

    Args:
        request_data: The URL processing request.
        http_request: The incoming request (for the deployment-locked model).

    Returns:
        Speaker range and Whisper model for the file.

    Raises:
        HTTPException: 422 if the requested model cannot be served.
    """
    # Per-file transcription options. The model is checked now so an unservable one is
    # a 422 here rather than a silent fallback after the download.
    return {
        "min_speakers": request_data.min_speakers,
        "max_speakers": request_data.max_speakers,
        "num_speakers": request_data.num_speakers,
        "whisper_model": require_servable_whisper_model(
            effective_whisper_model(request_data.whisper_model, http_request)
        ),
    }


def _apply_single_video_options(
    db: Session,
    media_file: MediaFile,
    request_data: URLProcessingRequest,
    transcription_options: dict[str, Any],
    user_id: int,
) -> None:
    """Record the requested options, skip-summary flag, collections and tags on the row.

    Args:
        db: Database session.
        media_file: The placeholder MediaFile just created.
        request_data: The URL processing request.
        transcription_options: Options from ``_build_transcription_options``.
        user_id: The requesting user's ID.
    """
    # Recorded on the row so they survive the download hop: the worker dispatches
    # transcription when the download finishes and reads them back (and a retry replays).
    apply_requested_options(media_file, transcription_options)
    db.commit()

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
                user_id,
                [_UUID(c) for c in request_data.collection_ids],
            )
        if request_data.tag_names:
            add_tags_to_file(db, media_file.id, request_data.tag_names, user_id)
        db.commit()


@router.post(
    "/process-url",
    response_model=URLProcessingResponse,
    dependencies=[Depends(refuse_under_support_grant)],
)
def process_media_url(
    request_data: URLProcessingRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> URLProcessingResponse:
    """
    Process a media URL by dispatching background tasks.

    Supports YouTube, Vimeo, Twitter/X, TikTok, and 1800+ other platforms via yt-dlp.
    YouTube playlists are also supported.

    Declared ``def``, not ``async def`` (issue #284 A2.4). Every call in this handler is
    blocking: ``yt-dlp``'s ``extract_info`` performs a full metadata fetch against the
    remote platform (seconds, sometimes tens of seconds), the rate limiter talks to Redis,
    and the duplicate check / placeholder insert talk to Postgres. In an ``async def``
    handler all of that ran *on the event loop*, so a single slow YouTube lookup stalled
    every other request served by the process. A plain ``def`` handler is dispatched to
    Starlette's threadpool instead, which is the same mechanism as ``run_in_threadpool``
    without hand-wrapping each call site.

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
            allowed, reason, retry_after_seconds = youtube_rate_limiter.check_rate_limit(
                current_user.id
            )
            if not allowed:
                headers = (
                    {"Retry-After": str(retry_after_seconds)}
                    if retry_after_seconds is not None
                    else None
                )
                raise HTTPException(status_code=429, detail=reason, headers=headers)

        # Validate and normalize URL
        normalized_url, media_service = _validate_media_url(request_data.url)

        transcription_options = _build_transcription_options(request_data, http_request)

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
                transcription_options=transcription_options,
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
        _check_duplicate_video(db, current_user.id, video_id, normalized_url, ctx.org_id)

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

        _apply_single_video_options(
            db, media_file, request_data, transcription_options, current_user.id
        )

        # Dispatch background task (pass credentials only for this processing request)
        _dispatch_url_video_task(
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
        _notify_url_file_created(media_file, current_user.id)

        logger.info(f"Created placeholder MediaFile {media_file.id} for media URL processing")
        # ORM MediaFile serialized to the schema via response_model (from_attributes)
        return media_file  # type: ignore[return-value]

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.error(f"Unexpected error processing media URL: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error processing media URL",
        ) from e


@router.get("/youtube/quota")
def get_youtube_download_quota(
    current_user: User = Depends(get_current_active_user),
):
    """Get user's remaining YouTube download quota.

    Sync handler: the quota lookup is a blocking Redis read (issue #284 A2.4).

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
