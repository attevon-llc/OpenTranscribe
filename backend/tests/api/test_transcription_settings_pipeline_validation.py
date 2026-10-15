"""``PUT /user-settings/transcription`` validates speaker detection with the plan (#1224).

The settings PUT resolves the pipeline plan on the would-be state and refuses (409) a NEW
selection the plan reports as an error, with a stable ``code`` and its fixed sentence. It
accepts every combination the plan calls valid, including local Whisper + pyannote.ai
(which the pipeline executes as a post stage) and another saved provider's speakers
(``asr_config``). Stored values keep their meaning, and only a change is refused.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models import UserSetting
from app.services.pipeline_plan import ISSUE_MESSAGES

_URL = "/api/user-settings/transcription"

pytestmark = pytest.mark.usefixtures("pipeline_env")


def _put(client, headers, **body):
    return client.put(_URL, json=body, headers=headers)


def _stored(db_session, user, key: str) -> str | None:
    db_session.expire_all()
    row = (
        db_session.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == f"transcription_{key}")
        .one_or_none()
    )
    return None if row is None else str(row.setting_value)


def _store(db_session, user, key: str, value: str) -> None:
    db_session.add(
        UserSetting(user_id=user.id, setting_key=f"transcription_{key}", setting_value=value)
    )
    db_session.commit()


def assert_refused(response, code: str) -> None:
    """The response is the plan's 409: the stable code and its fixed sentence, nothing else."""
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == {"code": code, "message": ISSUE_MESSAGES[code]}


# --- valid combinations are accepted ---------------------------------------------------


def test_local_whisper_with_pyannote_ai_is_accepted(
    client, user_token_headers, normal_user, db_session, store_pyannote_key
):
    store_pyannote_key()
    response = _put(client, user_token_headers, diarization_source="pyannote")
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _stored(db_session, normal_user, "diarization_source") == "pyannote"


