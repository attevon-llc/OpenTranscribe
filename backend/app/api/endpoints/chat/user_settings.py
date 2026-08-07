"""Per-user chat preferences (``/user-settings/chat``).

Follows the redaction-settings pattern: preferences live in ``UserSetting`` rows
with coded defaults, no ``.env`` vars, and unset fields fall back to the constant
rather than being written eagerly.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.chat.common import USER_SETTING_KEYS
from app.api.endpoints.chat.common import read_user_chat_settings
from app.db.base import get_db
from app.schemas.chat import ChatUserSettings
from app.schemas.chat import ChatUserSettingsUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/chat", response_model=ChatUserSettings)
def get_chat_user_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> ChatUserSettings:
    """Return the caller's chat preferences (coded defaults for unset fields)."""
    return ChatUserSettings(**read_user_chat_settings(db, current_user.id))


@router.put("/chat", response_model=ChatUserSettings)
def update_chat_user_settings(
    body: ChatUserSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> ChatUserSettings:
    """Update the caller's chat preferences (only provided fields)."""
    from app.api.endpoints.user_settings import _upsert_user_setting

    for field, value in body.model_dump(exclude_none=True).items():
        _upsert_user_setting(db, current_user.id, USER_SETTING_KEYS[field], value)
    db.commit()

    return ChatUserSettings(**read_user_chat_settings(db, current_user.id))


@router.delete("/chat")
def reset_chat_user_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Reset the caller's chat preferences to defaults."""
    db.query(models.UserSetting).filter(
        models.UserSetting.user_id == current_user.id,
        models.UserSetting.setting_key.in_(list(USER_SETTING_KEYS.values())),
    ).delete(synchronize_session=False)
    db.commit()
    return {"message": "Chat settings reset to defaults"}
