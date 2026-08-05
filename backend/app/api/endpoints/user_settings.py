"""
API endpoints for user settings management

This module provides REST endpoints for managing user-specific settings
that need to persist across sessions and devices. Supports:
- Recording settings (duration, quality, auto-stop)
- Audio extraction settings
- Transcription settings (speaker detection, garbage cleanup)
- Organization context settings (LLM prompt injection)
- Download settings (video/audio quality for URL downloads)
"""

from datetime import UTC
from typing import Any
from typing import Literal
from typing import cast

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings as app_settings
from app.core.constants import AUDIO_QUALITY_OPTIONS
from app.core.constants import COMMON_LANGUAGES
from app.core.constants import DEFAULT_AUDIO_ONLY
from app.core.constants import DEFAULT_AUDIO_QUALITY
from app.core.constants import DEFAULT_DIARIZATION_SOURCE
from app.core.constants import DEFAULT_GARBAGE_CLEANUP_ENABLED
from app.core.constants import DEFAULT_GARBAGE_CLEANUP_THRESHOLD
from app.core.constants import DEFAULT_HALLUCINATION_SILENCE_THRESHOLD
from app.core.constants import DEFAULT_LLM_OUTPUT_LANGUAGE
from app.core.constants import DEFAULT_ORG_CONTEXT_INCLUDE_CUSTOM_PROMPTS
from app.core.constants import DEFAULT_ORG_CONTEXT_INCLUDE_DEFAULT_PROMPTS
from app.core.constants import DEFAULT_ORG_CONTEXT_TEXT
from app.core.constants import DEFAULT_RECORDING_AUTO_STOP
from app.core.constants import DEFAULT_RECORDING_MAX_DURATION
from app.core.constants import DEFAULT_RECORDING_QUALITY
from app.core.constants import DEFAULT_REPETITION_PENALTY
from app.core.constants import DEFAULT_SOURCE_LANGUAGE
from app.core.constants import DEFAULT_SPEAKER_PROMPT_BEHAVIOR
from app.core.constants import DEFAULT_TRANSCRIPTION_MAX_SPEAKERS
from app.core.constants import DEFAULT_TRANSCRIPTION_MIN_SPEAKERS
from app.core.constants import DEFAULT_TRANSLATE_TO_ENGLISH
from app.core.constants import DEFAULT_VAD_MIN_SILENCE_MS
from app.core.constants import DEFAULT_VAD_MIN_SPEECH_MS
from app.core.constants import DEFAULT_VAD_SPEECH_PAD_MS
from app.core.constants import DEFAULT_VAD_THRESHOLD
from app.core.constants import DEFAULT_VIDEO_QUALITY
from app.core.constants import LLM_OUTPUT_LANGUAGES
from app.core.constants import VALID_AUDIO_QUALITIES
from app.core.constants import VALID_DIARIZATION_SOURCES
from app.core.constants import VALID_RECORDING_DURATIONS
from app.core.constants import VALID_RECORDING_QUALITIES
from app.core.constants import VALID_SPEAKER_PROMPT_BEHAVIORS
from app.core.constants import VALID_VIDEO_QUALITIES
from app.core.constants import VIDEO_QUALITY_OPTIONS
from app.core.constants import WHISPER_LANGUAGES
from app.db.base import get_db
from app.schemas.download_settings import DownloadSettings
from app.schemas.download_settings import DownloadSettingsUpdate
from app.schemas.download_settings import DownloadSystemDefaults
from app.schemas.media_source import UserMediaSourceCreate
from app.schemas.media_source import UserMediaSourceResponse
from app.schemas.media_source import UserMediaSourcesList
from app.schemas.media_source import UserMediaSourceUpdate
from app.schemas.organization_context import OrganizationContextSettings
from app.schemas.organization_context import OrganizationContextUpdate
from app.schemas.organization_context import SharedOrganizationContext
from app.schemas.organization_context import SharedOrganizationContextList
from app.schemas.speaker_attribute_settings import SpeakerAttributeSettings
from app.schemas.speaker_attribute_settings import SpeakerAttributeSettingsUpdate
from app.schemas.speaker_attribute_settings import SpeakerAttributeSystemDefaults
from app.schemas.topic import AutoLabelSettingsSchema
from app.schemas.transcription_settings import TranscriptionSettings
from app.schemas.transcription_settings import TranscriptionSettingsUpdate
from app.schemas.transcription_settings import TranscriptionSystemDefaults

