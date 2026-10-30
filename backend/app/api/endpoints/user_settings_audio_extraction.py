"""Audio extraction settings endpoints under ``/user-settings/audio-extraction``."""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.core.capabilities import require_capability
from app.db.base import get_db

router = APIRouter()


# Default values for audio extraction settings
DEFAULT_AUDIO_EXTRACTION_SETTINGS = {
    "auto_extract_enabled": True,  # Automatically extract audio from large videos
    "extraction_threshold_mb": 100,  # Minimum file size (in MB) to trigger extraction prompt
    "remember_choice": False,  # Remember user's last choice (extract vs upload full)
    "show_modal": True,  # Show extraction modal (false to auto-extract without asking)
}


@router.get(
    "/audio-extraction",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability("audio_extraction", platform_admin_bypass=False))],
)
def get_audio_extraction_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get user's audio extraction settings.

    Returns settings with defaults for any values not customized by the user.

    Returns:
        Dict containing auto_extract_enabled, extraction_threshold_mb, remember_choice, show_modal
    """
    # Get all audio extraction-related settings for the user
    extraction_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(
                [
                    "audio_extraction_auto_extract",
                    "audio_extraction_threshold_mb",
                    "audio_extraction_remember_choice",
                    "audio_extraction_show_modal",
                ]
            ),
        )
        .all()
    )

    # Build settings map from database results
    settings_map: dict[str, str] = {
        str(setting.setting_key): str(setting.setting_value) for setting in extraction_settings
    }

    # Map database keys to frontend keys and provide defaults for missing values
    settings = {
        "auto_extract_enabled": settings_map.get("audio_extraction_auto_extract", "true").lower()
        == "true",
        "extraction_threshold_mb": int(
            settings_map.get(
                "audio_extraction_threshold_mb",
                DEFAULT_AUDIO_EXTRACTION_SETTINGS["extraction_threshold_mb"],
            )
        ),
        "remember_choice": settings_map.get("audio_extraction_remember_choice", "false").lower()
        == "true",
        "show_modal": settings_map.get("audio_extraction_show_modal", "true").lower() == "true",
    }

    return settings


@router.put(
    "/audio-extraction",
    response_model=dict[str, Any],
    dependencies=[Depends(require_capability("audio_extraction", platform_admin_bypass=False))],
)
def update_audio_extraction_settings(
    *,
    db: Session = Depends(get_db),
    settings_data: dict[str, Any],
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Update user's audio extraction settings.

    Validates input data and updates only the provided settings.
    Creates new settings if they don't exist, updates existing ones otherwise.

    Args:
        settings_data: Dict with optional keys: auto_extract_enabled,
                      extraction_threshold_mb, remember_choice, show_modal

    Returns:
        Updated settings dict with all current values

    Raises:
        HTTPException: If validation fails for any setting
    """
    # Validate incoming data
    valid_keys = {
        "auto_extract_enabled",
        "extraction_threshold_mb",
        "remember_choice",
        "show_modal",
    }
    if not all(key in valid_keys for key in settings_data):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid setting keys. Valid keys are: {valid_keys}",
        )

    # Validate specific values
    if "extraction_threshold_mb" in settings_data:
        threshold = settings_data["extraction_threshold_mb"]
        if not isinstance(threshold, int) or threshold < 1 or threshold > 10000:
            raise HTTPException(
                status_code=400,
                detail="extraction_threshold_mb must be an integer between 1 and 10000",
            )

    for key in ["auto_extract_enabled", "remember_choice", "show_modal"]:
        if key in settings_data and not isinstance(settings_data[key], bool):
            raise HTTPException(status_code=400, detail=f"{key} must be a boolean")

    # Map frontend keys to database keys
    setting_mappings = {
        "auto_extract_enabled": "audio_extraction_auto_extract",
        "extraction_threshold_mb": "audio_extraction_threshold_mb",
        "remember_choice": "audio_extraction_remember_choice",
        "show_modal": "audio_extraction_show_modal",
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
    return get_audio_extraction_settings(db=db, current_user=current_user)
