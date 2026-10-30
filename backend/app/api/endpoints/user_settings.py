"""API endpoints for user settings management.

Persistent per-user settings, one module per settings group. This module only
wires the group routers together (in the original registration order) and owns
the ``/all`` debugging route.
"""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints import user_settings_ai_features
from app.api.endpoints import user_settings_audio_extraction
from app.api.endpoints import user_settings_download
from app.api.endpoints import user_settings_media_sources
from app.api.endpoints import user_settings_organization_context
from app.api.endpoints import user_settings_recording
from app.api.endpoints import user_settings_speaker_attributes
from app.api.endpoints import user_settings_transcription
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db

router = APIRouter()

router.include_router(user_settings_recording.router)
router.include_router(user_settings_audio_extraction.router)


@router.get("/all", response_model=dict[str, Any])
def get_all_user_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get all user settings (for debugging/admin purposes)
    """
    user_settings = (
        db.query(models.UserSetting).filter(models.UserSetting.user_id == current_user.id).all()
    )

    return {setting.setting_key: setting.setting_value for setting in user_settings}


router.include_router(user_settings_transcription.router)
router.include_router(user_settings_organization_context.router)
router.include_router(user_settings_speaker_attributes.router)
router.include_router(user_settings_download.router)
router.include_router(user_settings_ai_features.router)
router.include_router(user_settings_media_sources.router)
