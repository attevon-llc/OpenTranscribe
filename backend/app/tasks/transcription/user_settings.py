"""Per-user transcription preferences read from ``UserSetting`` rows.

Per-file overrides supplied at dispatch time win over these values; see
``app/services/CLAUDE.md``.
"""

from app.core.config import settings


def _get_user_language_settings(db, user_id: int) -> dict:
    """
    Retrieve user's language settings from the database.

    Args:
        db: Database session
        user_id: ID of the user

    Returns:
        Dict with source_language and translate_to_english keys
    """
    from app import models
    from app.core.constants import DEFAULT_SOURCE_LANGUAGE

    user_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(
                [
                    "transcription_source_language",
                    "transcription_translate_to_english",
                ]
            ),
        )
        .all()
    )

    settings_map = {s.setting_key: s.setting_value for s in user_settings}

    return {
        "source_language": settings_map.get(
            "transcription_source_language", DEFAULT_SOURCE_LANGUAGE
        ),
        "translate_to_english": settings_map.get(
            "transcription_translate_to_english", "false"
        ).lower()
        == "true",
    }


def _get_user_transcription_settings(db, user_id: int) -> dict:
    """Retrieve user's transcription tuning settings from the database."""
    from app import models
    from app.core.constants import DEFAULT_HALLUCINATION_SILENCE_THRESHOLD
    from app.core.constants import DEFAULT_REPETITION_PENALTY
    from app.core.constants import DEFAULT_VAD_MIN_SILENCE_MS
    from app.core.constants import DEFAULT_VAD_MIN_SPEECH_MS
    from app.core.constants import DEFAULT_VAD_SPEECH_PAD_MS
    from app.core.constants import DEFAULT_VAD_THRESHOLD

    setting_keys = [
        "transcription_vad_threshold",
        "transcription_vad_min_silence_ms",
        "transcription_vad_min_speech_ms",
        "transcription_vad_speech_pad_ms",
        "transcription_hallucination_silence_threshold",
        "transcription_repetition_penalty",
        "transcription_min_speakers",
        "transcription_max_speakers",
        "transcription_diarization_source",
    ]
    user_settings = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(setting_keys),
        )
        .all()
    )
    settings_map = {s.setting_key: s.setting_value for s in user_settings}

    hal_raw = settings_map.get("transcription_hallucination_silence_threshold", "")
    hal_value = float(hal_raw) if hal_raw else DEFAULT_HALLUCINATION_SILENCE_THRESHOLD

    return {
        "vad_threshold": float(
            settings_map.get("transcription_vad_threshold", str(DEFAULT_VAD_THRESHOLD))
        ),
        "vad_min_silence_ms": int(
            settings_map.get("transcription_vad_min_silence_ms", str(DEFAULT_VAD_MIN_SILENCE_MS))
        ),
        "vad_min_speech_ms": int(
            settings_map.get("transcription_vad_min_speech_ms", str(DEFAULT_VAD_MIN_SPEECH_MS))
        ),
        "vad_speech_pad_ms": int(
            settings_map.get("transcription_vad_speech_pad_ms", str(DEFAULT_VAD_SPEECH_PAD_MS))
        ),
        "hallucination_silence_threshold": hal_value,
        "repetition_penalty": float(
            settings_map.get("transcription_repetition_penalty", str(DEFAULT_REPETITION_PENALTY))
        ),
        "min_speakers": int(
            settings_map.get("transcription_min_speakers", str(settings.MIN_SPEAKERS))
        ),
        "max_speakers": int(
            settings_map.get("transcription_max_speakers", str(settings.MAX_SPEAKERS))
        ),
        "diarization_source": settings_map.get("transcription_diarization_source", "provider"),
        "disable_diarization": settings_map.get("transcription_diarization_source", "provider")
        == "off",
    }
