"""Speaker attribute settings endpoints under ``/user-settings/speaker-attributes``."""

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app import models
from app.api.deps_context import get_base_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.user_settings_common import _upsert_user_setting
from app.core.env import is_truthy
from app.db.base import get_db
from app.schemas.speaker_attribute_settings import SpeakerAttributeSettings
from app.schemas.speaker_attribute_settings import SpeakerAttributeSettingsUpdate
from app.schemas.speaker_attribute_settings import SpeakerAttributeSystemDefaults
from app.services.auto_llm_settings import SPEAKER_ID_SYSTEM_KEY
from app.services.auto_llm_settings import SPEAKER_ID_USER_KEY
from app.services.auto_llm_settings import resolve_auto_llm_flags
from app.services.request_context import RequestContext
from app.services.speaker_attribute_settings import resolve_speaker_attribute_flags

router = APIRouter()


# Database keys for speaker attribute settings
_SPEAKER_ATTR_DB_KEYS = [
    "speaker_attribute_detection_enabled",
    "speaker_attribute_gender_detection_enabled",
    "speaker_attribute_age_detection_enabled",
    "speaker_attribute_show_on_cards",
    SPEAKER_ID_USER_KEY,
]


@router.get("/speaker-attributes", response_model=SpeakerAttributeSettings)
def get_speaker_attribute_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_base_context),
) -> SpeakerAttributeSettings:
    """Get user's speaker attribute detection settings.

    ``gender_detection_enabled`` is what will actually happen for the caller's active
    organization: it is false when the instance or that organization forbids gender
    inference (issue #1280), whatever the user chose, and ``gender_locked_by`` names which
    layer. The user's own stored choice is untouched, so it applies again once the
    restriction is lifted.
    """
    user_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(_SPEAKER_ATTR_DB_KEYS),
        )
        .all()
    )

    settings_map: dict[str, str] = {str(s.setting_key): str(s.setting_value) for s in user_settings}
    # The same resolver the pipeline uses (user > system default > default), so the form
    # shows what will actually happen.
    flags = resolve_speaker_attribute_flags(db, current_user.id, organization_id=ctx.org_id)

    return SpeakerAttributeSettings(
        detection_enabled=flags.detection_enabled,
        gender_detection_enabled=flags.gender_detection_enabled,
        gender_locked_by=flags.gender_locked_by,
        age_detection_enabled=settings_map.get(
            "speaker_attribute_age_detection_enabled", "true"
        ).lower()
        == "true",
        show_attributes_on_cards=flags.show_on_cards,
        auto_llm_speaker_identification=resolve_auto_llm_flags(
            db, current_user.id
        ).speaker_identification,
    )


@router.put("/speaker-attributes", response_model=SpeakerAttributeSettings)
def update_speaker_attribute_settings(
    *,
    db: Session = Depends(get_db),
    settings_data: SpeakerAttributeSettingsUpdate,
    current_user: models.User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_base_context),
) -> SpeakerAttributeSettings:
    """Update user's speaker attribute detection settings (partial update)."""
    update_data = settings_data.model_dump(exclude_none=True)

    if not update_data:
        return get_speaker_attribute_settings(db=db, current_user=current_user, ctx=ctx)  # type: ignore[no-any-return]

    setting_mappings = {
        "detection_enabled": "speaker_attribute_detection_enabled",
        "gender_detection_enabled": "speaker_attribute_gender_detection_enabled",
        "age_detection_enabled": "speaker_attribute_age_detection_enabled",
        "show_attributes_on_cards": "speaker_attribute_show_on_cards",
        "auto_llm_speaker_identification": SPEAKER_ID_USER_KEY,
    }

    for frontend_key, value in update_data.items():
        _upsert_user_setting(db, current_user.id, setting_mappings[frontend_key], value)

    db.commit()

    return get_speaker_attribute_settings(db=db, current_user=current_user, ctx=ctx)  # type: ignore[no-any-return]


@router.delete("/speaker-attributes")
def reset_speaker_attribute_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Reset speaker attribute settings to defaults."""
    deleted_count = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(_SPEAKER_ATTR_DB_KEYS),
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    return {
        "message": (f"Speaker attribute settings reset. Removed {deleted_count} custom settings."),
        "default_settings": SpeakerAttributeSettings().model_dump(),
    }


@router.get(
    "/speaker-attributes/system-defaults",
    response_model=SpeakerAttributeSystemDefaults,
)
def get_speaker_attribute_system_defaults(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> SpeakerAttributeSystemDefaults:
    """Get system-level speaker attribute defaults."""
    import os

    from app.services.system_settings_service import get_settings_map

    env_enabled = os.environ.get("SPEAKER_ATTRIBUTE_DETECTION_ENABLED", "true").lower() == "true"

    # Single SELECT for all four keys instead of four round-trips.
    vals = get_settings_map(
        db,
        [
            "speaker_attribute.detection_enabled",
            "speaker_attribute.gender_detection_enabled",
            "speaker_attribute.age_detection_enabled",
            "speaker_attribute.show_on_cards",
            SPEAKER_ID_SYSTEM_KEY,
        ],
    )

    def _b(key: str, default: bool) -> bool:
        v = vals.get(key)
        return is_truthy(v) if v is not None else default

    return SpeakerAttributeSystemDefaults(
        detection_enabled=_b("speaker_attribute.detection_enabled", env_enabled),
        gender_detection_enabled=_b("speaker_attribute.gender_detection_enabled", True),
        age_detection_enabled=_b("speaker_attribute.age_detection_enabled", True),
        show_attributes_on_cards=_b("speaker_attribute.show_on_cards", True),
        auto_llm_speaker_identification=_b(SPEAKER_ID_SYSTEM_KEY, True),
    )