router = APIRouter()


# Default values for recording settings (when user hasn't set custom values)
DEFAULT_RECORDING_SETTINGS = {
    "max_recording_duration": DEFAULT_RECORDING_MAX_DURATION,
    "recording_quality": DEFAULT_RECORDING_QUALITY,
    "auto_stop_enabled": DEFAULT_RECORDING_AUTO_STOP,
}

# Default values for audio extraction settings
DEFAULT_AUDIO_EXTRACTION_SETTINGS = {
    "auto_extract_enabled": True,  # Automatically extract audio from large videos
    "extraction_threshold_mb": 100,  # Minimum file size (in MB) to trigger extraction prompt
    "remember_choice": False,  # Remember user's last choice (extract vs upload full)
    "show_modal": True,  # Show extraction modal (false to auto-extract without asking)
}

# Default values for download settings
DEFAULT_DOWNLOAD_SETTINGS = {
    "video_quality": DEFAULT_VIDEO_QUALITY,
    "audio_only": DEFAULT_AUDIO_ONLY,
    "audio_quality": DEFAULT_AUDIO_QUALITY,
}

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


@router.get("/audio-extraction", response_model=dict[str, Any])
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


@router.put("/audio-extraction", response_model=dict[str, Any])
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


# =============================================================================
# Transcription Settings Endpoints
# =============================================================================


def _validate_speaker_range(
    update_data: dict[str, Any],
    current_settings: TranscriptionSettings | None,
) -> None:
    """
    Validate that min_speakers <= max_speakers.

    Args:
        update_data: Dictionary of settings being updated
        current_settings: Current TranscriptionSettings (None if both min and max in update_data)

    Raises:
        HTTPException: If min_speakers > max_speakers
    """
    min_val = update_data.get("min_speakers")
    max_val = update_data.get("max_speakers")

    # Both provided in update
    if min_val is not None and max_val is not None:
        if min_val > max_val:
            raise HTTPException(
                status_code=400,
                detail="min_speakers cannot be greater than max_speakers",
            )
        return

    # Only min_speakers provided - check against current max
    if min_val is not None and current_settings is not None:
        if min_val > current_settings.max_speakers:
            raise HTTPException(
                status_code=400,
                detail=f"min_speakers ({min_val}) cannot be greater than current max_speakers ({current_settings.max_speakers})",
            )
        return

    # Only max_speakers provided - check against current min
    if (
        max_val is not None
        and current_settings is not None
        and max_val < current_settings.min_speakers
    ):
        raise HTTPException(
            status_code=400,
            detail=f"max_speakers ({max_val}) cannot be less than current min_speakers ({current_settings.min_speakers})",
        )


def _validate_speaker_prompt_behavior(behavior: str | None) -> None:
    """
    Validate speaker_prompt_behavior value.

    Args:
        behavior: The behavior value to validate (or None to skip)

    Raises:
        HTTPException: If behavior is not in VALID_SPEAKER_PROMPT_BEHAVIORS
    """
    if behavior is not None and behavior not in VALID_SPEAKER_PROMPT_BEHAVIORS:
        raise HTTPException(
            status_code=400,
            detail=f"speaker_prompt_behavior must be one of: {VALID_SPEAKER_PROMPT_BEHAVIORS}",
        )


def _validate_source_language(language: str | None) -> None:
    """
    Validate source_language value against supported Whisper languages.

    Args:
        language: The language code to validate (or None to skip)

    Raises:
        HTTPException: If language is not in WHISPER_LANGUAGES
    """
    if language is not None and language not in WHISPER_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail="source_language must be a valid ISO 639-1 code or 'auto'. See available_source_languages in system defaults.",
        )


