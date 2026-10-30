"""Transcription settings endpoints under ``/user-settings/transcription``."""

import logging
from typing import Any
from typing import Literal
from typing import cast
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.user_settings_common import _upsert_user_setting
from app.api.endpoints.user_settings_transcription_validators import (
    _refuse_unusable_speaker_selection,
)
from app.api.endpoints.user_settings_transcription_validators import _validate_llm_output_language
from app.api.endpoints.user_settings_transcription_validators import _validate_source_language
from app.api.endpoints.user_settings_transcription_validators import (
    _validate_speaker_prompt_behavior,
)
from app.api.endpoints.user_settings_transcription_validators import _validate_speaker_range
from app.core.config import settings as app_settings
from app.core.constants import DEFAULT_DIARIZATION_SOURCE
from app.core.constants import DEFAULT_GARBAGE_CLEANUP_ENABLED
from app.core.constants import DEFAULT_GARBAGE_CLEANUP_THRESHOLD
from app.core.constants import DEFAULT_HALLUCINATION_SILENCE_THRESHOLD
from app.core.constants import DEFAULT_REPETITION_PENALTY
from app.core.constants import DEFAULT_SPEAKER_PROMPT_BEHAVIOR
from app.core.constants import DEFAULT_TRANSCRIPTION_MAX_SPEAKERS
from app.core.constants import DEFAULT_TRANSCRIPTION_MIN_SPEAKERS
from app.core.constants import DEFAULT_VAD_MIN_SILENCE_MS
from app.core.constants import DEFAULT_VAD_MIN_SPEECH_MS
from app.core.constants import DEFAULT_VAD_SPEECH_PAD_MS
from app.core.constants import DEFAULT_VAD_THRESHOLD
from app.core.constants import TRANSCRIPTION_SETTING_GROUPS
from app.core.constants import VALID_DIARIZATION_SOURCES
from app.core.constants import VALID_SPEAKER_PROMPT_BEHAVIORS
from app.core.languages import COMMON_LANGUAGES
from app.core.languages import DEFAULT_LLM_OUTPUT_LANGUAGE
from app.core.languages import DEFAULT_SOURCE_LANGUAGE
from app.core.languages import DEFAULT_TRANSLATE_TO_ENGLISH
from app.core.languages import LLM_OUTPUT_LANGUAGES
from app.core.languages import WHISPER_LANGUAGES
from app.db.base import get_db
from app.schemas.transcription_settings import TranscriptionSettings
from app.schemas.transcription_settings import TranscriptionSettingsUpdate
from app.schemas.transcription_settings import TranscriptionSystemDefaults
from app.services import system_settings_service
from app.utils.lite_mode import is_lite_deployment

logger = logging.getLogger(__name__)

router = APIRouter()


# Default values for transcription settings
DEFAULT_TRANSCRIPTION_SETTINGS = {
    "min_speakers": DEFAULT_TRANSCRIPTION_MIN_SPEAKERS,
    "max_speakers": DEFAULT_TRANSCRIPTION_MAX_SPEAKERS,
    "speaker_prompt_behavior": DEFAULT_SPEAKER_PROMPT_BEHAVIOR,
    "garbage_cleanup_enabled": DEFAULT_GARBAGE_CLEANUP_ENABLED,
    "garbage_cleanup_threshold": DEFAULT_GARBAGE_CLEANUP_THRESHOLD,
    "source_language": DEFAULT_SOURCE_LANGUAGE,
    "translate_to_english": DEFAULT_TRANSLATE_TO_ENGLISH,
    "llm_output_language": DEFAULT_LLM_OUTPUT_LANGUAGE,
    "diarization_source": DEFAULT_DIARIZATION_SOURCE,
}


