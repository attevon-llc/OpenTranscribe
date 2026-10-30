"""Persistence helper shared by the per-group user-settings endpoint modules."""

from typing import Any

from sqlalchemy.orm import Session

from app import models


def _upsert_user_setting(
    db: Session,
    user_id: int,
    setting_key: str,
    setting_value: Any,
) -> None:
    """
    Insert or update a user setting in the database.

    Args:
        db: Database session
        user_id: ID of the user
        setting_key: The database key for the setting
        setting_value: The value to store (will be converted to string)
    """
    # Convert value to string for database storage
    if isinstance(setting_value, bool):
        db_value = "true" if setting_value else "false"
    else:
        db_value = str(setting_value)

    existing_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key == setting_key,
        )
        .first()
    )

    if existing_setting:
        existing_setting.setting_value = db_value  # type: ignore[assignment]
        db.add(existing_setting)
    else:
        new_setting = models.UserSetting(
            user_id=user_id, setting_key=setting_key, setting_value=db_value
        )
        db.add(new_setting)
