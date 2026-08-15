"""
Pydantic schemas for user download quality settings.

These schemas define the request/response models for user-level download
preferences including video quality, audio-only mode, and audio bitrate.
"""

from pydantic import BaseModel
from pydantic import Field

from app.core.constants import AUDIO_QUALITY_OPTIONS
from app.core.constants import DEFAULT_AUDIO_ONLY
from app.core.constants import DEFAULT_AUDIO_QUALITY
from app.core.constants import DEFAULT_VIDEO_QUALITY
from app.core.constants import VIDEO_QUALITY_OPTIONS


class DownloadSettings(BaseModel):
    """Response schema for user download settings."""

    video_quality: str = Field(
        default=DEFAULT_VIDEO_QUALITY,
        description="Video quality preference for URL downloads",
    )
    audio_only: bool = Field(
        default=DEFAULT_AUDIO_ONLY,
        description="Download only audio (no video)",
    )
    audio_quality: str = Field(
        default=DEFAULT_AUDIO_QUALITY,
        description="Audio bitrate preference for audio-only downloads",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "video_quality": "best",
                "audio_only": False,
                "audio_quality": "best",
            }
        }


class DownloadSettingsUpdate(BaseModel):
    """Request schema for updating user download settings. All fields optional.

    ``extra="forbid"`` so an unknown key is a 422 instead of being silently dropped.
    Without it, Pydantic discarded unrecognised keys, ``update_data`` came back empty and
    the handler returned the *current* settings with a 200 — so a client with a typo
    (``videoQuality`` for ``video_quality``) got a success response and no change, and
    ``update_download_settings``' own ``Unknown download setting field`` 422 could never
    fire. Safe to tighten: the only caller is `frontend/src/lib/api/downloadSettings.ts`,
    whose `DownloadSettingsUpdate` interface declares exactly these three fields.
    """

    video_quality: str | None = Field(
        default=None,
        description="Video quality preference for URL downloads",
    )
    audio_only: bool | None = Field(
        default=None,
        description="Download only audio (no video)",
    )
    audio_quality: str | None = Field(
        default=None,
        description="Audio bitrate preference for audio-only downloads",
    )

    class Config:
        # Set here rather than as a `model_config` dict: pydantic raises
        # `config-both` if a class-based `Config` and `model_config` are both present,
        # and this module uses the class-based style throughout.
        extra = "forbid"
        json_schema_extra = {
            "example": {
                "video_quality": "1080p",
                "audio_only": False,
                "audio_quality": "best",
            }
        }


class DownloadSystemDefaults(BaseModel):
    """Response schema for system-level download defaults and available options."""

    video_quality: str = Field(
        default=DEFAULT_VIDEO_QUALITY,
        description="Default video quality setting",
    )
    audio_only: bool = Field(
        default=DEFAULT_AUDIO_ONLY,
        description="Default audio-only setting",
    )
    audio_quality: str = Field(
        default=DEFAULT_AUDIO_QUALITY,
        description="Default audio quality setting",
    )
    available_video_qualities: dict[str, str] = Field(
        default=VIDEO_QUALITY_OPTIONS,
        description="Available video quality options (key -> display label)",
    )
    available_audio_qualities: dict[str, str] = Field(
        default=AUDIO_QUALITY_OPTIONS,
        description="Available audio quality options (key -> display label)",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "video_quality": "best",
                "audio_only": False,
                "audio_quality": "best",
                "available_video_qualities": {
                    "best": "Best Available",
                    "1080p": "1080p (Full HD)",
                    "720p": "720p (HD)",
                },
                "available_audio_qualities": {
                    "best": "Best Available",
                    "320": "320 kbps",
                    "192": "192 kbps",
                },
            }
        }
