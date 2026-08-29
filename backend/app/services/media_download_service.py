"""
Media download service for downloading and processing media from various platforms.

This service handles all media URL-related operations including URL validation,
video downloading, metadata extraction, and integration with the media processing pipeline.
Supports YouTube, Vimeo, Twitter/X, TikTok, and 1800+ other platforms via yt-dlp.
"""

import io
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests
import yt_dlp
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import VALID_AUDIO_QUALITIES
from app.core.constants import VALID_VIDEO_QUALITIES
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.minio_service import upload_file
from app.services.minio_service import upload_file_tuned
from app.services.protected_media_providers import PROTECTED_MEDIA_PROVIDERS
from app.services.protected_media_providers import ProtectedMediaProvider
from app.utils.thumbnail import generate_and_upload_thumbnail
from app.utils.url_validation import is_safe_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# yt-dlp base configuration
# ---------------------------------------------------------------------------

# Base yt-dlp options merged into EVERY YoutubeDL call (info extraction,
# playlist enumeration, and actual downloads).
#
# js_runtimes — required since yt-dlp 2025.11 for YouTube PO-token generation.
#   Deno is the recommended headless runtime; its binary lives at
#   /usr/local/bin/deno, installed via the Dockerfile multi-stage COPY
#   from denoland/deno:bin.
#
# Resilience settings prevent hung connections and transient failures that are
# common in headless / containerised server environments.
_YT_DLP_BASE_OPTS: dict[str, object] = {
    # JavaScript runtime — solves YouTube's bot-detection challenge
    "js_runtimes": {"deno": {"path": "/usr/local/bin/deno"}},
    # Connection resilience
    "socket_timeout": 30,
    "retries": 5,
    "fragment_retries": 5,
    "skip_unavailable_fragments": True,
    "concurrent_fragment_downloads": 4,
}

# YouTube extractor arguments for 2026 best-practice client rotation.
# Using 'default' (yt-dlp auto-selects) plus proven fallback clients.
# 'android' and 'android_sdkless' are deprecated/blocked; ios_downgraded
# and android_vr bypass SABR restrictions on many networks.
# Skip auto-translated subtitles to reduce unnecessary network requests.
_YOUTUBE_EXTRACTOR_ARGS: dict[str, dict] = {
    "youtube": {
        "player_client": ["default", "web_safari", "ios_downgraded", "android_vr"],
        "skip": ["translated_subs"],
    }
}

# Authentication and access error patterns with user-friendly messages
AUTH_ERROR_PATTERNS = {
    "logged-in": "requires a logged-in account",
    "log in": "requires login",
    "sign in": "requires sign-in",
    "credentials": "requires authentication credentials",
    "cookies": "requires browser cookies for authentication",
    "private": "is private or restricted",
    "age": "is age-restricted and requires verification",
    "geo": "is not available in your region (geo-restricted)",
    "removed": "has been removed or is unavailable",
    "unavailable": "is currently unavailable",
    "blocked": "is blocked or restricted",
    "premium": "requires a premium subscription",
    "members only": "is members-only content",
    "subscriber": "requires a subscription",
}

# Platform-specific guidance messages
PLATFORM_GUIDANCE = {
    "vimeo": "Most Vimeo videos require authentication. Try YouTube or Dailymotion instead.",
    "instagram": "Instagram videos typically require login. Try a different platform.",
    "facebook": "Facebook videos often require authentication.",
    "twitter": "Some Twitter/X videos may require login to access.",
    "x": "Some X (Twitter) videos may require login to access.",
    "tiktok": "Some TikTok videos may be region-restricted or require authentication.",
    "linkedin": "LinkedIn videos require authentication.",
    "patreon": "Patreon videos require a subscription to access.",
    "onlyfans": "OnlyFans content requires a subscription.",
    "twitch": "Some Twitch VODs may be subscriber-only.",
}

# Recommended platforms for public video downloads (YouTube is most reliable)
RECOMMENDED_PLATFORMS = ["YouTube", "Dailymotion", "Twitter/X"]


def _detect_auth_error(error_message: str) -> tuple[bool, str]:
    """
    Detect if an error message indicates an authentication-related issue.

    Args:
        error_message: The error message from yt-dlp

    Returns:
        Tuple of (is_auth_error, matched_reason)
    """
    error_lower = error_message.lower()
    for pattern, reason in AUTH_ERROR_PATTERNS.items():
        if pattern in error_lower:
            return True, reason
    return False, ""


def _get_platform_from_error(error_message: str) -> str:
    """
    Try to extract platform name from error message or URL in the error.

    Args:
        error_message: The error message from yt-dlp

    Returns:
        Platform name or empty string
    """
    error_lower = error_message.lower()

    # Check for known platform names in the error
    platforms = [
        "vimeo",
        "instagram",
        "facebook",
        "twitter",
        "x.com",
        "tiktok",
        "linkedin",
        "patreon",
        "twitch",
        "youtube",
    ]
    for platform in platforms:
        if platform in error_lower:
            # Normalize x.com to twitter
            return "twitter" if platform == "x.com" else platform
    return ""


def create_user_friendly_error(error_message: str, url: str = "") -> str:
    """
    Create a user-friendly error message from a yt-dlp error.

    Detects authentication-related errors and provides helpful guidance
    about platform limitations.

    Args:
        error_message: The raw error message from yt-dlp
        url: The original URL (optional, for platform detection)

    Returns:
        User-friendly error message with guidance
    """
    is_auth_error, auth_reason = _detect_auth_error(error_message)

    # Try to detect platform from error message or URL
    platform = _get_platform_from_error(error_message)
    if not platform and url:
        platform = _get_platform_from_error(url)

    if is_auth_error:
        # Build user-friendly message
        if platform:
            platform_title = platform.title() if platform != "x" else "X (Twitter)"
            guidance = PLATFORM_GUIDANCE.get(platform.lower(), "")

            if guidance:
                return (
                    f"This {platform_title} video {auth_reason}. {guidance} "
                    f"For best results, try {', '.join(RECOMMENDED_PLATFORMS)}."
                )
            return (
                f"This {platform_title} video {auth_reason}. "
                f"For best results, try publicly accessible videos on "
                f"{', '.join(RECOMMENDED_PLATFORMS)}."
            )

        # Generic auth error without platform
        return (
            f"This video {auth_reason}. Some platforms restrict video downloads "
            f"to authenticated users. For best results, try publicly accessible "
            f"videos on {', '.join(RECOMMENDED_PLATFORMS)}."
        )

    # Not an auth error - return the original message but cleaned up
    # Remove common yt-dlp prefixes
    cleaned = error_message
    prefixes_to_remove = [
        "ERROR: ",
        "DownloadError: ",
        "[download] ",
        "[generic] ",
    ]
    for prefix in prefixes_to_remove:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]

    return cleaned


