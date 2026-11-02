"""yt-dlp base options and download format / quality selection."""

import logging
from typing import Any

from app.core.constants import VALID_AUDIO_QUALITIES
from app.core.constants import VALID_VIDEO_QUALITIES

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
YT_DLP_BASE_OPTS: dict[str, object] = {
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
YOUTUBE_EXTRACTOR_ARGS: dict[str, dict] = {
    "youtube": {
        "player_client": ["default", "web_safari", "ios_downgraded", "android_vr"],
        "skip": ["translated_subs"],
    }
}


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
