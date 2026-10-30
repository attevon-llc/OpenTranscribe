"""Thumbnail URL resolution and fallback generation for downloaded media."""

import logging
from typing import TYPE_CHECKING
from typing import Any

import requests

from app.services.thumbnail import generate_and_upload_thumbnail

if TYPE_CHECKING:
    from app.services.media_download_service import MediaDownloadService

logger = logging.getLogger(__name__)


def _resolve_thumbnail_url(media_info: dict[str, Any]) -> str | None:
    """
    Resolve the best thumbnail URL from media metadata.

    Args:
        media_info: Media metadata from yt-dlp

    Returns:
        Best available thumbnail URL or None
    """
    thumbnails = media_info.get("thumbnails", [])

    # Fallback to single thumbnail URL if no thumbnails list
    if not thumbnails:
        return media_info.get("thumbnail")  # type: ignore[return-value]

    # Find the highest quality thumbnail
    max_width = 0
    thumbnail_url: str | None = None
    for thumb in thumbnails:
        width = thumb.get("width", 0)
        if width > max_width and thumb.get("url"):
            max_width = width
            thumbnail_url = thumb["url"]

    if thumbnail_url:
        return thumbnail_url

    # Fallback to standard YouTube thumbnail URLs if it's a YouTube video
    return _get_fallback_thumbnail_url(media_info.get("id"), media_info.get("extractor", ""))


def _get_fallback_thumbnail_url(video_id: str | None, extractor: str) -> str | None:
    """
    Try standard thumbnail URLs as fallback for known platforms.

    Args:
        video_id: Video ID
        extractor: Platform extractor name

    Returns:
        Working thumbnail URL or None
    """
    if not video_id:
        return None

    # YouTube-specific fallback URLs
    if "youtube" in extractor.lower():
        potential_urls = [
            f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
            f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
        ]

        for test_url in potential_urls:
            try:
                response = requests.head(test_url, timeout=10)
                if response.status_code == 200:
                    return test_url
            except requests.exceptions.RequestException as e:
                logger.debug(f"Thumbnail URL test failed for {test_url}: {e}")

    return None


def _get_thumbnail_with_fallback(
    media_service: "MediaDownloadService",
    media_info: dict[str, Any],
    user_id: int,
    media_file_id: int,
    video_path: str,
    *,
    defer_fallback_generation: bool = False,
    storage_path: str | None = None,
) -> str | None:
    """Get the source-supplied thumbnail (cheap HTTP GET) or defer generation.

    The source thumbnail pull is always attempted inline because it's a
    single ~60 KB request to the upstream CDN — faster than spinning up
    a Celery task round-trip. The FFmpeg-frame-extraction fallback (3-8 s
    of subprocess time) is the expensive path; ``defer_fallback_generation``
    lets the caller ship it to the thumbnail-generation queue instead of
    blocking the download worker.

    Args:
        media_service: MediaDownloadService instance.
        media_info: yt-dlp metadata dict (contains ``thumbnail`` URL if any).
        user_id: Owner user ID.
        media_file_id: Media file integer ID.
        video_path: Local path to the downloaded video (used only when the
            fallback path is taken — either inline or for the async task).
        defer_fallback_generation: When True, dispatch
            ``generate_thumbnail_task`` asynchronously instead of running
            FFmpeg inline. Keeps the URL ingest worker free for the next
            download (Phase 2 PR #9).
        storage_path: Required when ``defer_fallback_generation`` is True
            so the async task knows which MinIO object to decode frames
            from.

    Returns:
        Thumbnail storage path when the source-supplied thumbnail succeeded,
        or None when the fallback generation was deferred (or both failed).
    """
    try:
        thumbnail_path = media_service._download_media_thumbnail_sync(media_info, user_id)
        if thumbnail_path:
            logger.debug(f"Successfully downloaded media thumbnail: {thumbnail_path}")
            return thumbnail_path
        logger.warning("Failed to download media thumbnail, will generate from video")
    except Exception as e:
        logger.error(f"Error downloading media thumbnail: {e}")

    # Fallback path — either defer to Celery or run inline.
    if defer_fallback_generation and storage_path:
        try:
            from app.tasks.thumbnail import generate_thumbnail_task

            generate_thumbnail_task.delay(
                file_id=media_file_id,
                user_id=user_id,
                storage_path=storage_path,
            )
            logger.info(
                f"Dispatched async thumbnail generation for media_file {media_file_id} "
                f"(fallback for URL ingest; object={storage_path})"
            )
            return None
        except Exception as dispatch_err:
            logger.warning(
                f"Async thumbnail dispatch failed for {media_file_id}, "
                f"falling back to inline generation: {dispatch_err}"
            )

    # Inline fallback — keeps legacy behavior for callers that opt out.
    try:
        return generate_and_upload_thumbnail(
            user_id=user_id,
            media_file_id=media_file_id,
            video_path=video_path,
            timestamp=5.0,
        )
    except Exception as fallback_error:
        logger.error(f"Fallback thumbnail generation also failed: {fallback_error}")
        return None