@router.get("/transcription", response_model=TranscriptionSettings)
def get_transcription_settings(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> TranscriptionSettings:
    """
    Get user's transcription settings.

    Returns settings with defaults for any values not customized by the user.
    Defaults fall back to system-level settings from environment variables
    for speaker counts, and application constants for other settings.

    Returns:
        TranscriptionSettings containing min_speakers, max_speakers,
        speaker_prompt_behavior, garbage_cleanup_enabled, garbage_cleanup_threshold,
        source_language, translate_to_english, and llm_output_language
    """
    # Get all transcription-related settings for the user
    transcription_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(
                [
                    "transcription_min_speakers",
                    "transcription_max_speakers",
                    "transcription_speaker_prompt_behavior",
                    "transcription_garbage_cleanup_enabled",
                    "transcription_garbage_cleanup_threshold",
                    "transcription_source_language",
                    "transcription_translate_to_english",
                    "transcription_llm_output_language",
                    "transcription_vad_threshold",
                    "transcription_vad_min_silence_ms",
                    "transcription_vad_min_speech_ms",
                    "transcription_vad_speech_pad_ms",
                    "transcription_hallucination_silence_threshold",
                    "transcription_repetition_penalty",
                    "transcription_diarization_source",
                    "transcription_diarization_asr_config_uuid",
                ]
            ),
        )
        .all()
    )

    # Build settings map from database results. A value the deployment has locked
    # (issue #1109) is dropped so the response reports the default that applies.
    from app.core.locked_settings import locked_transcription_db_keys

    locked_keys = locked_transcription_db_keys(request)
    settings_map: dict[str, str] = {
        str(setting.setting_key): str(setting.setting_value)
        for setting in transcription_settings
        if str(setting.setting_key) not in locked_keys
    }

    # Get system defaults from environment (via app_settings)
    system_min_speakers = app_settings.MIN_SPEAKERS
    system_max_speakers = app_settings.MAX_SPEAKERS

    # Get values with proper type casting
    min_speakers_value = settings_map.get("transcription_min_speakers", str(system_min_speakers))
    max_speakers_value = settings_map.get("transcription_max_speakers", str(system_max_speakers))
    speaker_behavior_value = settings_map.get(
        "transcription_speaker_prompt_behavior",
        str(DEFAULT_TRANSCRIPTION_SETTINGS["speaker_prompt_behavior"]),
    )
    # Unset means "inherit the deployment default" (admin garbage-cleanup setting), which
    # is also what the pipeline applies, so the form shows the value that will actually run.
    garbage_effective = system_settings_service.get_effective_garbage_cleanup_config(
        db, current_user.id
    )
    source_language_value = settings_map.get(
        "transcription_source_language",
        str(DEFAULT_TRANSCRIPTION_SETTINGS["source_language"]),
    )
    llm_output_language_value = settings_map.get(
        "transcription_llm_output_language",
        str(DEFAULT_TRANSCRIPTION_SETTINGS["llm_output_language"]),
    )

    # Build response with user values or defaults
    return TranscriptionSettings(
        min_speakers=int(min_speakers_value),
        max_speakers=int(max_speakers_value),
        speaker_prompt_behavior=cast(
            Literal["always_prompt", "use_defaults", "use_custom"],
            speaker_behavior_value,
        ),
        garbage_cleanup_enabled=garbage_effective["garbage_cleanup_enabled"],
        garbage_cleanup_threshold=garbage_effective["max_word_length"],
        source_language=source_language_value,
        translate_to_english=settings_map.get("transcription_translate_to_english", "false").lower()
        == "true",
        llm_output_language=llm_output_language_value,
        vad_threshold=float(
            settings_map.get("transcription_vad_threshold", str(DEFAULT_VAD_THRESHOLD))
        ),
        vad_min_silence_ms=int(
            settings_map.get("transcription_vad_min_silence_ms", str(DEFAULT_VAD_MIN_SILENCE_MS))
        ),
        vad_min_speech_ms=int(
            settings_map.get("transcription_vad_min_speech_ms", str(DEFAULT_VAD_MIN_SPEECH_MS))
        ),
        vad_speech_pad_ms=int(
            settings_map.get("transcription_vad_speech_pad_ms", str(DEFAULT_VAD_SPEECH_PAD_MS))
        ),
        hallucination_silence_threshold=(
            float(settings_map["transcription_hallucination_silence_threshold"])
            if settings_map.get("transcription_hallucination_silence_threshold")
            else None
        ),
        repetition_penalty=float(
            settings_map.get("transcription_repetition_penalty", str(DEFAULT_REPETITION_PENALTY))
        ),
        diarization_source=settings_map.get(
            "transcription_diarization_source", DEFAULT_DIARIZATION_SOURCE
        ),
        diarization_asr_config_uuid=_stored_uuid(
            settings_map.get("transcription_diarization_asr_config_uuid")
        ),
    )


def _stored_uuid(value: str | None) -> UUID | None:
    """A stored UUID setting, or ``None`` when unset or unreadable (never a 500)."""
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