def _validate_llm_output_language(language: str | None) -> None:
    """
    Validate llm_output_language value against supported LLM output languages.

    Args:
        language: The language code to validate (or None to skip)

    Raises:
        HTTPException: If language is not in LLM_OUTPUT_LANGUAGES
    """
    if language is not None and language not in LLM_OUTPUT_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"llm_output_language must be one of: {list(LLM_OUTPUT_LANGUAGES.keys())}",
        )


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


@router.get("/transcription", response_model=TranscriptionSettings)
def get_transcription_settings(
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
                ]
            ),
        )
        .all()
    )

    # Build settings map from database results
    settings_map: dict[str, str] = {
        str(setting.setting_key): str(setting.setting_value) for setting in transcription_settings
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
    garbage_threshold_value = settings_map.get(
        "transcription_garbage_cleanup_threshold",
        str(DEFAULT_TRANSCRIPTION_SETTINGS["garbage_cleanup_threshold"]),
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
        garbage_cleanup_enabled=settings_map.get(
            "transcription_garbage_cleanup_enabled", "true"
        ).lower()
        == "true",
        garbage_cleanup_threshold=int(garbage_threshold_value),
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
    )


@router.put("/transcription", response_model=TranscriptionSettings)
def update_transcription_settings(
    *,
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

    if not update_data:
        return get_transcription_settings(db=db, current_user=current_user)  # type: ignore[no-any-return]

    # Get current settings only if needed for validation
    needs_current = ("min_speakers" in update_data) != ("max_speakers" in update_data)
    current_settings = (
        get_transcription_settings(db=db, current_user=current_user) if needs_current else None
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
    }

    # Validate diarization_source value
    diarization_src = update_data.get("diarization_source")
    if diarization_src is not None:
        from app.core.constants import VALID_DIARIZATION_SOURCES

        if diarization_src not in VALID_DIARIZATION_SOURCES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid diarization_source: '{diarization_src}'. "
                    f"Must be one of {list(VALID_DIARIZATION_SOURCES)}"
                ),
            )

    # Update each setting in the database
    for frontend_key, value in update_data.items():
        db_key, transform = setting_mappings[frontend_key]
        db_value = transform(value) if transform else value
        _upsert_user_setting(db, current_user.id, db_key, db_value)

    db.commit()

    return get_transcription_settings(db=db, current_user=current_user)  # type: ignore[no-any-return]