# Generic URL pattern - accepts any HTTP/HTTPS URL
GENERIC_URL_PATTERN = re.compile(r"^https?://.+$")

# YouTube URL validation regex - supports both videos and playlists
YOUTUBE_URL_PATTERN = re.compile(
    r"^https?://(www\.)?(youtube\.com/(watch\?v=|embed/|v/|playlist\?list=)|youtu\.be/)[\w\-_]+.*$"
)

# YouTube playlist URL validation regex
YOUTUBE_PLAYLIST_PATTERN = re.compile(
    r"^https?://(www\.)?youtube\.com/playlist\?list=([\w\-_]+).*$"
)


def _find_downloaded_file(output_path: str, clean_title: str, ext: str) -> str:
    """
    Find the downloaded file in the output directory.

    Args:
        output_path: Directory where file was downloaded
        clean_title: Cleaned title for expected filename
        ext: Expected file extension

    Returns:
        Path to the downloaded file

    Raises:
        FileNotFoundError: If no video file is found
    """
    expected_filename = f"{clean_title}.{ext}"
    downloaded_file = os.path.join(output_path, expected_filename)

    if os.path.exists(downloaded_file):
        return downloaded_file

    # Look for any video file in the directory (yt-dlp might change the name)
    for file in os.listdir(output_path):
        if file.endswith((".mp4", ".webm", ".mkv", ".avi")):
            return os.path.join(output_path, file)

    raise FileNotFoundError("Downloaded file not found")


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


def _check_existing_youtube_video(db: Session, user_id: int, video_id: str) -> MediaFile | None:
    """
    Check if a YouTube video already exists in the user's library.

    Args:
        db: Database session
        user_id: User ID
        video_id: YouTube video ID

    Returns:
        Existing MediaFile if found, None otherwise
    """
    from sqlalchemy import text

    result = (
        db.query(MediaFile)
        .filter(
            MediaFile.user_id == user_id,
            text("metadata_raw->>'youtube_id' = :youtube_id"),
        )
        .params(youtube_id=video_id)
        .first()
    )
    return result  # type: ignore[no-any-return]


def _process_playlist_videos(
    db: Session,
    user_id: int,
    videos: list[dict[str, Any]],
    playlist_info: dict[str, Any],
    playlist_url: str,
    video_count: int,
    progress_callback: Callable[[int, str, dict], None] | None = None,
    organization_id: int | None = None,
) -> tuple[list[MediaFile], list[dict[str, Any]]]:
    """
    Process playlist videos and create placeholders.

    Args:
        db: Database session
        user_id: User ID
        videos: List of video entries from playlist
        playlist_info: Playlist metadata
        playlist_url: Original playlist URL
        video_count: Total video count for progress
        progress_callback: Optional progress callback
        organization_id: Originating request's tenant (threaded through the
            task kwargs — issue #262c); stamped on every placeholder

    Returns:
        Tuple of (created_media_files, skipped_videos)
    """
    created_media_files = []
    skipped_videos = []

    for idx, video_entry in enumerate(videos):
        video_id = video_entry.get("video_id")
        video_title = video_entry.get("title", "Unknown")

        # Report progress
        if progress_callback:
            progress = int(20 + (idx / video_count) * 70)
            progress_callback(
                progress,
                f"Processing video {idx + 1} of {video_count}: {video_title[:50]}...",
                {"current_video": idx + 1, "total_videos": video_count, "video_title": video_title},
            )

        # Check for existing video - ensure video_id is a string
        if video_id:
            existing_video = _check_existing_youtube_video(db, user_id, str(video_id))
        else:
            existing_video = None

        if existing_video:
            logger.info(f"Video already exists in library: {video_title} (YouTube ID: {video_id})")
            skipped_videos.append(
                {
                    "video_id": video_id,
                    "title": video_title,
                    "reason": "duplicate",
                    "existing_file_id": existing_video.id,
                }
            )
            continue

        # Create placeholder
        try:
            video_entry["playlist_index"] = video_entry.get("playlist_index", idx + 1)
            media_file = _create_playlist_video_placeholder(
                db,
                user_id,
                video_entry,
                playlist_info,
                playlist_url,
                organization_id=organization_id,
            )
            created_media_files.append(media_file)
            logger.info(
                f"Created placeholder MediaFile {media_file.id} for playlist video: {video_title}"
            )
        except Exception as e:
            logger.error(f"Error creating placeholder for video {video_title}: {e}")
            skipped_videos.append(
                {
                    "video_id": video_id,
                    "title": video_title,
                    "reason": f"error: {str(e)}",
                }
            )

    return created_media_files, skipped_videos


def _create_playlist_video_placeholder(
    db: Session,
    user_id: int,
    video_entry: dict[str, Any],
    playlist_info: dict[str, Any],
    playlist_url: str,
    organization_id: int | None = None,
) -> MediaFile:
    """
    Create a placeholder MediaFile for a playlist video.

    Args:
        db: Database session
        user_id: User ID
        video_entry: Video entry from playlist
        playlist_info: Playlist metadata
        playlist_url: Original playlist URL
        organization_id: Tenant of the ORIGINATING request (issue #262c) —
            never resolved from the owner's memberships here. None = personal.

    Returns:
        Created MediaFile
    """
    video_id = video_entry.get("video_id")
    video_url = video_entry.get("url")
    video_title = video_entry.get("title", "Unknown")
    playlist_index = video_entry.get("playlist_index", 1)

    placeholder_metadata = {
        "youtube_id": video_id,
        "youtube_url": video_url,
        "title": video_title,
        "processing": True,
        "from_playlist": True,
        "playlist_id": playlist_info.get("playlist_id"),
        "playlist_title": playlist_info.get("playlist_title"),
        "playlist_url": playlist_url,
        "playlist_index": playlist_index,
    }

    media_file = MediaFile(
        user_id=user_id,
        organization_id=organization_id,
        filename=video_title[:255],
        storage_path="",
        file_size=0,
        content_type="video/mp4",
        duration=video_entry.get("duration"),
        status=FileStatus.QUEUED,  # Playlist placeholder, waiting for download
        title=video_title,
        author=video_entry.get("uploader"),
        source_url=video_url,
        metadata_raw=placeholder_metadata,
        metadata_important=placeholder_metadata,
    )

    db.add(media_file)
    db.flush()

    return media_file


