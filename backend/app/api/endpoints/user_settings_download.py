"""Download settings endpoints under ``/user-settings/download``."""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.user_settings_common import _upsert_user_setting
from app.core.constants import AUDIO_QUALITY_OPTIONS
from app.core.constants import DEFAULT_AUDIO_ONLY
from app.core.constants import DEFAULT_AUDIO_QUALITY
from app.core.constants import DEFAULT_VIDEO_QUALITY
from app.core.constants import VALID_AUDIO_QUALITIES
from app.core.constants import VALID_VIDEO_QUALITIES
from app.core.constants import VIDEO_QUALITY_OPTIONS
from app.db.base import get_db
from app.schemas.download_settings import DownloadSettings
from app.schemas.download_settings import DownloadSettingsUpdate
from app.schemas.download_settings import DownloadSystemDefaults

router = APIRouter()


# Default values for download settings
DEFAULT_DOWNLOAD_SETTINGS = {
    "video_quality": DEFAULT_VIDEO_QUALITY,
    "audio_only": DEFAULT_AUDIO_ONLY,
    "audio_quality": DEFAULT_AUDIO_QUALITY,
}


def _validate_video_quality(quality: str | None) -> None:
    """Validate video_quality value."""
    if quality is not None and quality not in VALID_VIDEO_QUALITIES:
        raise HTTPException(
            status_code=400,
            detail=f"video_quality must be one of: {VALID_VIDEO_QUALITIES}",
        )


def _validate_audio_quality(quality: str | None) -> None:
    """Validate audio_quality value."""
    if quality is not None and quality not in VALID_AUDIO_QUALITIES:
        raise HTTPException(
            status_code=400,
            detail=f"audio_quality must be one of: {VALID_AUDIO_QUALITIES}",
        )


@router.get("/download", response_model=DownloadSettings)
def get_download_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> DownloadSettings:
    """Get user's download quality settings."""
    download_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(
                [
                    "download_video_quality",
                    "download_audio_only",
                    "download_audio_quality",
                ]
            ),
        )
        .all()
    )

    settings_map: dict[str, str] = {
        str(setting.setting_key): str(setting.setting_value) for setting in download_settings
    }

    return DownloadSettings(
        video_quality=settings_map.get("download_video_quality", DEFAULT_VIDEO_QUALITY),
        audio_only=settings_map.get("download_audio_only", "false").lower() == "true",
        audio_quality=settings_map.get("download_audio_quality", DEFAULT_AUDIO_QUALITY),
    )


@router.put("/download", response_model=DownloadSettings)
def update_download_settings(
    *,
    db: Session = Depends(get_db),
    settings_data: DownloadSettingsUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> DownloadSettings:
    """Update user's download quality settings."""
    update_data = settings_data.model_dump(exclude_none=True)

    if not update_data:
        return get_download_settings(db=db, current_user=current_user)  # type: ignore[no-any-return]

    _validate_video_quality(update_data.get("video_quality"))
    _validate_audio_quality(update_data.get("audio_quality"))

    setting_mappings = {
        "video_quality": "download_video_quality",
        "audio_only": "download_audio_only",
        "audio_quality": "download_audio_quality",
    }

    for frontend_key, value in update_data.items():
        db_key = setting_mappings.get(frontend_key)
        if db_key is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown download setting field: '{frontend_key}'",
            )
        _upsert_user_setting(db, current_user.id, db_key, value)

    db.commit()

    return get_download_settings(db=db, current_user=current_user)  # type: ignore[no-any-return]


@router.delete("/download")
def reset_download_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Reset user's download settings to defaults."""
    deleted_count = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(
                [
                    "download_video_quality",
                    "download_audio_only",
                    "download_audio_quality",
                ]
            ),
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    return {
        "message": f"Download settings reset to defaults. Removed {deleted_count} custom settings.",
        "default_settings": DEFAULT_DOWNLOAD_SETTINGS,
    }


@router.get("/download/system-defaults", response_model=DownloadSystemDefaults)
def get_download_system_defaults(
    current_user: models.User = Depends(get_current_active_user),
) -> DownloadSystemDefaults:
    """Get system-level download defaults and available options."""
    return DownloadSystemDefaults(
        video_quality=DEFAULT_VIDEO_QUALITY,
        audio_only=DEFAULT_AUDIO_ONLY,
        audio_quality=DEFAULT_AUDIO_QUALITY,
        available_video_qualities=VIDEO_QUALITY_OPTIONS,
        available_audio_qualities=AUDIO_QUALITY_OPTIONS,
    )
