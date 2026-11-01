"""Auto-label and AI summary preference endpoints."""

from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.user_settings_common import _upsert_user_setting
from app.db.base import get_db
from app.schemas.topic import AutoLabelSettingsSchema

router = APIRouter()


@router.get("/auto-label")
def get_auto_label_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Get user's auto-label settings."""
    from app.services.auto_label_settings import get_user_auto_label_settings

    return get_user_auto_label_settings(db, current_user.id)


@router.put("/auto-label")
def update_auto_label_settings(
    settings_data: AutoLabelSettingsSchema = Body(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Update user's auto-label settings."""
    from app.services.auto_label_settings import get_user_auto_label_settings
    from app.services.auto_label_settings import save_user_auto_label_settings

    # exclude_unset: a client that predates a field must not reset it to its default.
    save_user_auto_label_settings(db, current_user.id, settings_data.model_dump(exclude_unset=True))
    return get_user_auto_label_settings(db, current_user.id)


# =============================================================================
# AI Summary Settings Endpoints
# =============================================================================


@router.get("/ai-summary")
def get_ai_summary_setting(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Get user's auto-summary preference.

    Returns:
        Dict with ``ai_summary_enabled`` boolean.
    """
    from app.utils.summary_settings import is_summary_enabled_for_user

    enabled = is_summary_enabled_for_user(db, current_user.id)
    return {"ai_summary_enabled": enabled}


@router.put("/ai-summary")
def update_ai_summary_setting(
    *,
    enabled: bool = Body(..., embed=True),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Enable or disable automatic AI summary generation for this user.

    Args:
        enabled: Whether to auto-generate summaries after transcription.

    Returns:
        Updated setting and confirmation message.
    """
    _upsert_user_setting(db, current_user.id, "ai_summary_enabled", enabled)
    db.commit()
    state = "enabled" if enabled else "disabled"
    return {
        "ai_summary_enabled": enabled,
        "message": f"AI summary auto-generation {state}",
    }