@router.delete("/transcription")
def reset_transcription_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Reset user's transcription settings to defaults.

    Deletes all user-specific transcription settings from the database,
    causing the system to fall back to default values.

    Returns:
        Message confirming reset and the default settings that will now apply
    """
    deleted_count = (
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
                ]
            ),
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    # Return defaults including system-level speaker settings
    default_settings = {
        "min_speakers": app_settings.MIN_SPEAKERS,
        "max_speakers": app_settings.MAX_SPEAKERS,
        "speaker_prompt_behavior": DEFAULT_TRANSCRIPTION_SETTINGS["speaker_prompt_behavior"],
        "garbage_cleanup_enabled": DEFAULT_TRANSCRIPTION_SETTINGS["garbage_cleanup_enabled"],
        "garbage_cleanup_threshold": DEFAULT_TRANSCRIPTION_SETTINGS["garbage_cleanup_threshold"],
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
def get_transcription_system_defaults() -> TranscriptionSystemDefaults:
    """
    Get system-level transcription defaults.

    Returns the system-wide default values from environment configuration
    (MIN_SPEAKERS, MAX_SPEAKERS from .env) and application constants.
    This endpoint is useful for showing users what the default values are
    before they customize their settings.

    Note: This endpoint does not require authentication as it returns
    only system configuration, not user-specific data.

    Returns:
        TranscriptionSystemDefaults containing system min/max speakers,
        garbage cleanup defaults, valid behavior options, and language options
    """
    return TranscriptionSystemDefaults(
        min_speakers=app_settings.MIN_SPEAKERS,
        max_speakers=app_settings.MAX_SPEAKERS,
        garbage_cleanup_enabled=DEFAULT_GARBAGE_CLEANUP_ENABLED,
        garbage_cleanup_threshold=DEFAULT_GARBAGE_CLEANUP_THRESHOLD,
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
        valid_diarization_sources=list(VALID_DIARIZATION_SOURCES),
    )


# =============================================================================
# Organization Context Settings Endpoints
# =============================================================================

# Database keys for organization context settings
_ORG_CONTEXT_DB_KEYS = [
    "org_context_text",
    "org_context_include_default_prompts",
    "org_context_include_custom_prompts",
    "org_context_is_shared",
    "org_context_use_shared_from",
]

# Defaults for organization context settings
DEFAULT_ORG_CONTEXT_SETTINGS = {
    "context_text": DEFAULT_ORG_CONTEXT_TEXT,
    "include_in_default_prompts": DEFAULT_ORG_CONTEXT_INCLUDE_DEFAULT_PROMPTS,
    "include_in_custom_prompts": DEFAULT_ORG_CONTEXT_INCLUDE_CUSTOM_PROMPTS,
}


def _build_org_context_response(db: Session, user_id: int) -> OrganizationContextSettings:
    """Build organization context settings response for a user."""
    org_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(_ORG_CONTEXT_DB_KEYS),
        )
        .all()
    )

    settings_map: dict[str, str] = {
        str(setting.setting_key): str(setting.setting_value) for setting in org_settings
    }

    return OrganizationContextSettings(
        context_text=settings_map.get("org_context_text", DEFAULT_ORG_CONTEXT_TEXT),
        include_in_default_prompts=settings_map.get(
            "org_context_include_default_prompts", "true"
        ).lower()
        == "true",
        include_in_custom_prompts=settings_map.get(
            "org_context_include_custom_prompts", "false"
        ).lower()
        == "true",
        is_shared=settings_map.get("org_context_is_shared", "false").lower() == "true",
        using_shared_from=settings_map.get("org_context_use_shared_from"),
    )


@router.get("/organization-context", response_model=OrganizationContextSettings)
def get_organization_context(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> OrganizationContextSettings:
    """
    Get user's organization context settings.

    Returns settings with defaults for any values not customized by the user.

    Returns:
        OrganizationContextSettings containing context_text, include_in_default_prompts,
        and include_in_custom_prompts
    """
    return _build_org_context_response(db, current_user.id)


@router.put("/organization-context", response_model=OrganizationContextSettings)
def update_organization_context(
    *,
    db: Session = Depends(get_db),
    settings_data: OrganizationContextUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> OrganizationContextSettings:
    """
    Update user's organization context settings.

    Validates input and updates only the provided fields.

    Args:
        settings_data: OrganizationContextUpdate with optional fields

    Returns:
        Updated OrganizationContextSettings with all current values
    """
    update_data = settings_data.model_dump(exclude_none=True)

    if not update_data:
        return _build_org_context_response(db, current_user.id)

    # Map frontend keys to database keys
    setting_mappings = {
        "context_text": "org_context_text",
        "include_in_default_prompts": "org_context_include_default_prompts",
        "include_in_custom_prompts": "org_context_include_custom_prompts",
        "is_shared": "org_context_is_shared",
    }

    for frontend_key, value in update_data.items():
        db_key = setting_mappings.get(frontend_key)
        if db_key:
            _upsert_user_setting(db, current_user.id, db_key, value)

    # If unsharing, clean up other users who were using this shared context
    if update_data.get("is_shared") is False:
        db.query(models.UserSetting).filter(
            models.UserSetting.setting_key == "org_context_use_shared_from",
            models.UserSetting.setting_value == str(current_user.id),
        ).delete(synchronize_session=False)

    db.commit()

    return _build_org_context_response(db, current_user.id)


@router.delete("/organization-context")
def reset_organization_context(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Reset user's organization context settings to defaults.

    Deletes all user-specific organization context settings from the database.

    Returns:
        Message confirming reset and the default settings
    """
    deleted_count = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(_ORG_CONTEXT_DB_KEYS),
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    return {
        "message": f"Organization context reset to defaults. Removed {deleted_count} custom settings.",
        "default_settings": DEFAULT_ORG_CONTEXT_SETTINGS,
    }