@router.put("/transcription", response_model=TranscriptionSettings)
def update_transcription_settings(
    *,
    request: Request,
    db: Session = Depends(get_db),
    settings_data: TranscriptionSettingsUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> TranscriptionSettings:
    """
    Update user's transcription settings.

    Validates input data and updates only the provided settings.
    Creates new settings if they don't exist, updates existing ones otherwise.

    Args:
        settings_data: TranscriptionSettingsUpdate with optional fields:
            min_speakers, max_speakers, speaker_prompt_behavior,
            garbage_cleanup_enabled, garbage_cleanup_threshold,
            source_language, translate_to_english, llm_output_language

    Returns:
        Updated TranscriptionSettings with all current values

    Raises:
        HTTPException: If validation fails for any setting
    """
    # Convert Pydantic model to dict, excluding None values
    # But preserve hallucination_silence_threshold=None (means "disable")
    update_data = settings_data.model_dump(exclude_none=True)
    if (
        settings_data.hallucination_silence_threshold is None
        and "hallucination_silence_threshold" not in update_data
    ):
        # Check if the field was explicitly set in the request body
        raw = settings_data.model_dump(exclude_unset=True)
        if "hallucination_silence_threshold" in raw:
            update_data["hallucination_silence_threshold"] = None

    # Fields the deployment owns (issue #1109) are ignored rather than rejected: the
    # settings form sends every field on save, and the rest of the request is valid.
    from app.core.locked_settings import locked_transcription_fields

    locked = locked_transcription_fields(request) & update_data.keys()
    if locked:
        logger.info(
            "Ignoring deployment-locked transcription settings %s for user %s",
            sorted(locked),
            current_user.id,
        )
        for field in locked:
            del update_data[field]

    if not update_data:
        return get_transcription_settings(request=request, db=db, current_user=current_user)  # type: ignore[no-any-return]

    # Get current settings only if needed for validation
    needs_current = ("min_speakers" in update_data) != ("max_speakers" in update_data)
    current_settings = (
        get_transcription_settings(request=request, db=db, current_user=current_user)
        if needs_current
        else None
    )

    # Validate speaker range and prompt behavior
    _validate_speaker_range(update_data, current_settings)
    _validate_speaker_prompt_behavior(update_data.get("speaker_prompt_behavior"))

    # Validate language settings
    _validate_source_language(update_data.get("source_language"))
    _validate_llm_output_language(update_data.get("llm_output_language"))

    # Map frontend keys to database keys with optional value transform
    setting_mappings: dict[str, tuple[str, Any]] = {
        "min_speakers": ("transcription_min_speakers", None),
        "max_speakers": ("transcription_max_speakers", None),
        "speaker_prompt_behavior": ("transcription_speaker_prompt_behavior", None),
        "garbage_cleanup_enabled": ("transcription_garbage_cleanup_enabled", None),
        "garbage_cleanup_threshold": ("transcription_garbage_cleanup_threshold", None),
        "source_language": ("transcription_source_language", None),
        "translate_to_english": ("transcription_translate_to_english", None),
        "llm_output_language": ("transcription_llm_output_language", None),
        "vad_threshold": ("transcription_vad_threshold", None),
        "vad_min_silence_ms": ("transcription_vad_min_silence_ms", None),
        "vad_min_speech_ms": ("transcription_vad_min_speech_ms", None),
        "vad_speech_pad_ms": ("transcription_vad_speech_pad_ms", None),
        "hallucination_silence_threshold": (
            "transcription_hallucination_silence_threshold",
            lambda v: str(v) if v is not None else "",
        ),
        "repetition_penalty": ("transcription_repetition_penalty", None),
        "diarization_source": ("transcription_diarization_source", None),
        "diarization_asr_config_uuid": ("transcription_diarization_asr_config_uuid", str),
    }

    diarization_src = update_data.get("diarization_source")
    if diarization_src is not None and diarization_src not in VALID_DIARIZATION_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid diarization_source: '{diarization_src}'. "
                f"Must be one of {list(VALID_DIARIZATION_SOURCES)}"
            ),
        )
    if update_data.keys() & {"diarization_source", "diarization_asr_config_uuid"}:
        _refuse_unusable_speaker_selection(request, db, current_user.id, update_data)

    # Update each setting in the database
    for frontend_key, value in update_data.items():
        db_key, transform = setting_mappings[frontend_key]
        db_value = transform(value) if transform else value
        _upsert_user_setting(db, current_user.id, db_key, db_value)

    db.commit()

    return get_transcription_settings(request=request, db=db, current_user=current_user)  # type: ignore[no-any-return]