def _update_media_file_with_download_data(
    media_file: MediaFile,
    media_info: dict[str, Any],
    media_metadata: dict[str, Any],
    technical_metadata: dict[str, Any],
    storage_path: str,
    file_size: int,
    thumbnail_path: str | None,
    original_filename: str,
    source_url: str,
    imohash_value: str | None = None,
) -> None:
    """
    Update MediaFile record with downloaded media and technical metadata.

    Args:
        media_file: MediaFile to update
        media_info: Media video info from yt-dlp
        media_metadata: Prepared media metadata dict
        technical_metadata: Technical metadata from file
        storage_path: Storage path in MinIO
        file_size: File size in bytes
        thumbnail_path: Path to thumbnail
        original_filename: Original filename
        source_url: Original media URL
        imohash_value: Optional imohash fingerprint computed from the
            downloaded file. Enables server-side duplicate detection and
            artifact cache lookups across different source URLs pointing
            at the same content.
    """
    media_file.filename = media_info.get("title", original_filename)[:255]  # type: ignore[assignment]
    media_file.storage_path = storage_path  # type: ignore[assignment]
    media_file.file_size = file_size  # type: ignore[assignment]
    media_file.content_type = technical_metadata.get("content_type", "video/mp4")  # type: ignore[assignment]
    media_file.duration = technical_metadata.get("duration") or media_info.get("duration")  # type: ignore[assignment]
    media_file.status = FileStatus.PENDING  # type: ignore[assignment]
    media_file.thumbnail_path = thumbnail_path  # type: ignore[assignment]
    if imohash_value:
        media_file.imohash = imohash_value  # type: ignore[assignment]

    # Media-specific metadata
    media_file.title = media_info.get("title")  # type: ignore[assignment]
    media_file.author = media_info.get("uploader")  # type: ignore[assignment]
    media_file.description = media_info.get("description")  # type: ignore[assignment]
    media_file.source_url = source_url  # type: ignore[assignment]
    media_file.metadata_raw = media_metadata  # type: ignore[assignment]
    media_file.metadata_important = media_metadata  # type: ignore[assignment]

    # Technical metadata from extraction
    media_file.media_format = technical_metadata.get("format")  # type: ignore[assignment]
    media_file.codec = technical_metadata.get("video_codec")  # type: ignore[assignment]
    media_file.frame_rate = technical_metadata.get("frame_rate")  # type: ignore[assignment]
    media_file.resolution_width = technical_metadata.get("width")  # type: ignore[assignment]
    media_file.resolution_height = technical_metadata.get("height")  # type: ignore[assignment]
    media_file.audio_channels = technical_metadata.get("audio_channels")  # type: ignore[assignment]
    media_file.audio_sample_rate = technical_metadata.get("audio_sample_rate")  # type: ignore[assignment]


# Ordered height map for quality cascade (highest → lowest)
_QUALITY_HEIGHT_MAP: dict[str, int] = {
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}


def _build_yt_dlp_format_string(
    video_quality: str = "best",
    audio_only: bool = False,
    audio_quality: str = "best",
) -> str:
    """Build a yt-dlp format string based on quality settings.

    Used as the initial format spec (before actual available formats are known).
    Invalid quality values fall back to "best" with a warning rather than raising,
    so the download always proceeds.  The "/" separator means "or" — yt-dlp tries
    each option in order and uses the first that matches.

    Args:
        video_quality: Quality preference (best, 2160p, 1440p, 1080p, 720p, 480p, 360p)
        audio_only: If True, download only the audio track.
        audio_quality: Audio bitrate preference (best, 320, 192, 128).

    Returns:
        yt-dlp format selection string.
    """
    if video_quality not in VALID_VIDEO_QUALITIES:
        logger.warning("Unknown video_quality '%s'; falling back to 'best'", video_quality)
        video_quality = "best"
    if audio_quality not in VALID_AUDIO_QUALITIES:
        logger.warning("Unknown audio_quality '%s'; falling back to 'best'", audio_quality)
        audio_quality = "best"

    if audio_only:
        if audio_quality == "best":
            return "bestaudio[ext=m4a]/bestaudio/best"
        return f"bestaudio[abr<={audio_quality}][ext=m4a]/bestaudio[ext=m4a]/bestaudio/best"

    if video_quality == "best":
        return (
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec*=h264][ext=mp4]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/best"
        )

    height = _QUALITY_HEIGHT_MAP[video_quality]
    return (
        f"bestvideo[height<={height}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}][vcodec*=h264][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}][ext=mp4]+bestaudio/"
        f"best[height<={height}][ext=mp4]/"
        f"best[ext=mp4]/best"
    )


def _select_quality_from_available_formats(
    formats: list[dict[str, Any]],
    video_quality: str,
    audio_only: bool,
    audio_quality: str,
) -> tuple[str, str]:
    """Select the best matching format spec from the formats actually available.

    Inspects the format list returned by yt-dlp's ``extract_info`` and picks
    the highest resolution at or below the user's preference, stepping down
    through the quality cascade until a match is found.  Returns both the
    yt-dlp format spec and a human-readable description of what will actually
    be downloaded so callers can inform the user.

    Args:
        formats: List of format dicts from ``info['formats']`` (may be empty).
        video_quality: User's quality preference key (e.g. "1080p", "best").
        audio_only: If True, select an audio-only format.
        audio_quality: User's audio bitrate preference (e.g. "192", "best").

    Returns:
        Tuple of (yt_dlp_format_spec, human_readable_description).
    """
    if audio_only:
        if audio_quality == "best":
            return "bestaudio[ext=m4a]/bestaudio/best", "best available audio"
        return (
            f"bestaudio[abr<={audio_quality}][ext=m4a]/bestaudio[ext=m4a]/bestaudio/best",
            f"audio up to {audio_quality} kbps",
        )

    if video_quality == "best" or not formats:
        return (
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec*=h264][ext=mp4]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/best",
            "best available quality",
        )

    if video_quality not in _QUALITY_HEIGHT_MAP:
        logger.warning("Unknown video_quality '%s'; using best available", video_quality)
        return (
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "best available quality (unknown preference)",
        )

    # Collect heights that actually have a video stream
    available_heights: set[int] = {
        int(fmt["height"])
        for fmt in formats
        if fmt.get("height") and fmt.get("vcodec", "none") not in ("none", None)
    }

    if not available_heights:
        # No height metadata (e.g. direct-link platforms) — fall back to format string
        return _build_yt_dlp_format_string(video_quality, audio_only, audio_quality), video_quality

    requested_height = _QUALITY_HEIGHT_MAP[video_quality]
    # Walk from requested height downward; pick the highest available at or below
    cascade = sorted(_QUALITY_HEIGHT_MAP.values(), reverse=True)
    candidates = [h for h in cascade if h <= requested_height and h in available_heights]

    if candidates:
        selected_height = candidates[0]
        if selected_height < requested_height:
            logger.info(
                "Requested quality %s not available; stepping down to %dp",
                video_quality,
                selected_height,
            )
        description = f"{selected_height}p"
    else:
        # All available resolutions are higher than requested — use the lowest available
        # so the user still gets something (better than an empty download)
        selected_height = min(available_heights)
        logger.warning(
            "No quality at or below %s found (available: %s); "
            "video only exists in higher resolutions — using lowest available %dp",
            video_quality,
            sorted(available_heights, reverse=True),
            selected_height,
        )
        description = (
            f"{selected_height}p "
            f"(your preference of {video_quality} was unavailable; lowest available used)"
        )

    format_spec = (
        f"bestvideo[height<={selected_height}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={selected_height}][vcodec*=h264][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={selected_height}][ext=mp4]+bestaudio/"
        f"best[height<={selected_height}][ext=mp4]/"
        f"best[ext=mp4]/best"
    )
    return format_spec, description


