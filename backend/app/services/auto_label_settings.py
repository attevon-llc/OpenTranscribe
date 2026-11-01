"""Per-user auto-label settings persistence (the ``user_setting`` rows)."""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD
from app.models.prompt import UserSetting
from app.services.auto_llm_settings import TOPIC_USER_KEY
from app.services.auto_llm_settings import resolve_auto_llm_flags

logger = logging.getLogger(__name__)


def get_user_auto_label_settings(db: Session, user_id: int) -> dict:
    """Read auto-label settings from user_setting table."""
    defaults = {
        "enabled": True,
        "confidence_threshold": DEFAULT_AUTO_LABEL_CONFIDENCE_THRESHOLD,
        "tags_enabled": True,
        "collections_enabled": True,
        "bulk_grouping_enabled": True,
    }

    setting_keys: dict[str, tuple[str, Any]] = {
        "auto_label_enabled": ("enabled", lambda v: v.lower() == "true"),
        "auto_label_confidence_threshold": ("confidence_threshold", float),
        "auto_label_tags_enabled": ("tags_enabled", lambda v: v.lower() == "true"),
        "auto_label_collections_enabled": (
            "collections_enabled",
            lambda v: v.lower() == "true",
        ),
        "auto_label_bulk_grouping_enabled": (
            "bulk_grouping_enabled",
            lambda v: v.lower() == "true",
        ),
    }

    settings = (
        db.query(UserSetting)
        .filter(
            UserSetting.user_id == user_id,
            UserSetting.setting_key.in_(setting_keys.keys()),
        )
        .all()
    )

    result = dict(defaults)
    for setting in settings:
        key_info = setting_keys.get(setting.setting_key)
        if key_info:
            result_key, converter = key_info
            try:
                result[result_key] = converter(setting.setting_value)
            except (ValueError, AttributeError):
                logger.debug(f"Invalid value for setting {setting.setting_key}, using default")

    # Resolved (user > system default > on) rather than read raw, so the form shows
    # what the pipeline will actually do.
    result["auto_topic_extraction"] = resolve_auto_llm_flags(db, user_id).topic_extraction

    return result


def save_user_auto_label_settings(db: Session, user_id: int, settings_data: dict) -> None:
    """Save auto-label settings to user_setting table.

    Validates confidence_threshold is between 0.0 and 1.0 before saving.

    Raises:
        ValueError: If confidence_threshold is out of range.
    """
    # Validate confidence_threshold range
    if "confidence_threshold" in settings_data:
        try:
            threshold = float(settings_data["confidence_threshold"])
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence_threshold must be a number between 0.0 and 1.0") from exc
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError(f"confidence_threshold must be between 0.0 and 1.0, got {threshold}")

    key_map = {
        "enabled": "auto_label_enabled",
        "confidence_threshold": "auto_label_confidence_threshold",
        "tags_enabled": "auto_label_tags_enabled",
        "collections_enabled": "auto_label_collections_enabled",
        "bulk_grouping_enabled": "auto_label_bulk_grouping_enabled",
        "auto_topic_extraction": TOPIC_USER_KEY,
    }

    for field, db_key in key_map.items():
        if field not in settings_data:
            continue
        value = (
            str(settings_data[field]).lower()
            if isinstance(settings_data[field], bool)
            else str(settings_data[field])
        )

        existing = (
            db.query(UserSetting)
            .filter(UserSetting.user_id == user_id, UserSetting.setting_key == db_key)
            .first()
        )
        if existing:
            existing.setting_value = value
        else:
            new_setting = UserSetting(
                user_id=user_id,
                setting_key=db_key,
                setting_value=value,
            )
            db.add(new_setting)

    db.commit()