@router.delete("/transcription")
def reset_transcription_settings(
    group: Literal["language", "accuracy", "speakers"] | None = Query(
        None,
        description="Reset only this field group. Omit to reset every transcription setting.",
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Reset user's transcription settings to defaults.

    Deletes user-specific transcription settings from the database, causing the
    system to fall back to default values. With ``group`` only that group's
    fields are reset; without it, all of them are.

    Returns:
        Message confirming reset and the default settings that will now apply
    """
    fields = (
        TRANSCRIPTION_SETTING_GROUPS[group]
        if group is not None
        else tuple(f for grp in TRANSCRIPTION_SETTING_GROUPS.values() for f in grp)
    )
    deleted_count = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_([f"transcription_{f}" for f in fields]),
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    # Return defaults including system-level speaker settings
    garbage_default = system_settings_service.get_garbage_cleanup_config(db)
    default_settings = {
        "min_speakers": app_settings.MIN_SPEAKERS,
        "max_speakers": app_settings.MAX_SPEAKERS,
        "speaker_prompt_behavior": DEFAULT_TRANSCRIPTION_SETTINGS["speaker_prompt_behavior"],
        "garbage_cleanup_enabled": garbage_default["garbage_cleanup_enabled"],
        "garbage_cleanup_threshold": garbage_default["max_word_length"],
        "source_language": DEFAULT_TRANSCRIPTION_SETTINGS["source_language"],
        "translate_to_english": DEFAULT_TRANSCRIPTION_SETTINGS["translate_to_english"],
        "llm_output_language": DEFAULT_TRANSCRIPTION_SETTINGS["llm_output_language"],
        "vad_threshold": DEFAULT_VAD_THRESHOLD,
        "vad_min_silence_ms": DEFAULT_VAD_MIN_SILENCE_MS,
        "vad_min_speech_ms": DEFAULT_VAD_MIN_SPEECH_MS,
        "vad_speech_pad_ms": DEFAULT_VAD_SPEECH_PAD_MS,
        "hallucination_silence_threshold": DEFAULT_HALLUCINATION_SILENCE_THRESHOLD,
        "repetition_penalty": DEFAULT_REPETITION_PENALTY,
    }

    return {
        "message": f"Transcription settings reset to defaults. Removed {deleted_count} custom settings.",
        "default_settings": default_settings,
    }


@router.get("/transcription/system-defaults", response_model=TranscriptionSystemDefaults)
def get_transcription_system_defaults(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> TranscriptionSystemDefaults:
    """
    Get system-level transcription defaults.

    Returns the system-wide default values from environment configuration
    (MIN_SPEAKERS, MAX_SPEAKERS from .env) and application constants.
    This endpoint is useful for showing users what the default values are
    before they customize their settings.

    Note: This endpoint DOES require authentication (see the dependency above).
    An earlier docstring claimed otherwise; it returns
    only system configuration, not user-specific data.

    Returns:
        TranscriptionSystemDefaults containing system min/max speakers,
        garbage cleanup defaults, valid behavior options, and language options
    """
    garbage_default = system_settings_service.get_garbage_cleanup_config(db)
    return TranscriptionSystemDefaults(
        min_speakers=app_settings.MIN_SPEAKERS,
        max_speakers=app_settings.MAX_SPEAKERS,
        garbage_cleanup_enabled=garbage_default["garbage_cleanup_enabled"],
        garbage_cleanup_threshold=garbage_default["max_word_length"],
        valid_speaker_prompt_behaviors=list(VALID_SPEAKER_PROMPT_BEHAVIORS),
        available_source_languages=WHISPER_LANGUAGES,
        available_llm_output_languages=LLM_OUTPUT_LANGUAGES,
        common_languages=COMMON_LANGUAGES,
        vad_threshold=DEFAULT_VAD_THRESHOLD,
        vad_min_silence_ms=DEFAULT_VAD_MIN_SILENCE_MS,
        vad_min_speech_ms=DEFAULT_VAD_MIN_SPEECH_MS,
        vad_speech_pad_ms=DEFAULT_VAD_SPEECH_PAD_MS,
        hallucination_silence_threshold=DEFAULT_HALLUCINATION_SILENCE_THRESHOLD,
        repetition_penalty=DEFAULT_REPETITION_PENALTY,
        diarization_source_default=DEFAULT_DIARIZATION_SOURCE,
        # Lite has no local speaker detection (#1228): never offer "this server" there.
        valid_diarization_sources=[
            source
            for source in VALID_DIARIZATION_SOURCES
            if not (source == "local" and is_lite_deployment())
        ],
    )
