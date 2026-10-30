"""Recording settings endpoints under ``/user-settings/recording``."""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.core.constants import DEFAULT_RECORDING_AUTO_STOP
from app.core.constants import DEFAULT_RECORDING_MAX_DURATION
from app.core.constants import DEFAULT_RECORDING_QUALITY
from app.core.constants import VALID_RECORDING_DURATIONS
from app.core.constants import VALID_RECORDING_QUALITIES
from app.db.base import get_db

router = APIRouter()


# Default values for recording settings (when user hasn't set custom values)
DEFAULT_RECORDING_SETTINGS = {
    "max_recording_duration": DEFAULT_RECORDING_MAX_DURATION,
    "recording_quality": DEFAULT_RECORDING_QUALITY,
    "auto_stop_enabled": DEFAULT_RECORDING_AUTO_STOP,
}


@router.get("/recording", response_model=dict[str, Any])
def get_recording_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get user's recording settings.

    Returns settings with defaults for any values not customized by the user.
    Maps database keys to frontend-friendly keys.

    Returns:
        Dict containing max_recording_duration, recording_quality, and auto_stop_enabled
    """
    # Get all recording-related settings for the user
    recording_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(
                ["recording_max_duration", "recording_quality", "recording_auto_stop"]
            ),
        )
        .all()
    )

    # Build settings map from database results
    settings_map: dict[str, str] = {
        str(setting.setting_key): str(setting.setting_value) for setting in recording_settings
    }

    # Map database keys to frontend keys and provide defaults for missing values
    max_duration_value = settings_map.get(
        "recording_max_duration",
        str(DEFAULT_RECORDING_SETTINGS["max_recording_duration"]),
    )
    settings = {
        "max_recording_duration": int(max_duration_value),
        "recording_quality": settings_map.get(
            "recording_quality", DEFAULT_RECORDING_SETTINGS["recording_quality"]
        ),
        "auto_stop_enabled": settings_map.get("recording_auto_stop", "true").lower() == "true",
    }

    return settings


@router.put("/recording", response_model=dict[str, Any])
def update_recording_settings(
    *,
    db: Session = Depends(get_db),
    settings_data: dict[str, Any],
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Update user's recording settings.

    Validates input data and updates only the provided settings.
    Creates new settings if they don't exist, updates existing ones otherwise.

    Args:
        settings_data: Dict with optional keys: max_recording_duration,
                      recording_quality, auto_stop_enabled

    Returns:
        Updated settings dict with all current values

    Raises:
        HTTPException: If validation fails for any setting
    """
    # Validate incoming data
    valid_keys = {"max_recording_duration", "recording_quality", "auto_stop_enabled"}
    if not all(key in valid_keys for key in settings_data):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid setting keys. Valid keys are: {valid_keys}",
        )

    # Validate specific values
    if "max_recording_duration" in settings_data:
        duration = settings_data["max_recording_duration"]
        if not isinstance(duration, int) or duration not in VALID_RECORDING_DURATIONS:
            valid_durations = ", ".join(map(str, VALID_RECORDING_DURATIONS))
            raise HTTPException(
                status_code=400,
                detail=f"max_recording_duration must be one of: {valid_durations} (minutes)",
            )

    if "recording_quality" in settings_data:
        quality = settings_data["recording_quality"]
        if quality not in VALID_RECORDING_QUALITIES:
            valid_qualities = ", ".join(VALID_RECORDING_QUALITIES)
            raise HTTPException(
                status_code=400,
                detail=f"recording_quality must be one of: {valid_qualities}",
            )

    if "auto_stop_enabled" in settings_data:
        auto_stop = settings_data["auto_stop_enabled"]
        if not isinstance(auto_stop, bool):
            raise HTTPException(status_code=400, detail="auto_stop_enabled must be a boolean")

    # Map frontend keys to database keys
    setting_mappings = {
        "max_recording_duration": "recording_max_duration",
        "recording_quality": "recording_quality",
        "auto_stop_enabled": "recording_auto_stop",
    }

    for frontend_key, value in settings_data.items():
        db_key = setting_mappings[frontend_key]

        # Convert value to string for database storage
        db_value = ("true" if value else "false") if isinstance(value, bool) else str(value)

        # Check if setting already exists
        existing_setting = (
            db.query(models.UserSetting)
            .filter(
                models.UserSetting.user_id == current_user.id,
                models.UserSetting.setting_key == db_key,
            )
            .first()
        )

        if existing_setting:
            existing_setting.setting_value = db_value  # type: ignore[assignment]
            db.add(existing_setting)
        else:
            new_setting = models.UserSetting(
                user_id=current_user.id, setting_key=db_key, setting_value=db_value
            )
            db.add(new_setting)

    db.commit()

    # Return updated settings
    return get_recording_settings(db=db, current_user=current_user)


@router.delete("/recording")
def reset_recording_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Reset user's recording settings to defaults (delete from database)
    """
    deleted_count = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(
                ["recording_max_duration", "recording_quality", "recording_auto_stop"]
            ),
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    return {
        "message": f"Recording settings reset to defaults. Removed {deleted_count} custom settings.",
        "default_settings": DEFAULT_RECORDING_SETTINGS,
    }
