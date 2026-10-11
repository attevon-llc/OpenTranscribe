"""Deleting a saved ASR config that is someone's speaker detector resets that choice (#1226).

Plan E5: a ``transcription_diarization_source = asr_config`` naming a deleted configuration
would otherwise leave a blocking ``diarizer_config_missing`` in the holder's plan and fail
every file they upload. ``DELETE /asr-settings/config/{uuid}`` (and ``/all``) removes the
pointer for every user who held it and reverts a still-``asr_config`` source to the default,
answering ``diarizer_source_reverted`` for the caller. Other choices are untouched.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models import UserSetting

pytestmark = pytest.mark.usefixtures("pipeline_env")

SOURCE_KEY = "transcription_diarization_source"
POINTER_KEY = "transcription_diarization_asr_config_uuid"


def _choose(db_session, user, cfg, source: str = "asr_config") -> None:
    db_session.add_all(
        [
            UserSetting(user_id=user.id, setting_key=SOURCE_KEY, setting_value=source),
            UserSetting(user_id=user.id, setting_key=POINTER_KEY, setting_value=str(cfg.uuid)),
        ]
    )
    db_session.commit()


def _setting(db_session, user, key: str) -> str | None:
    db_session.expire_all()
    row = (
        db_session.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == key)
        .one_or_none()
    )
    return None if row is None else str(row.setting_value)


def _pipeline_codes(client, headers) -> set[str]:
    response = client.get("/api/user-settings/pipeline", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    return {issue["code"] for issue in response.json()["issues"]}


def test_deleting_my_speaker_detector_resets_my_choice(
    client, user_token_headers, db_session, normal_user, add_asr_config
):
    add_asr_config("openai", "whisper-1", active=True)
    diarizer = add_asr_config("deepgram", "nova-3")
    _choose(db_session, normal_user, diarizer)
    assert "diarizer_double_billing" in _pipeline_codes(client, user_token_headers)

    response = client.delete(
        f"/api/asr-settings/config/{diarizer.uuid}", headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() == {"deleted": True, "diarizer_source_reverted": True}
    assert _setting(db_session, normal_user, SOURCE_KEY) is None
    assert _setting(db_session, normal_user, POINTER_KEY) is None
    settings = client.get("/api/user-settings/transcription", headers=user_token_headers).json()
    assert settings["diarization_source"] == "provider"
    assert "diarizer_config_missing" not in _pipeline_codes(client, user_token_headers)


def test_a_shared_detector_is_reset_for_everyone_who_chose_it(
    client,
    user_token_headers,
    db_session,
    normal_user,
    other_user,
    add_asr_config,
):
    shared = add_asr_config("deepgram", "nova-3", shared=True)
    _choose(db_session, other_user, shared)

    response = client.delete(f"/api/asr-settings/config/{shared.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["diarizer_source_reverted"] is False  # the caller's was not set
    assert _setting(db_session, other_user, SOURCE_KEY) is None
    assert _setting(db_session, other_user, POINTER_KEY) is None


def test_a_choice_naming_another_config_is_untouched(
    client, user_token_headers, db_session, normal_user, add_asr_config
):
    """Control: only references to the deleted configuration are reset."""
    kept = add_asr_config("deepgram", "nova-3")
    doomed = add_asr_config("gladia", "standard")
    _choose(db_session, normal_user, kept)

    response = client.delete(f"/api/asr-settings/config/{doomed.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["diarizer_source_reverted"] is False
    assert _setting(db_session, normal_user, SOURCE_KEY) == "asr_config"
    assert _setting(db_session, normal_user, POINTER_KEY) == str(kept.uuid)


def test_a_stale_pointer_goes_but_a_different_source_stays(
    client, user_token_headers, db_session, normal_user, add_asr_config, store_pyannote_key
):
    """The user moved on to pyannote.ai; the leftover pointer is cleared, the choice kept."""
    store_pyannote_key()
    doomed = add_asr_config("deepgram", "nova-3")
    _choose(db_session, normal_user, doomed, source="pyannote")

    response = client.delete(f"/api/asr-settings/config/{doomed.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["diarizer_source_reverted"] is False
    assert _setting(db_session, normal_user, SOURCE_KEY) == "pyannote"
    assert _setting(db_session, normal_user, POINTER_KEY) is None


def test_deleting_all_configs_resets_a_choice_naming_any_of_them(
    client, user_token_headers, db_session, normal_user, add_asr_config
):
    add_asr_config("openai", "whisper-1", active=True)
    diarizer = add_asr_config("assemblyai", "universal")
    _choose(db_session, normal_user, diarizer)

    response = client.delete("/api/asr-settings/all", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() == {"deleted": True, "diarizer_source_reverted": True}
    assert _setting(db_session, normal_user, SOURCE_KEY) is None
    assert _setting(db_session, normal_user, POINTER_KEY) is None
