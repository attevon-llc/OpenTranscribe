"""The ASR-side switches re-plan the pipeline (issue #1225).

``PUT /user-settings/transcription`` refuses a new speaker-detection choice the plan cannot
run. These four switches can break the same combination from the transcription side, so
they ask the plan too:

* ``POST /asr-settings/set-active`` and ``POST /asr-settings/clear-active`` refuse (409, the
  PUT's ``{code, message}``) a switch that INTRODUCES a diarization-step error, and report
  the resulting pipeline's issues otherwise;
* ``POST /asr-settings`` auto-activates a first config only when that introduces no error;
* ``DELETE /asr-settings/config/{uuid}`` promotes the first remaining config that keeps the
  choice runnable, else clears.

The #1109 lock pins the source, so a locked deployment is never refused.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models import UserSetting
from app.services.pipeline_plan import ISSUE_MESSAGES

_BASE = "/api/asr-settings"

pytestmark = pytest.mark.usefixtures("pipeline_env")


def _store_source(db_session, user, value: str) -> None:
    db_session.add(
        UserSetting(
            user_id=user.id, setting_key="transcription_diarization_source", setting_value=value
        )
    )
    db_session.commit()


def _active_id(db_session, user) -> str | None:
    db_session.expire_all()
    row = (
        db_session.query(UserSetting)
        .filter(UserSetting.user_id == user.id, UserSetting.setting_key == "active_asr_config_id")
        .one_or_none()
    )
    return None if row is None else str(row.setting_value)


def _codes(body: dict) -> set[str]:
    return {issue["code"] for issue in body["pipeline_issues"]}


def assert_refused(response, code: str) -> None:
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"] == {"code": code, "message": ISSUE_MESSAGES[code]}


# --- set-active ---------------------------------------------------------------------------


def test_activating_an_untimed_model_under_automatic_is_refused(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    current = add_asr_config("deepgram", "nova-3", active=True)
    untimed = add_asr_config("openai", "gpt-4o-transcribe")

    response = client.post(
        f"{_BASE}/set-active", json={"config_uuid": str(untimed.uuid)}, headers=user_token_headers
    )

    assert_refused(response, "asr_no_timing_for_speakers")
    assert _active_id(db_session, normal_user) == str(current.id)


def test_activating_a_segment_timed_model_is_accepted_with_its_warning(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    """Control for the refusal: the same switch to a model with segment times runs."""
    add_asr_config("deepgram", "nova-3", active=True)
    segment_timed = add_asr_config("openai", "whisper-1")

    response = client.post(
        f"{_BASE}/set-active",
        json={"config_uuid": str(segment_timed.uuid)},
        headers=user_token_headers,
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["config_uuid"] == str(segment_timed.uuid)
    assert "segment_timing_only" in _codes(body)
    assert _active_id(db_session, normal_user) == str(segment_timed.id)


def test_an_untimed_model_is_accepted_when_speaker_detection_is_off(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    _store_source(db_session, normal_user, "off")
    untimed = add_asr_config("openai", "gpt-4o-transcribe")

    response = client.post(
        f"{_BASE}/set-active", json={"config_uuid": str(untimed.uuid)}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _codes(response.json()) == set()
    assert _active_id(db_session, normal_user) == str(untimed.id)


def test_an_error_the_switch_did_not_introduce_is_reported_not_refused(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    """pyannote.ai with no key is the stored state's problem, before and after."""
    _store_source(db_session, normal_user, "pyannote")
    target = add_asr_config("deepgram", "nova-3")

    response = client.post(
        f"{_BASE}/set-active", json={"config_uuid": str(target.uuid)}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert "pyannote_key_missing" in _codes(response.json())
    assert _active_id(db_session, normal_user) == str(target.id)


def test_a_locked_source_is_never_refused(
    client, user_token_headers, normal_user, db_session, add_asr_config, caps_off
):
    """The deployment pins the source (#1109); the user cannot change it to get out."""
    caps_off("transcription.diarization_source")
    add_asr_config("deepgram", "nova-3", active=True)
    untimed = add_asr_config("openai", "gpt-4o-transcribe")

    response = client.post(
        f"{_BASE}/set-active", json={"config_uuid": str(untimed.uuid)}, headers=user_token_headers
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert {"asr_no_timing_for_speakers", "source_locked"} <= _codes(response.json())
    assert _active_id(db_session, normal_user) == str(untimed.id)


# --- clear-active -------------------------------------------------------------------------


def test_clearing_onto_an_untimed_env_default_is_refused(
    client, user_token_headers, normal_user, db_session, add_asr_config, monkeypatch
):
    monkeypatch.setenv("ASR_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-a-key")
    monkeypatch.setenv("OPENAI_ASR_MODEL", "gpt-4o-transcribe")
    current = add_asr_config("deepgram", "nova-3", active=True)

    response = client.post(f"{_BASE}/clear-active", headers=user_token_headers)

    assert_refused(response, "asr_no_timing_for_speakers")
    assert _active_id(db_session, normal_user) == str(current.id)


def test_clearing_onto_local_whisper_is_accepted(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    add_asr_config("openai", "whisper-1", active=True)

    response = client.post(f"{_BASE}/clear-active", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert "segment_timing_only" not in _codes(response.json())
    assert _active_id(db_session, normal_user) is None


def test_lite_reports_the_transcription_error_on_clear_without_refusing(
    client, user_token_headers, normal_user, db_session, add_asr_config, pipeline_env
):
    """Lite's transcription-step refusal is WP6's; until then it is reported, not silent."""
    pipeline_env("lite")
    add_asr_config("deepgram", "nova-3", active=True)

    response = client.post(f"{_BASE}/clear-active", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert "local_asr_unavailable" in _codes(response.json())


# --- create: first-config auto-activation ---------------------------------------------------


def _create(client, headers, provider: str, model: str):
    return client.post(
        _BASE,
        json={"name": f"{provider}-{model}-created", "provider": provider, "model_name": model},
        headers=headers,
    )


def test_a_first_untimed_config_is_saved_but_not_activated(
    client, user_token_headers, normal_user, db_session
):
    response = _create(client, user_token_headers, "openai", "gpt-4o-transcribe")

    assert response.status_code == status.HTTP_201_CREATED, response.text
    body = response.json()
    assert body["auto_activated"] is False
    assert _active_id(db_session, normal_user) is None
    # The pipeline in effect is still local Whisper: nothing to report about the new config.
    assert "asr_no_timing_for_speakers" not in _codes(body)


def test_a_first_runnable_config_is_auto_activated(
    client, user_token_headers, normal_user, db_session
):
    response = _create(client, user_token_headers, "deepgram", "nova-3")

    assert response.status_code == status.HTTP_201_CREATED, response.text
    body = response.json()
    assert body["auto_activated"] is True
    assert _active_id(db_session, normal_user) is not None
    assert "asr_no_timing_for_speakers" not in _codes(body)


# --- delete: promotion -------------------------------------------------------------------


def test_deleting_the_active_config_promotes_a_runnable_one_over_an_earlier_untimed_one(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    untimed = add_asr_config("openai", "gpt-4o-transcribe")
    active = add_asr_config("deepgram", "nova-3", active=True)
    runnable = add_asr_config("assemblyai", "universal")
    assert untimed.id < runnable.id, "control: the untimed config is the one picked first"

    response = client.delete(f"{_BASE}/config/{active.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    assert _active_id(db_session, normal_user) == str(runnable.id)


def test_deleting_the_active_config_clears_when_nothing_else_is_runnable(
    client, user_token_headers, normal_user, db_session, add_asr_config
):
    add_asr_config("openai", "gpt-4o-transcribe")
    active = add_asr_config("deepgram", "nova-3", active=True)

    response = client.delete(f"{_BASE}/config/{active.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    assert _active_id(db_session, normal_user) is None
