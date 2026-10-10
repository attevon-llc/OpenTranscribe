"""Per-user transcription preferences read from ``UserSetting`` rows.

Per-file overrides supplied at dispatch time win over these values; see
``app/services/CLAUDE.md``.
"""

from dataclasses import dataclass

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
    # Values the deployment has locked (issue #1109) fall back to the defaults below,
    # including ones the user stored before the lock.
    from app.core.locked_settings import locked_transcription_db_keys

    locked_keys = locked_transcription_db_keys()
    settings_map = {
        s.setting_key: s.setting_value for s in user_settings if s.setting_key not in locked_keys
    }

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


def load_vocabulary_terms(db, user_id: int, file_id: int) -> list[str]:
    """Active custom vocabulary for a file, in the file's tenant.

    The owner's terms stamped with the file's organization (none = personal) plus
    the owner-less terms visible there — never the owner's terms from another
    tenant. Community edition: nothing is stamped, so this is the owner's terms
    plus the system terms, as before.
    """
    from app.models.custom_vocabulary import CustomVocabulary
    from app.models.media import MediaFile
    from app.utils.db_helpers import org_stamp_is

    file_org_id = db.query(MediaFile.organization_id).filter(MediaFile.id == file_id).scalar()
    shared_stamp = CustomVocabulary.organization_id.is_(None)
    if file_org_id is not None:
        shared_stamp = shared_stamp | (CustomVocabulary.organization_id == file_org_id)
    return [
        row.term
        for row in db.query(CustomVocabulary.term)
        .filter(
            (
                (CustomVocabulary.user_id == user_id)
                & org_stamp_is(CustomVocabulary.organization_id, file_org_id)
            )
            | (CustomVocabulary.user_id.is_(None) & shared_stamp),
            CustomVocabulary.is_active.is_(True),
        )
        .all()
    ]


@dataclass(frozen=True)
class SpeakerRange:
    """The speaker-count hints a diarizer is given."""

    min_speakers: int
    max_speakers: int
    num_speakers: int | None


def resolve_speaker_range(
    user_settings: dict,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
) -> SpeakerRange:
    """The one place speaker-count precedence is decided (issue #1198).

    Per-file value, then the user's saved setting, then the deployment env default. A
    ``None`` from any caller therefore means "use my saved value", never "use the env
    default": ``user_settings`` (``_get_user_transcription_settings``) has already folded
    the env default in beneath the saved value. ``num_speakers`` has no per-user setting,
    so it falls back to the env ``NUM_SPEAKERS`` alone.
    """
    return SpeakerRange(
        min_speakers=min_speakers if min_speakers is not None else user_settings["min_speakers"],
        max_speakers=max_speakers if max_speakers is not None else user_settings["max_speakers"],
        num_speakers=num_speakers if num_speakers is not None else settings.NUM_SPEAKERS,
    )


def resolve_speaker_range_for_user(
    user_id: int,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
) -> SpeakerRange:
    """``resolve_speaker_range`` for a caller that has not loaded the user's settings."""
    from app.db.session_utils import session_scope

    with session_scope() as db:
        user_settings = _get_user_transcription_settings(db, user_id)
    return resolve_speaker_range(user_settings, min_speakers, max_speakers, num_speakers)
