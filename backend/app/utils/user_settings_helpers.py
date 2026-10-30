"""Shared helpers for querying user settings from the database."""

import logging

from sqlalchemy.orm import Session

from app.core.languages import DEFAULT_LLM_OUTPUT_LANGUAGE
from app.models.prompt import UserSetting

logger = logging.getLogger(__name__)


def get_user_llm_output_language(db: Session, user_id: int) -> str:
    """Retrieve user's LLM output language setting from the database.

    Args:
        db: Database session.
        user_id: ID of the user.

    Returns:
        LLM output language code (default: "en").
    """
    setting = (
        db.query(UserSetting)
        .filter(
            UserSetting.user_id == user_id,
            UserSetting.setting_key == "transcription_llm_output_language",
        )
        .first()
    )

    if setting:
        return str(setting.setting_value)
    return DEFAULT_LLM_OUTPUT_LANGUAGE