@router.get("/organization-context/shared", response_model=SharedOrganizationContextList)
def get_shared_organization_contexts(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> SharedOrganizationContextList:
    """Get organization contexts shared by other users."""
    # Find all users who have shared their org context
    shared_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.setting_key == "org_context_is_shared",
            models.UserSetting.setting_value == "true",
            models.UserSetting.user_id != current_user.id,
        )
        .all()
    )

    if not shared_settings:
        return SharedOrganizationContextList(shared_contexts=[])

    # Get the context text for each sharing user
    sharer_ids = [int(s.user_id) for s in shared_settings]
    context_texts = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.setting_key == "org_context_text",
            models.UserSetting.user_id.in_(sharer_ids),
        )
        .all()
    )
    text_map = {int(s.user_id): str(s.setting_value) for s in context_texts}

    # Batch-fetch owners for attribution
    owners = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(sharer_ids)).all()}

    # Check which shared context the current user is using
    using_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key == "org_context_use_shared_from",
        )
        .first()
    )
    using_from_id = str(using_setting.setting_value) if using_setting else None

    shared_contexts = []
    for uid in sharer_ids:
        owner = owners.get(uid)
        ctx_text = text_map.get(uid, "")
        if not ctx_text or not owner:
            continue
        shared_contexts.append(
            SharedOrganizationContext(
                user_id=str(uid),
                owner_name=owner.full_name or owner.email,
                owner_role=owner.role or "user",
                context_text=ctx_text,
                is_active=using_from_id == str(uid),
            )
        )

    return SharedOrganizationContextList(shared_contexts=shared_contexts)


@router.post("/organization-context/use-shared")
def use_shared_organization_context(
    *,
    db: Session = Depends(get_db),
    body: dict = Body(...),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Start or stop using another user's shared organization context."""
    shared_user_id = body.get("user_id")

    if shared_user_id is None:
        # Stop using shared context — revert to own
        db.query(models.UserSetting).filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key == "org_context_use_shared_from",
        ).delete(synchronize_session=False)
        db.commit()
        return _build_org_context_response(db, current_user.id)

    # Verify the target user's context is actually shared
    is_shared = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == int(shared_user_id),
            models.UserSetting.setting_key == "org_context_is_shared",
            models.UserSetting.setting_value == "true",
        )
        .first()
    )
    if not is_shared:
        raise HTTPException(status_code=404, detail="Shared organization context not found")

    _upsert_user_setting(db, current_user.id, "org_context_use_shared_from", str(shared_user_id))
    db.commit()

    return _build_org_context_response(db, current_user.id)


# =============================================================================
# Speaker Attribute Settings Endpoints
# =============================================================================

# Database keys for speaker attribute settings
_SPEAKER_ATTR_DB_KEYS = [
    "speaker_attribute_detection_enabled",
    "speaker_attribute_gender_detection_enabled",
    "speaker_attribute_age_detection_enabled",
    "speaker_attribute_show_on_cards",
]


@router.get("/speaker-attributes", response_model=SpeakerAttributeSettings)
def get_speaker_attribute_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> SpeakerAttributeSettings:
    """Get user's speaker attribute detection settings."""
    user_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(_SPEAKER_ATTR_DB_KEYS),
        )
        .all()
    )

    settings_map: dict[str, str] = {str(s.setting_key): str(s.setting_value) for s in user_settings}

    return SpeakerAttributeSettings(
        detection_enabled=settings_map.get("speaker_attribute_detection_enabled", "true").lower()
        == "true",
        gender_detection_enabled=settings_map.get(
            "speaker_attribute_gender_detection_enabled", "true"
        ).lower()
        == "true",
        age_detection_enabled=settings_map.get(
            "speaker_attribute_age_detection_enabled", "true"
        ).lower()
        == "true",
        show_attributes_on_cards=settings_map.get("speaker_attribute_show_on_cards", "true").lower()
        == "true",
    )


