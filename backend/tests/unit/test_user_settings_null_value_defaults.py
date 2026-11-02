"""A stored NULL ``setting_value`` means "not set": the task settings fall back to defaults."""

from __future__ import annotations

from app.core.constants import DEFAULT_REPETITION_PENALTY
from app.core.constants import DEFAULT_VAD_MIN_SILENCE_MS
from app.core.constants import DEFAULT_VAD_THRESHOLD
from app.core.languages import DEFAULT_SOURCE_LANGUAGE
from app.models import UserSetting
from app.tasks.transcription.user_settings import _get_user_language_settings
from app.tasks.transcription.user_settings import _get_user_transcription_settings


def _store_null(db_session, user, keys):
    for key in keys:
        db_session.add(UserSetting(user_id=user.id, setting_key=key, setting_value=None))
    db_session.commit()


def test_null_language_settings_resolve_to_defaults(db_session, normal_user):
    _store_null(
        db_session,
        normal_user,
        ["transcription_source_language", "transcription_translate_to_english"],
    )

    resolved = _get_user_language_settings(db_session, normal_user.id)

    assert resolved == {"source_language": DEFAULT_SOURCE_LANGUAGE, "translate_to_english": False}


def test_null_tuning_settings_resolve_to_defaults(db_session, normal_user):
    _store_null(
        db_session,
        normal_user,
        [
            "transcription_vad_threshold",
            "transcription_vad_min_silence_ms",
            "transcription_repetition_penalty",
            "transcription_min_speakers",
        ],
    )

    resolved = _get_user_transcription_settings(db_session, normal_user.id)

    assert resolved["vad_threshold"] == DEFAULT_VAD_THRESHOLD
    assert resolved["vad_min_silence_ms"] == DEFAULT_VAD_MIN_SILENCE_MS
    assert resolved["repetition_penalty"] == DEFAULT_REPETITION_PENALTY
