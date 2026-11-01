"""Request/response models and URL validation for ``POST /files/process-url``.

The URL gate here (``_validate_media_url``) is the SSRF/egress check every URL import
passes before any extraction or task dispatch.
"""

import re

from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.core.constants import VALID_AUDIO_QUALITIES
from app.core.constants import VALID_VIDEO_QUALITIES
from app.schemas.media import MediaFile as MediaFileSchema
from app.services.media_download_service import MediaDownloadService


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
    min_speakers: int | None = Field(
        default=None,
        ge=1,
        description="Minimum speakers for diarization. None = the user's saved range.",
    )
    max_speakers: int | None = Field(
        default=None,
        ge=1,
        description="Maximum speakers for diarization. None = the user's saved range.",
    )
    num_speakers: int | None = Field(
        default=None,
        ge=1,
        description="Fixed speaker count for diarization (overrides min/max when set).",
    )
    whisper_model: str | None = Field(
        default=None,
        description=(
            "Whisper model for this import. None = the deployment's model. Accepts that "
            "model or a lightweight CPU model (tiny, base); anything else is rejected with 422."
        ),
    )

    @model_validator(mode="after")
    def validate_min_max_speakers(self) -> "URLProcessingRequest":
        """min_speakers must not exceed max_speakers."""
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError("min_speakers must be less than or equal to max_speakers")
        return self

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