@router.put("/speaker-attributes", response_model=SpeakerAttributeSettings)
def update_speaker_attribute_settings(
    *,
    db: Session = Depends(get_db),
    settings_data: SpeakerAttributeSettingsUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> SpeakerAttributeSettings:
    """Update user's speaker attribute detection settings (partial update)."""
    update_data = settings_data.model_dump(exclude_none=True)

    if not update_data:
        return get_speaker_attribute_settings(db=db, current_user=current_user)  # type: ignore[no-any-return]

    setting_mappings = {
        "detection_enabled": "speaker_attribute_detection_enabled",
        "gender_detection_enabled": "speaker_attribute_gender_detection_enabled",
        "age_detection_enabled": "speaker_attribute_age_detection_enabled",
        "show_attributes_on_cards": "speaker_attribute_show_on_cards",
    }

    for frontend_key, value in update_data.items():
        _upsert_user_setting(db, current_user.id, setting_mappings[frontend_key], value)

    db.commit()

    return get_speaker_attribute_settings(db=db, current_user=current_user)  # type: ignore[no-any-return]


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
        ],
    )

    def _b(key: str, default: bool) -> bool:
        v = vals.get(key)
        return v.lower() in ("true", "1", "yes", "on") if v is not None else default

    return SpeakerAttributeSystemDefaults(
        detection_enabled=_b("speaker_attribute.detection_enabled", env_enabled),
        gender_detection_enabled=_b("speaker_attribute.gender_detection_enabled", True),
        age_detection_enabled=_b("speaker_attribute.age_detection_enabled", True),
        show_attributes_on_cards=_b("speaker_attribute.show_on_cards", True),
    )


# =============================================================================
# Download Settings Endpoints
# =============================================================================


def _validate_video_quality(quality: str | None) -> None:
    """Validate video_quality value."""
    if quality is not None and quality not in VALID_VIDEO_QUALITIES:
        raise HTTPException(
            status_code=400,
            detail=f"video_quality must be one of: {VALID_VIDEO_QUALITIES}",
        )


def _validate_audio_quality(quality: str | None) -> None:
    """Validate audio_quality value."""
    if quality is not None and quality not in VALID_AUDIO_QUALITIES:
        raise HTTPException(
            status_code=400,
            detail=f"audio_quality must be one of: {VALID_AUDIO_QUALITIES}",
        )


@router.get("/download", response_model=DownloadSettings)
def get_download_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> DownloadSettings:
    """Get user's download quality settings."""
    download_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(
                [
                    "download_video_quality",
                    "download_audio_only",
                    "download_audio_quality",
                ]
            ),
        )
        .all()
    )

    settings_map: dict[str, str] = {
        str(setting.setting_key): str(setting.setting_value) for setting in download_settings
    }

    return DownloadSettings(
        video_quality=settings_map.get("download_video_quality", DEFAULT_VIDEO_QUALITY),
        audio_only=settings_map.get("download_audio_only", "false").lower() == "true",
        audio_quality=settings_map.get("download_audio_quality", DEFAULT_AUDIO_QUALITY),
    )


@router.put("/download", response_model=DownloadSettings)
def update_download_settings(
    *,
    db: Session = Depends(get_db),
    settings_data: DownloadSettingsUpdate,
    current_user: models.User = Depends(get_current_active_user),
) -> DownloadSettings:
    """Update user's download quality settings."""
    update_data = settings_data.model_dump(exclude_none=True)

    if not update_data:
        return get_download_settings(db=db, current_user=current_user)  # type: ignore[no-any-return]

    _validate_video_quality(update_data.get("video_quality"))
    _validate_audio_quality(update_data.get("audio_quality"))

    setting_mappings = {
        "video_quality": "download_video_quality",
        "audio_only": "download_audio_only",
        "audio_quality": "download_audio_quality",
    }

    for frontend_key, value in update_data.items():
        db_key = setting_mappings.get(frontend_key)
        if db_key is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown download setting field: '{frontend_key}'",
            )
        _upsert_user_setting(db, current_user.id, db_key, value)

    db.commit()

    return get_download_settings(db=db, current_user=current_user)  # type: ignore[no-any-return]