class MediaDownloadService:
    """Service for processing media from various platforms.

    Uses yt-dlp for public platforms and a pluggable registry of
    ProtectedMediaProvider implementations for authenticated sites
    (for example, internal corporate media portals).
    """

    def __init__(self):
        pass

    def _get_protected_provider(
        self, url: str, user_id: int | None = None
    ) -> ProtectedMediaProvider | None:
        """Return a protected media provider that can handle this URL, if any."""
        for provider in PROTECTED_MEDIA_PROVIDERS:
            try:
                if provider.can_handle(url, user_id=user_id):
                    return provider
            except Exception as e:
                logger.warning(
                    f"Protected media provider {provider.__class__.__name__} "
                    f"failed in can_handle for {url}: {e}"
                )
        return None

    def is_valid_media_url(self, url: str) -> bool:
        """
        Validate if URL is a valid and safe media URL.

        Checks both URL format (HTTP/HTTPS) and SSRF safety
        (blocks private IPs, localhost, cloud metadata endpoints).

        Args:
            url: URL to validate

        Returns:
            True if valid and safe media URL, False otherwise
        """
        if not GENERIC_URL_PATTERN.match(url.strip()):
            return False
        safe, reason = is_safe_url(url)
        if not safe:
            logger.warning("SSRF blocked: %s for URL: %s", reason, url[:200])
            return False
        return True

    def is_youtube_url(self, url: str) -> bool:
        """
        Check if URL is a YouTube URL (for backward compatibility and special handling).

        Args:
            url: URL to check

        Returns:
            True if YouTube URL, False otherwise
        """
        return bool(YOUTUBE_URL_PATTERN.match(url.strip()))

    def is_playlist_url(self, url: str) -> bool:
        """
        Check if URL is a YouTube playlist URL.

        Args:
            url: URL to validate

        Returns:
            True if URL is a playlist, False if it's a single video
        """
        return bool(YOUTUBE_PLAYLIST_PATTERN.match(url.strip()))

    def extract_video_info(
        self,
        url: str,
        media_username: str | None = None,
        media_password: str | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Extract video metadata without downloading.

        For generic public platforms this uses yt-dlp. For URLs handled by
        a ProtectedMediaProvider, it delegates to that provider's custom API-based logic.

        Args:
            url: Media URL
            media_username: Optional username for protected media sources
            media_password: Optional password for protected media sources
            user_id: Optional user ID for per-user credential resolution

        Returns:
            Dictionary with video information

        Raises:
            HTTPException: If unable to extract video information
        """
        # Try protected media providers first (authenticated corporate sites, etc.)
        provider = self._get_protected_provider(url, user_id=user_id)
        if provider is not None:
            return provider.extract_info(
                url, username=media_username, password=media_password, user_id=user_id
            )

        ydl_opts = {
            **_YT_DLP_BASE_OPTS,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "extractor_args": _YOUTUBE_EXTRACTOR_ARGS,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info  # type: ignore[no-any-return]
        except yt_dlp.DownloadError as e:
            error_msg = str(e)
            logger.error(f"Error extracting video info from {url}: {error_msg}")
            # Create user-friendly error message
            user_friendly_error = create_user_friendly_error(error_msg, url)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to extract video information: {user_friendly_error}",
            ) from e
        except Exception as e:
            logger.error(f"Error extracting video info from {url}: {e}")
            user_friendly_error = create_user_friendly_error(str(e), url)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to extract video information: {user_friendly_error}",
            ) from e

    def extract_playlist_info(self, url: str) -> dict[str, Any]:
        """
        Extract playlist metadata and video list without downloading.

        Args:
            url: YouTube playlist URL

        Returns:
            Dictionary with playlist information including:
            - playlist_id: Playlist ID
            - playlist_title: Playlist title
            - playlist_uploader: Playlist creator
            - video_count: Number of videos
            - videos: List of video entries with URLs and basic info

        Raises:
            HTTPException: If unable to extract playlist information
        """
        ydl_opts = {
            **_YT_DLP_BASE_OPTS,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",  # Extract video info without downloading
            "skip_download": True,
            "extractor_args": _YOUTUBE_EXTRACTOR_ARGS,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                if not info:
                    raise ValueError("No playlist information found")

                # Extract video entries
                entries = info.get("entries", [])
                videos = []

                for idx, entry in enumerate(entries):
                    if entry:  # Some entries might be None (unavailable videos)
                        video_id = entry.get("id")
                        if video_id:
                            videos.append(
                                {
                                    "video_id": video_id,
                                    "url": f"https://www.youtube.com/watch?v={video_id}",
                                    "title": entry.get("title", "Unknown"),
                                    "duration": entry.get("duration"),
                                    "uploader": entry.get("uploader"),
                                    "playlist_index": idx + 1,
                                }
                            )

                return {
                    "playlist_id": info.get("id"),
                    "playlist_title": info.get("title", "Unknown Playlist"),
                    "playlist_uploader": info.get("uploader") or info.get("channel"),
                    "playlist_description": info.get("description"),
                    "video_count": len(videos),
                    "videos": videos,
                }

        except Exception as e:
            logger.error(f"Error extracting playlist info from {url}: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to extract playlist information: {str(e)}",
            ) from e

    def download_video(
        self,
        url: str,
        output_path: str,
        progress_callback: Callable[[int, str], None] | None = None,
        media_username: str | None = None,
        media_password: str | None = None,
        video_quality: str = "best",
        audio_only: bool = False,
        audio_quality: str = "best",
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Download video from media URL.

        For public platforms uses yt-dlp; for URLs recognized by a
        ProtectedMediaProvider it delegates to that provider.

        Args:
            url: Media URL
            output_path: Directory to save downloaded file
            progress_callback: Optional callback for progress updates
            user_id: Optional user ID for per-user credential resolution

        Returns:
            Dictionary with file path, filename, and video info

        Raises:
            HTTPException: If download fails
        """
        # Re-validate at FETCH time, not just at submission (issue #284 A0.2).
        # `is_valid_media_url` runs in the request handler, but the download executes
        # later on a Celery worker — a submitter controlling the domain can repoint DNS
        # in between and have the worker fetch an internal address. Re-checking here
        # closes that queue-delay window, which in practice is the whole attack.
        #
        # It does NOT fully close TOCTOU: yt-dlp resolves DNS itself at connect time and
        # follows redirects internally, so a sub-second rebind or a redirect to a private
        # host still gets through. Closing that needs egress restriction on the download
        # worker (deny RFC1918 + link-local at the network layer) — infrastructure, not
        # something this process can enforce.
        if not self.is_valid_media_url(url):
            raise HTTPException(
                status_code=400,
                detail="Invalid URL. Please enter a valid HTTP or HTTPS URL.",
            )

        # First, try pluggable protected-media providers
        provider = self._get_protected_provider(url, user_id=user_id)
        if provider is not None:
            return provider.download(
                url,
                output_path,
                progress_callback=progress_callback,
                username=media_username,
                password=media_password,
                user_id=user_id,
            )

        # Create progress hook function
        def progress_hook(d):
            if progress_callback and d.get("status") == "downloading":
                # Calculate progress percentage from downloaded_bytes and total_bytes
                total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded_bytes = d.get("downloaded_bytes", 0)

                if total_bytes and total_bytes > 0:
                    progress_percent = min(
                        int((downloaded_bytes / total_bytes) * 40) + 20, 60
                    )  # Map to 20-60% range
                    progress_callback(progress_percent, "Downloading video...")

        # Configure yt-dlp options for highest quality with web-compatible output
        ydl_opts = {
            **_YT_DLP_BASE_OPTS,
            # Initial format string — refined after extract_info reveals real availability
            "format": _build_yt_dlp_format_string(video_quality, audio_only, audio_quality),
            "outtmpl": os.path.join(output_path, "%(title)s.%(ext)s"),
            "restrictfilenames": True,  # Safe filenames (no special chars)
            "no_warnings": True,
            "ignoreerrors": False,
            "no_playlist": True,  # Single-video downloads only
            "max_filesize": 15 * 1024 * 1024 * 1024,  # 15 GB — matches upload limit
            # Always merge/remux to MP4 for broadest browser compatibility
            "merge_output_format": "mp4",
            # Cache and temp dirs
            "cachedir": str(settings.TEMP_DIR / "yt-dlp-cache"),
            "paths": {"temp": output_path},
            # 2026 YouTube client rotation — avoids SABR blocking.
            # 'android' and 'android_sdkless' are deprecated; ios_downgraded
            # and android_vr bypass server-side restrictions on many networks.
            "extractor_args": _YOUTUBE_EXTRACTOR_ARGS,
            # Browser impersonation headers (Chrome 131 — current stable Jan 2026)
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Sec-Fetch-Mode": "navigate",
            },
            # FFmpegVideoRemuxer: fast, lossless container remux to MP4.
            # Our format string already selects H.264+AAC streams so no
            # re-encoding is needed; FFmpegMetadata embeds title/uploader info.
            "postprocessors": [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "mp4",
                },
                {
                    "key": "FFmpegMetadata",
                    "add_metadata": True,
                },
            ],
        }

        # Audio-only mode adjustments
        if audio_only:
            ydl_opts["merge_output_format"] = "m4a"
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                }
            ]

        # Add progress hook if callback is provided
        if progress_callback:
            ydl_opts["progress_hooks"] = [progress_hook]

        # Cookie authentication for sign-in required videos
        if settings.YOUTUBE_COOKIE_FILE and os.path.exists(settings.YOUTUBE_COOKIE_FILE):
            # Explicit cookie file (for headless servers)
            ydl_opts["cookiefile"] = settings.YOUTUBE_COOKIE_FILE
            logger.info(f"Using cookie file: {settings.YOUTUBE_COOKIE_FILE}")
        elif settings.YOUTUBE_COOKIE_BROWSER:
            # Browser cookie extraction (for servers with browser installed)
            ydl_opts["cookiesfrombrowser"] = (settings.YOUTUBE_COOKIE_BROWSER, None, None, None)
            logger.info(f"Using cookies from browser: {settings.YOUTUBE_COOKIE_BROWSER}")

        try:
            # First context: extract info only (before download)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            # Refine format selection based on formats actually available for this video.
            # This ensures we cascade from the user's preferred quality down to the best
            # real match rather than relying solely on the yt-dlp selector string.
            format_spec, quality_description = _select_quality_from_available_formats(
                info.get("formats", []),
                video_quality,
                audio_only,
                audio_quality,
            )
            logger.info("Quality selection for %s: %s", url, quality_description)
            ydl_opts["format"] = format_spec

            # Check duration - for very long videos (>4hr), switch to audio-only
            duration = info.get("duration")
            if duration and duration > 14400:  # 4 hours limit
                logger.warning(
                    f"Video duration {duration:.0f}s exceeds 4hr limit, "
                    f"downloading audio-only for {url}"
                )
                ydl_opts["format"] = "bestaudio[ext=m4a]/bestaudio"
                ydl_opts["merge_output_format"] = "m4a"
                ydl_opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "m4a",
                    }
                ]
                quality_description = "audio only (video exceeds 4-hour limit)"

            # Second context: download with refined format selection
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # Find the downloaded file
            title = info.get("title", "video")
            ext = info.get("ext", "mp4")
            clean_title = re.sub(r"[^\w\-_\.]", "_", title)[:100]
            downloaded_file = _find_downloaded_file(output_path, clean_title, ext)

            # Detect actual file extension and content type
            from pathlib import Path

            actual_ext = Path(downloaded_file).suffix.lstrip(".")
            content_type_map = {
                "mp4": "video/mp4",
                "webm": "video/webm",
                "mkv": "video/x-matroska",
                "m4a": "audio/mp4",
                "mp3": "audio/mpeg",
                "ogg": "audio/ogg",
                "wav": "audio/wav",
                "flac": "audio/flac",
            }
            actual_content_type = content_type_map.get(actual_ext, f"application/{actual_ext}")

            return {
                "file_path": downloaded_file,
                "filename": os.path.basename(downloaded_file),
                "content_type": actual_content_type,
                "info": info,
                "quality_description": quality_description,
            }

        except yt_dlp.DownloadError as e:
            error_msg = str(e)
            logger.error(f"yt-dlp download error for {url}: {error_msg}")
            # Create user-friendly error message
            user_friendly_error = create_user_friendly_error(error_msg, url)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to download video: {user_friendly_error}",
            ) from e
        except Exception as e:
            logger.error(f"Unexpected error downloading {url}: {e}")
            user_friendly_error = create_user_friendly_error(str(e), url)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected error during download: {user_friendly_error}",
            ) from e

    def _extract_technical_metadata(self, file_path: str) -> dict[str, Any]:
        """
        Extract technical metadata from downloaded file.

        Args:
            file_path: Path to the downloaded file

        Returns:
            Dictionary with technical metadata
        """
        try:
            # Use the existing metadata extraction service
            from app.tasks.transcription.metadata_extractor import extract_media_metadata
            from app.tasks.transcription.metadata_extractor import get_important_metadata

            raw_metadata = extract_media_metadata(file_path)
            if raw_metadata:
                important_metadata = get_important_metadata(raw_metadata)

                # Convert to format expected by MediaFile model
                return {
                    "content_type": raw_metadata.get("File:MIMEType", "video/mp4"),
                    "format": important_metadata.get("FileType"),
                    "video_codec": important_metadata.get("VideoCodec"),
                    "width": important_metadata.get("VideoWidth"),
                    "height": important_metadata.get("VideoHeight"),
                    "frame_rate": important_metadata.get("VideoFrameRate"),
                    "audio_channels": important_metadata.get("AudioChannels"),
                    "audio_sample_rate": important_metadata.get("AudioSampleRate"),
                    "duration": important_metadata.get("Duration"),
                }
            else:
                logger.warning("No metadata extracted, using fallback")
                return self._extract_basic_metadata(file_path)
        except Exception as e:
            logger.warning(f"Failed to extract technical metadata: {e}")
            return self._extract_basic_metadata(file_path)

    def _safe_frame_rate_eval(self, frame_rate_str: str) -> float | None:
        """
        Safely evaluate frame rate string like '30/1' or '29.97'.

        Args:
            frame_rate_str: Frame rate string from ffprobe

        Returns:
            Frame rate as float or None if invalid
        """
        try:
            if "/" in frame_rate_str:
                numerator, denominator = frame_rate_str.split("/")
                return float(numerator) / float(denominator)
            else:
                return float(frame_rate_str)
        except (ValueError, ZeroDivisionError):
            logger.warning(f"Invalid frame rate format: {frame_rate_str}")
            return None

    def _extract_basic_metadata(self, file_path: str) -> dict[str, Any]:
        """
        Fallback method to extract basic metadata using ffprobe.

        Args:
            file_path: Path to the media file

        Returns:
            Dictionary with basic metadata
        """
        try:
            import ffmpeg  # type: ignore[import-untyped]

            probe = ffmpeg.probe(file_path)
            format_info = probe.get("format", {})
            video_stream = next(
                (stream for stream in probe["streams"] if stream["codec_type"] == "video"),
                None,
            )
            audio_stream = next(
                (stream for stream in probe["streams"] if stream["codec_type"] == "audio"),
                None,
            )

            metadata = {
                "content_type": "video/mp4",  # Default
                "format": format_info.get("format_name"),
                "duration": float(format_info.get("duration", 0)),
            }

            if video_stream:
                metadata.update(
                    {
                        "video_codec": video_stream.get("codec_name"),
                        "width": video_stream.get("width"),
                        "height": video_stream.get("height"),
                        "frame_rate": self._safe_frame_rate_eval(video_stream.get("r_frame_rate"))
                        if video_stream.get("r_frame_rate")
                        else None,
                    }
                )

            if audio_stream:
                metadata.update(
                    {
                        "audio_channels": audio_stream.get("channels"),
                        "audio_sample_rate": audio_stream.get("sample_rate"),
                    }
                )

            return metadata

        except Exception as e:
            logger.warning(f"Failed to extract basic metadata: {e}")
            return {"content_type": "video/mp4"}

    def _prepare_media_metadata(self, url: str, media_info: dict[str, Any]) -> dict[str, Any]:
        """
        Prepare media-specific metadata for storage.

        Args:
            url: Original media URL
            media_info: Information extracted from yt-dlp

        Returns:
            Dictionary with media metadata
        """
        # Detect the source platform dynamically
        source = media_info.get("extractor", "unknown").lower()

        # Build metadata with platform-agnostic keys plus platform-specific ones
        metadata = {
            "source": source,
            "original_url": url,
            "video_id": media_info.get("id"),
            "title": media_info.get("title"),
            "description": media_info.get("description"),
            "uploader": media_info.get("uploader"),
            "upload_date": media_info.get("upload_date"),
            "duration": media_info.get("duration"),
            "view_count": media_info.get("view_count"),
            "like_count": media_info.get("like_count"),
            "thumbnail": media_info.get("thumbnail"),
            "tags": media_info.get("tags", []),
            "categories": media_info.get("categories", []),
        }

        # Add YouTube-specific fields for backward compatibility
        if "youtube" in source:
            metadata.update(
                {
                    "youtube_id": media_info.get("id"),
                    "youtube_title": media_info.get("title"),
                    "youtube_description": media_info.get("description"),
                    "youtube_uploader": media_info.get("uploader"),
                    "youtube_upload_date": media_info.get("upload_date"),
                    "youtube_duration": media_info.get("duration"),
                    "youtube_view_count": media_info.get("view_count"),
                    "youtube_like_count": media_info.get("like_count"),
                    "youtube_thumbnail": media_info.get("thumbnail"),
                    "youtube_tags": media_info.get("tags", []),
                    "youtube_categories": media_info.get("categories", []),
                }
            )

        return metadata

    def _download_media_thumbnail_sync(
        self, media_info: dict[str, Any], user_id: int
    ) -> str | None:
        """
        Download media thumbnail and upload to storage (synchronous version).

        Args:
            media_info: Media metadata from yt-dlp
            user_id: User ID for storage path

        Returns:
            Storage path of uploaded thumbnail or None if failed
        """
        try:
            thumbnail_url = _resolve_thumbnail_url(media_info)

            if not thumbnail_url:
                logger.warning("No thumbnail URL found in media metadata")
                return None

            # `is_safe_url` above (`:846`) only validates the SUBMITTED page URL — this is
            # yt-dlp's EXTRACTED metadata for that page, which a malicious page (via a
            # generic extractor) can point at an arbitrary internal/link-local target. Same
            # SSRF class as the submitted-URL check; same guard, applied to what is actually
            # fetched.
            safe, reason = is_safe_url(thumbnail_url)
            if not safe:
                logger.warning(
                    "SSRF blocked: %s for thumbnail URL: %s", reason, thumbnail_url[:200]
                )
                return None

            # Download the thumbnail
            response = requests.get(thumbnail_url, timeout=30)
            response.raise_for_status()
            thumbnail_data = response.content

            if not thumbnail_data:
                logger.warning("Empty thumbnail data received")
                return None

            # Generate storage path and upload
            video_id = media_info.get("id", "unknown")
            source = media_info.get("extractor", "media").lower()
            storage_path = f"user_{user_id}/{source}_{video_id}/thumbnail.jpg"

            upload_file(
                file_content=io.BytesIO(thumbnail_data),
                file_size=len(thumbnail_data),
                object_name=storage_path,
                content_type="image/jpeg",
            )

            logger.info(f"Successfully downloaded and uploaded media thumbnail: {storage_path}")
            return storage_path

        except Exception as e:
            logger.error(f"Error downloading media thumbnail: {e}")
            return None

    def process_media_url_sync(
        self,
        url: str,
        db: Session,
        user: User,
        media_file: MediaFile,
        progress_callback: Callable[[int, str], None] | None = None,
        media_username: str | None = None,
        media_password: str | None = None,
        video_quality: str = "best",
        audio_only: bool = False,
        audio_quality: str = "best",
        benchmark_task_id: str | None = None,
    ) -> MediaFile:
        """
        Process a media URL by downloading the video and updating the MediaFile record (synchronous).

        Args:
            url: Media URL to process
            db: Database session
            user: User requesting the processing
            media_file: Pre-created MediaFile to update
            progress_callback: Optional callback for progress updates
            benchmark_task_id: Optional application-level task_id minted upstream
                (e.g., in ``process_youtube_url_task``) so per-stage wall-clock
                markers join the same ``benchmark:{task_id}`` Redis hash as the
                downstream transcription pipeline. No-op when falsy or when
                ``ENABLE_BENCHMARK_TIMING`` is off.

        Returns:
            Updated MediaFile object

        Raises:
            HTTPException: If processing fails
        """
        from app.utils import benchmark_timing

        if not self.is_valid_media_url(url):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid media URL")

        # Phase 1 — read the two identifiers the download phase needs, then END
        # the caller's transaction. A yt-dlp download plus a multi-gigabyte MinIO
        # upload used to run with it open, holding ACCESS SHARE on media_file for
        # the whole ingest (``app/tasks/CLAUDE.md``). Committing here is safe:
        # this method already commits below, so no caller relies on it leaving
        # work pending.
        resolve_user_id = int(user.id) if user else None
        file_id = int(media_file.id)
        db.commit()

        # Phase 2 — download + upload. NO transaction is held.
        staged = self._download_and_stage_media(
            url=url,
            user_id=resolve_user_id,
            file_id=file_id,
            progress_callback=progress_callback,
            media_username=media_username,
            media_password=media_password,
            video_quality=video_quality,
            audio_only=audio_only,
            audio_quality=audio_quality,
            benchmark_task_id=benchmark_task_id,
        )

        if progress_callback:
            progress_callback(95, "Finalizing and updating database...")

        # Phase 3 — write (DB only).
        _update_media_file_with_download_data(
            media_file=media_file,
            media_info=staged["media_info"],
            media_metadata=staged["media_metadata"],
            technical_metadata=staged["technical_metadata"],
            storage_path=staged["storage_path"],
            file_size=staged["file_size"],
            thumbnail_path=staged["thumbnail_path"],
            original_filename=staged["original_filename"],
            source_url=url,
            imohash_value=staged["imohash_value"],
        )
        with benchmark_timing.stage(benchmark_task_id, "db_commit"):
            db.commit()
            db.refresh(media_file)

        # Surface per-file context (size + MIME) so the analysis layer can
        # slice timing by file characteristics later.
        benchmark_timing.set_context(
            benchmark_task_id,
            {
                "file_size_bytes": int(staged["file_size"]) if staged["file_size"] else 0,
                "content_type": staged["technical_metadata"].get("content_type") or "",
            },
        )

        logger.info(f"Updated MediaFile record {media_file.id} for media video")

        return media_file

    def _download_and_stage_media(
        self,
        *,
        url: str,
        user_id: int | None,
        file_id: int,
        progress_callback: Callable[[int, str], None] | None = None,
        media_username: str | None = None,
        media_password: str | None = None,
        video_quality: str = "best",
        audio_only: bool = False,
        audio_quality: str = "best",
        benchmark_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Download a media URL and stage it in object storage.

        **Takes no DB session and opens none.** This is the slow half of
        :meth:`process_media_url_sync` — yt-dlp extraction, the download itself,
        ffprobe metadata, the imohash sample, the MinIO upload and the thumbnail
        — and it must run with the caller's transaction closed.

        Args:
            url: Media URL to download.
            user_id: Owner id; the storage key is namespaced by it.
            file_id: Internal media file id, used for thumbnail naming.
            progress_callback: Optional ``(percent, message)`` callback.
            media_username: Optional site credential.
            media_password: Optional site credential.
            video_quality: yt-dlp video quality selector.
            audio_only: Download audio only.
            audio_quality: yt-dlp audio quality selector.
            benchmark_task_id: Optional task id for per-stage timing markers.

        Returns:
            Plain data for the caller's write phase: ``media_info``,
            ``media_metadata``, ``technical_metadata``, ``storage_path``,
            ``file_size``, ``thumbnail_path``, ``original_filename`` and
            ``imohash_value``.

        Raises:
            HTTPException: If the video id cannot be extracted.
        """
        from app.utils import benchmark_timing

        resolve_user_id = user_id

        # Extract video info first to get video ID
        logger.debug(f"Extracting video information for URL: {url}")
        video_info = self.extract_video_info(
            url,
            media_username=media_username,
            media_password=media_password,
            user_id=resolve_user_id,
        )
        video_id = video_info.get("id")

        if not video_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not extract video ID from URL",
            )

        # Create temporary directory for download in configured TEMP_DIR
        # This ensures the non-root container user has write permissions
        temp_dir = tempfile.mkdtemp(prefix="media_download_", dir=str(settings.TEMP_DIR))

        try:
            if progress_callback:
                progress_callback(15, "Preparing for download...")

            # Download the video using the already extracted info (this will use 20-60% progress range)
            logger.info(f"Starting media download for URL: {url}")
            with benchmark_timing.stage(benchmark_task_id, "url_download"):
                download_result = self.download_video(
                    url,
                    temp_dir,
                    progress_callback=progress_callback,
                    media_username=media_username,
                    media_password=media_password,
                    video_quality=video_quality,
                    audio_only=audio_only,
                    audio_quality=audio_quality,
                    user_id=resolve_user_id,
                )

            if progress_callback:
                progress_callback(65, "Video downloaded, processing metadata...")

            downloaded_file = download_result["file_path"]
            original_filename = download_result["filename"]
            media_info = video_info  # Use the info we already extracted

            # Get file stats
            file_stats = os.stat(downloaded_file)
            file_size = file_stats.st_size

            # Extract technical metadata from downloaded file first
            technical_metadata = self._extract_technical_metadata(downloaded_file)

            if progress_callback:
                progress_callback(75, "Uploading to storage...")

            # Generate unique storage path
            file_uuid = str(uuid.uuid4())
            file_extension = Path(downloaded_file).suffix
            storage_path = f"media/{resolve_user_id}/{file_uuid}{file_extension}"

            # Compute imohash against the local downloaded file BEFORE the
            # upload clears it from disk. imohash samples three small byte
            # ranges (≤384 KiB total) — constant-time regardless of file
            # size, and cheap enough to run on every URL ingest. Used for
            # server-side duplicate detection + artifact cache keys
            # (Phase 2 PR #9 — URL ingress parity with direct uploads).
            imohash_value: str | None = None
            try:
                from app.services.imohash_service import compute_from_stream

                with (
                    benchmark_timing.stage(benchmark_task_id, "imohash"),
                    open(downloaded_file, "rb") as imo_fp,
                ):
                    imohash_value = compute_from_stream(imo_fp, file_size)
            except Exception as imo_err:
                logger.debug(f"imohash compute failed for {url} (non-fatal): {imo_err}")

            # Upload to MinIO. Use the tuned uploader so large yt-dlp files
            # use 64 MiB multipart parts (~12× fewer parts than the minio-py
            # 5 MB default) — Phase 2 PR #6.
            logger.info(f"Uploading downloaded video to MinIO: {storage_path}")
            with (
                benchmark_timing.stage(benchmark_task_id, "minio_put"),
                open(downloaded_file, "rb") as f,
            ):
                file_content = io.BytesIO(f.read())
                upload_file_tuned(
                    file_content=file_content,
                    file_size=file_size,
                    object_name=storage_path,
                    content_type=technical_metadata.get("content_type", "video/mp4"),
                )

            if progress_callback:
                progress_callback(85, "Processing thumbnails...")

            # Thumbnail derivation. If the source already provides a thumbnail
            # URL (YouTube, Vimeo, etc.) we fetch it inline — a single ~60 KB
            # HTTP GET is cheap and keeps the row usable immediately. Only the
            # expensive FFmpeg-frame-extraction fallback is deferred to an
            # async task (Phase 2 PR #9, item 3).
            thumbnail_path: str | None = None
            with benchmark_timing.stage(benchmark_task_id, "thumbnail"):
                thumbnail_path = _get_thumbnail_with_fallback(
                    self,
                    media_info,
                    int(resolve_user_id) if resolve_user_id is not None else 0,
                    file_id,
                    downloaded_file,
                    defer_fallback_generation=True,
                    storage_path=storage_path,
                )

            # Prepare media metadata for the caller's write phase.
            media_metadata = self._prepare_media_metadata(url, media_info)

            return {
                "media_info": media_info,
                "media_metadata": media_metadata,
                "technical_metadata": technical_metadata,
                "storage_path": storage_path,
                "file_size": file_size,
                "thumbnail_path": thumbnail_path,
                "original_filename": original_filename,
                "imohash_value": imohash_value,
            }

        finally:
            # Clean up temporary files
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.debug(f"Cleaned up temporary directory: {temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory {temp_dir}: {e}")

    def process_youtube_playlist_sync(
        self,
        url: str,
        db: Session,
        user: User,
        progress_callback: Callable[[int, str, dict], None] | None = None,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Process a YouTube playlist by extracting video list and creating placeholder MediaFile records.

        This method extracts the playlist information and creates MediaFile records for each video.
        Individual video downloads are handled by separate Celery tasks for parallel processing.

        Args:
            url: YouTube playlist URL
            db: Database session
            user: User requesting the processing
            progress_callback: Optional callback for progress updates (progress, message, data)

        Returns:
            Dictionary containing:
            - playlist_info: Playlist metadata
            - media_files: List of created MediaFile records
            - skipped_videos: List of videos that were skipped (duplicates or errors)

        Raises:
            HTTPException: If playlist extraction fails
        """
        if not self.is_playlist_url(url):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="URL is not a valid YouTube playlist",
            )

        # Extract playlist information
        logger.info(f"Extracting playlist information from: {url}")
        if progress_callback:
            progress_callback(10, "Extracting playlist information...", {})

        try:
            playlist_info = self.extract_playlist_info(url)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error extracting playlist info: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to extract playlist information: {str(e)}",
            ) from e

        video_count = playlist_info.get("video_count", 0)
        if video_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Playlist is empty or contains no accessible videos",
            )

        logger.info(
            f"Found {video_count} videos in playlist: {playlist_info.get('playlist_title')}"
        )
        if progress_callback:
            progress_callback(
                20,
                f"Found {video_count} videos in playlist...",
                {"video_count": video_count, "playlist_title": playlist_info.get("playlist_title")},
            )

        # Create placeholder MediaFile records for each video
        videos = playlist_info.get("videos", [])
        created_media_files, skipped_videos = _process_playlist_videos(
            db,
            int(user.id),
            videos,
            playlist_info,
            url,
            video_count,
            progress_callback,
            organization_id=organization_id,
        )

        # Commit and refresh all placeholder records
        db.commit()
        for media_file in created_media_files:
            db.refresh(media_file)

        if progress_callback:
            progress_callback(
                100,
                f"Playlist processing complete: {len(created_media_files)} videos queued",
                {"created_count": len(created_media_files), "skipped_count": len(skipped_videos)},
            )

        logger.info(
            f"Playlist processing complete: {len(created_media_files)} videos created, "
            f"{len(skipped_videos)} skipped"
        )

        return {
            "playlist_info": playlist_info,
            "media_files": created_media_files,
            "skipped_videos": skipped_videos,
            "created_count": len(created_media_files),
            "skipped_count": len(skipped_videos),
            "total_videos": video_count,
        }

    # Backward compatibility aliases
    def is_valid_youtube_url(self, url: str) -> bool:
        """Alias for is_valid_media_url for backward compatibility."""
        return self.is_valid_media_url(url)

    def process_youtube_url_sync(
        self,
        url: str,
        db: Session,
        user: User,
        media_file: MediaFile,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> MediaFile:
        """Alias for process_media_url_sync for backward compatibility."""
        return self.process_media_url_sync(url, db, user, media_file, progress_callback)

    def _prepare_youtube_metadata(self, url: str, youtube_info: dict[str, Any]) -> dict[str, Any]:
        """Alias for _prepare_media_metadata for backward compatibility."""
        return self._prepare_media_metadata(url, youtube_info)

    def _download_youtube_thumbnail_sync(
        self, youtube_info: dict[str, Any], user_id: int
    ) -> str | None:
        """Alias for _download_media_thumbnail_sync for backward compatibility."""
        return self._download_media_thumbnail_sync(youtube_info, user_id)


# Backward compatibility alias
YouTubeService = MediaDownloadService
