"""Validators for transcription settings updates."""

from typing import Any

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app import models
from app.core.constants import DEFAULT_DIARIZATION_SOURCE
from app.core.constants import VALID_SPEAKER_PROMPT_BEHAVIORS
from app.core.languages import LLM_OUTPUT_LANGUAGES
from app.core.languages import WHISPER_LANGUAGES
from app.schemas.transcription_settings import TranscriptionSettings


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


def _refuse_unusable_speaker_selection(
    request: Request, db: Session, user_id: int, update_data: dict[str, Any]
) -> None:
    """409 a NEW speaker-detection choice the pipeline plan cannot run (issue #1224).

    The plan is resolved on the would-be state (stored settings with this update applied),
    so every rule lives in ``services/pipeline_plan`` and none is restated here. The
    response carries the stable ``code`` and its fixed sentence, never exception text.

    Only a change is refused: the settings form re-sends every field on save, and a
    selection stored earlier (e.g. pyannote.ai before #1204 required a key) must not make
    the user's other settings unsaveable. A stored selection's problems are reported by
    ``GET /user-settings/pipeline`` and per file by the pipeline instead.
    """
    from app.services.pipeline_plan_inputs import DIARIZER_CONFIG_KEY
    from app.services.pipeline_plan_inputs import SOURCE_KEY
    from app.services.pipeline_plan_inputs import plan_for_user

    stored = {
        str(key): str(value)
        for key, value in db.query(models.UserSetting.setting_key, models.UserSetting.setting_value)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_([SOURCE_KEY, DIARIZER_CONFIG_KEY]),
        )
        .all()
        if value
    }
    stored_source = stored.get(SOURCE_KEY, DEFAULT_DIARIZATION_SOURCE)
    source = str(update_data.get("diarization_source", stored_source))
    config_uuid = (
        str(update_data["diarization_asr_config_uuid"])
        if update_data.get("diarization_asr_config_uuid") is not None
        else stored.get(DIARIZER_CONFIG_KEY)
    )
    changed = source != stored_source or (
        source == "asr_config" and config_uuid != stored.get(DIARIZER_CONFIG_KEY)
    )
    if not changed:
        return
    plan = plan_for_user(
        db,
        user_id,
        request=request,
        diarization_source=source,
        diarization_asr_config_uuid=config_uuid,
    )
    refused = [issue for issue in plan.blocking() if issue.step == "diarization"]
    if refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": refused[0].code, "message": refused[0].message},
        )