def test_automatic_with_a_text_only_timed_model_is_accepted(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    add_asr_config("openai", "whisper-1", active=True)
    _store(db_session, normal_user, "diarization_source", "off")
    response = _put(client, user_token_headers, diarization_source="provider")
    assert response.status_code == status.HTTP_200_OK, response.text


def test_another_providers_speakers_are_accepted_and_stored(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    add_asr_config("openai", "whisper-1", active=True)
    diarizer = add_asr_config("assemblyai", "universal")
    response = _put(
        client,
        user_token_headers,
        diarization_source="asr_config",
        diarization_asr_config_uuid=str(diarizer.uuid),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["diarization_asr_config_uuid"] == str(diarizer.uuid)
    assert _stored(db_session, normal_user, "diarization_asr_config_uuid") == str(diarizer.uuid)


# --- errors are refused with a stable code ---------------------------------------------


def test_pyannote_without_a_key_is_refused(client, user_token_headers):
    assert_refused(
        _put(client, user_token_headers, diarization_source="pyannote"), "pyannote_key_missing"
    )


@pytest.mark.parametrize("source", ["provider", "local", "pyannote", "asr_config"])
def test_any_speaker_detection_with_an_untimed_model_is_refused(
    source,
    client,
    user_token_headers,
    normal_user,
    db_session,
    add_asr_config,
    store_pyannote_key,
):
    add_asr_config("openai", "gpt-4o-transcribe", active=True)
    diarizer = add_asr_config("assemblyai", "universal")
    store_pyannote_key()
    _store(db_session, normal_user, "diarization_source", "off")
    response = _put(
        client,
        user_token_headers,
        diarization_source=source,
        diarization_asr_config_uuid=str(diarizer.uuid),
    )
    assert_refused(response, "asr_no_timing_for_speakers")
    assert _stored(db_session, normal_user, "diarization_source") == "off"


def test_off_is_always_accepted_with_an_untimed_model(client, user_token_headers, add_asr_config):
    add_asr_config("openai", "gpt-4o-transcribe", active=True)
    response = _put(client, user_token_headers, diarization_source="off")
    assert response.status_code == status.HTTP_200_OK, response.text


def test_asr_config_errors(
    client, user_token_headers, other_user, add_asr_config, db_session, normal_user
):
    add_asr_config("deepgram", "nova-3", active=True)
    text_only = add_asr_config("openai", "whisper-1")
    someone_elses = add_asr_config("assemblyai", "universal", owner=other_user)

    assert_refused(
        _put(client, user_token_headers, diarization_source="asr_config"),
        "diarizer_config_missing",
    )
    assert_refused(
        _put(
            client,
            user_token_headers,
            diarization_source="asr_config",
            diarization_asr_config_uuid=str(uuid.uuid4()),
        ),
        "diarizer_config_missing",
    )
    assert_refused(
        _put(
            client,
            user_token_headers,
            diarization_source="asr_config",
            diarization_asr_config_uuid=str(someone_elses.uuid),
        ),
        "diarizer_config_missing",
    )
    assert_refused(
        _put(
            client,
            user_token_headers,
            diarization_source="asr_config",
            diarization_asr_config_uuid=str(text_only.uuid),
        ),
        "diarizer_config_no_speakers",
    )
    assert _stored(db_session, normal_user, "diarization_source") is None


def test_asr_config_needs_the_user_provider_capability(
    client, user_token_headers, add_asr_config, caps_off
):
    add_asr_config("openai", "whisper-1", active=True)
    diarizer = add_asr_config("assemblyai", "universal")
    caps_off("asr.user_providers")
    response = _put(
        client,
        user_token_headers,
        diarization_source="asr_config",
        diarization_asr_config_uuid=str(diarizer.uuid),
    )
    assert_refused(response, "diarizer_config_missing")


def test_unknown_source_is_still_a_422(client, user_token_headers):
    response = _put(client, user_token_headers, diarization_source="pyannote-typo")
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_the_refusal_carries_no_exception_text(client, user_token_headers):
    body = _put(client, user_token_headers, diarization_source="pyannote").json()
    assert set(body["detail"]) == {"code", "message"}
    assert body["detail"]["message"] in ISSUE_MESSAGES.values()


# --- lite: cloud-only transcription and speaker detection ------------------------------


def test_lite_refuses_local_speaker_detection(
    client, user_token_headers, add_asr_config, pipeline_env
):
    pipeline_env("lite")
    add_asr_config("deepgram", "nova-3", active=True)
    assert_refused(
        _put(client, user_token_headers, diarization_source="local"),
        "local_diarization_unavailable",
    )


def test_lite_refuses_automatic_with_a_text_only_model(
    client, user_token_headers, normal_user, db_session, add_asr_config, pipeline_env
):
    pipeline_env("lite")
    add_asr_config("openai", "whisper-1", active=True)
    _store(db_session, normal_user, "diarization_source", "off")
    assert_refused(
        _put(client, user_token_headers, diarization_source="provider"),
        "automatic_needs_speaker_provider",
    )


def test_lite_accepts_a_cloud_diarizer_for_a_text_only_model(
    client, user_token_headers, add_asr_config, store_pyannote_key, pipeline_env
):
    pipeline_env("lite")
    add_asr_config("openai", "whisper-1", active=True)
    store_pyannote_key()
    response = _put(client, user_token_headers, diarization_source="pyannote")
    assert response.status_code == status.HTTP_200_OK, response.text


def test_full_image_control_accepts_local_with_the_same_config(
    client, user_token_headers, add_asr_config
):
    """Control for the lite refusal: the same request on the full image is valid."""
    add_asr_config("deepgram", "nova-3", active=True)
    response = _put(client, user_token_headers, diarization_source="local")
    assert response.status_code == status.HTTP_200_OK, response.text


# --- back-compat: stored values keep their meaning, only a change is refused -----------


@pytest.mark.parametrize("stored", ["provider", "local", "pyannote", "off"])
def test_stored_values_read_back_unchanged(
    stored, client, user_token_headers, normal_user, db_session
):
    _store(db_session, normal_user, "diarization_source", stored)
    body = client.get(_URL, headers=user_token_headers).json()
    assert body["diarization_source"] == stored
    assert body["diarization_asr_config_uuid"] is None


def test_a_stale_stored_selection_does_not_block_other_fields(
    client, user_token_headers, normal_user, db_session, add_asr_config, pipeline_env
):
    """lite + a stored 'local' is now an error, but re-saving it with other fields is not."""
    pipeline_env("lite")
    add_asr_config("deepgram", "nova-3", active=True)
    _store(db_session, normal_user, "diarization_source", "local")
    response = _put(
        client, user_token_headers, diarization_source="local", min_speakers=2, max_speakers=4
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["min_speakers"] == 2


# --- the #1109 lock covers the diarizer config too -------------------------------------


def test_locked_source_ignores_the_diarizer_config(
    client, user_token_headers, normal_user, db_session, add_asr_config, caps_off
):
    diarizer = add_asr_config("assemblyai", "universal")
    _store(db_session, normal_user, "diarization_asr_config_uuid", str(uuid.uuid4()))
    caps_off("transcription.diarization_source")

    response = _put(
        client,
        user_token_headers,
        diarization_source="asr_config",
        diarization_asr_config_uuid=str(diarizer.uuid),
        source_language="de",
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["diarization_source"] == "provider"
    assert body["diarization_asr_config_uuid"] is None
    assert body["source_language"] == "de"
    assert _stored(db_session, normal_user, "diarization_source") is None
    assert _stored(db_session, normal_user, "diarization_asr_config_uuid") != str(diarizer.uuid)


def test_unlocked_control_stores_the_diarizer_config(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    """Control for the lock test: without the lock the same request is stored."""
    diarizer = add_asr_config("assemblyai", "universal")
    response = _put(
        client,
        user_token_headers,
        diarization_source="asr_config",
        diarization_asr_config_uuid=str(diarizer.uuid),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _stored(db_session, normal_user, "diarization_asr_config_uuid") == str(diarizer.uuid)


def test_reset_of_the_speakers_group_clears_the_diarizer_config(
    client, user_token_headers, normal_user, db_session
):
    _store(db_session, normal_user, "diarization_asr_config_uuid", str(uuid.uuid4()))
    response = client.delete(f"{_URL}?group=speakers", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert _stored(db_session, normal_user, "diarization_asr_config_uuid") is None