@router.delete("/download")
def reset_download_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Reset user's download settings to defaults."""
    deleted_count = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key.in_(
                [
                    "download_video_quality",
                    "download_audio_only",
                    "download_audio_quality",
                ]
            ),
        )
        .delete(synchronize_session=False)
    )

    db.commit()

    return {
        "message": f"Download settings reset to defaults. Removed {deleted_count} custom settings.",
        "default_settings": DEFAULT_DOWNLOAD_SETTINGS,
    }


@router.get("/download/system-defaults", response_model=DownloadSystemDefaults)
def get_download_system_defaults() -> DownloadSystemDefaults:
    """Get system-level download defaults and available options."""
    return DownloadSystemDefaults(
        video_quality=DEFAULT_VIDEO_QUALITY,
        audio_only=DEFAULT_AUDIO_ONLY,
        audio_quality=DEFAULT_AUDIO_QUALITY,
        available_video_qualities=VIDEO_QUALITY_OPTIONS,
        available_audio_qualities=AUDIO_QUALITY_OPTIONS,
    )


@router.get("/auto-label")
def get_auto_label_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Get user's auto-label settings."""
    from app.services.auto_label_service import AutoLabelService

    service = AutoLabelService(db)
    return service.get_user_auto_label_settings(current_user.id)


@router.put("/auto-label")
def update_auto_label_settings(
    settings_data: AutoLabelSettingsSchema = Body(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Update user's auto-label settings."""
    from app.services.auto_label_service import AutoLabelService

    service = AutoLabelService(db)
    service.save_user_auto_label_settings(current_user.id, settings_data.model_dump())
    return service.get_user_auto_label_settings(current_user.id)


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


# ─────────────────────────── Media Sources (per-user with sharing) ───────────


def _media_source_to_response(
    source: models.UserMediaSource,
    owner: models.User | None = None,
    is_own: bool = True,
) -> dict:
    """Build a UserMediaSourceResponse-compatible dict."""
    return {
        "uuid": str(source.uuid),
        "hostname": source.hostname,
        "provider_type": source.provider_type,
        "username": (source.username or "") if is_own else "",
        "has_credentials": bool(source.username and source.password),
        "verify_ssl": source.verify_ssl,
        "label": source.label or "",
        "is_active": source.is_active,
        "is_shared": source.is_shared,
        "shared_at": source.shared_at,
        "owner_name": (owner.full_name or owner.email) if owner else None,
        "owner_role": owner.role if owner else None,
        "is_own": is_own,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


@router.get("/media-sources", response_model=UserMediaSourcesList)
def get_media_sources(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Get user's own media sources and shared sources from other users."""
    # Own sources
    own_sources = (
        db.query(models.UserMediaSource)
        .filter(models.UserMediaSource.user_id == current_user.id)
        .order_by(models.UserMediaSource.created_at.desc())
        .all()
    )

    # Shared sources from other active users
    shared_sources = (
        db.query(models.UserMediaSource)
        .join(models.User, models.User.id == models.UserMediaSource.user_id)
        .filter(
            models.UserMediaSource.is_shared == True,  # noqa: E712
            models.UserMediaSource.is_active == True,  # noqa: E712
            models.UserMediaSource.user_id != current_user.id,
            models.User.is_active == True,  # noqa: E712
        )
        .order_by(models.UserMediaSource.shared_at.desc().nullslast())
        .all()
    )

    # Batch-fetch owners for shared source attribution
    owner_ids = {s.user_id for s in shared_sources}
    owners = {}
    if owner_ids:
        owners = {
            u.id: u for u in db.query(models.User).filter(models.User.id.in_(owner_ids)).all()
        }

    return {
        "sources": [_media_source_to_response(s, current_user, is_own=True) for s in own_sources],
        "shared_sources": [
            _media_source_to_response(s, owners.get(s.user_id), is_own=False)
            for s in shared_sources
        ],
    }


@router.post("/media-sources", response_model=UserMediaSourceResponse)
def create_media_source(
    data: UserMediaSourceCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Create a new user media source."""
    import uuid as uuid_pkg

    from sqlalchemy.exc import IntegrityError

    from app.utils.encryption import encrypt_api_key
    from app.utils.encryption import test_encryption

    # Enforce per-user source limit
    existing_count = (
        db.query(models.UserMediaSource)
        .filter(models.UserMediaSource.user_id == current_user.id)
        .count()
    )
    if existing_count >= 50:
        raise HTTPException(status_code=400, detail="Maximum number of media sources reached (50)")

    # Encrypt password if provided
    encrypted_password = None
    if data.password:
        if not test_encryption():
            raise HTTPException(status_code=500, detail="Encryption not available")
        encrypted_password = encrypt_api_key(data.password)
        if not encrypted_password:
            raise HTTPException(status_code=500, detail="Failed to encrypt password")

    new_source = models.UserMediaSource(
        uuid=uuid_pkg.uuid4(),
        user_id=current_user.id,
        hostname=data.hostname,
        provider_type=data.provider_type,
        username=data.username or None,
        password=encrypted_password,
        verify_ssl=data.verify_ssl,
        label=data.label or None,
    )
    db.add(new_source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"A source with hostname '{data.hostname}' already exists",
        ) from None
    db.refresh(new_source)

    return _media_source_to_response(new_source, current_user, is_own=True)


@router.put("/media-sources/{source_uuid}", response_model=UserMediaSourceResponse)
def update_media_source(
    source_uuid: str,
    data: UserMediaSourceUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Update an existing user media source. Owner only."""
    from datetime import datetime

    from sqlalchemy.exc import IntegrityError

    from app.utils.encryption import encrypt_api_key
    from app.utils.encryption import test_encryption

    source = (
        db.query(models.UserMediaSource)
        .filter(
            models.UserMediaSource.uuid == source_uuid,
            models.UserMediaSource.user_id == current_user.id,
        )
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Media source not found")

    if data.hostname and data.hostname != source.hostname:
        source.hostname = data.hostname
    if data.provider_type is not None:
        source.provider_type = data.provider_type
    if data.username is not None:
        source.username = data.username or None
    if data.password is not None:
        if data.password:
            if not test_encryption():
                raise HTTPException(status_code=500, detail="Encryption not available")
            encrypted = encrypt_api_key(data.password)
            if not encrypted:
                raise HTTPException(status_code=500, detail="Failed to encrypt password")
            source.password = encrypted
        else:
            source.password = None
    if data.verify_ssl is not None:
        source.verify_ssl = data.verify_ssl
    if data.label is not None:
        source.label = data.label or None

    # Handle sharing toggle
    if data.is_shared is not None:
        if data.is_shared and not source.is_shared:
            source.is_shared = True
            source.shared_at = datetime.now(UTC)
        elif not data.is_shared and source.is_shared:
            source.is_shared = False
            source.shared_at = None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"A source with hostname '{data.hostname or source.hostname}' already exists",
        ) from None
    db.refresh(source)
    return _media_source_to_response(source, current_user, is_own=True)


@router.delete("/media-sources/{source_uuid}")
def delete_media_source(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Delete a user media source. Owner only."""
    source = (
        db.query(models.UserMediaSource)
        .filter(
            models.UserMediaSource.uuid == source_uuid,
            models.UserMediaSource.user_id == current_user.id,
        )
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Media source not found")

    db.delete(source)
    db.commit()
    return {"success": True}
