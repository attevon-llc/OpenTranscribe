"""``GET /user-settings/pipeline``: the caller's processing pipeline, read-only (#1224).

The view is built from the same resolver the pipeline uses, so these tests drive it
through real stored settings (ASR config rows, the pyannote.ai key row, the source
setting) and assert what the four steps report, including where audio goes.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.models import UserSetting

_URL = "/api/user-settings/pipeline"

pytestmark = pytest.mark.usefixtures("pipeline_env")


def _set_source(db_session, user, value: str) -> None:
    db_session.add(
        UserSetting(
            user_id=user.id, setting_key="transcription_diarization_source", setting_value=value
        )
    )
    db_session.commit()


def _view(client, headers) -> dict:
    response = client.get(_URL, headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    body: dict = response.json()
    return body


def _step(view: dict, name: str) -> dict:
    step: dict = next(s for s in view["steps"] if s["step"] == name)
    return step


def test_four_steps_in_processing_order(client, user_token_headers):
    view = _view(client, user_token_headers)
    assert [s["step"] for s in view["steps"]] == [
        "transcription",
        "diarization",
        "speaker_id",
        "llm",
    ]
    assert view["deployment_mode"] == "full"
    assert view["diarization_source_locked"] is False


def test_default_user_runs_everything_on_this_server(client, user_token_headers):
    view = _view(client, user_token_headers)
    transcription = _step(view, "transcription")
    assert transcription["engine"] == "local_gpu"
    assert transcription["where"] == "server"
    assert transcription["timing"] == "word"
    assert transcription["audio_leaves_server"] is False
    diarization = _step(view, "diarization")
    assert (diarization["engine"], diarization["execution"]) == ("local", "inline_local")
    assert diarization["provider"] == "native"
    assert diarization["speaker_hints"] == []
    assert _step(view, "speaker_id")["engine"] == "inline_native"
    assert "speaker_hints_ignored" in {i["code"] for i in view["issues"]}


def test_local_whisper_with_pyannote_ai_shows_audio_leaving_for_speakers_only(
    client, user_token_headers, normal_user, db_session, store_pyannote_key
):
    store_pyannote_key()
    _set_source(db_session, normal_user, "pyannote")
    view = _view(client, user_token_headers)

    assert _step(view, "transcription")["audio_leaves_server"] is False
    diarization = _step(view, "diarization")
    assert diarization["engine"] == "pyannote_ai"
    assert diarization["execution"] == "post_stage_cloud"
    assert diarization["where"] == "cloud"
    assert diarization["audio_leaves_server"] is True
    assert diarization["provider_display"] == "pyannote.ai"
    assert diarization["speaker_hints"] == ["max", "min", "num"]
    speaker_id = _step(view, "speaker_id")
    assert (speaker_id["where"], speaker_id["engine"]) == ("server", "embedding_stage")
    assert not [i for i in view["issues"] if i["severity"] == "error"]


def test_cloud_transcription_step_reports_speakers_and_timing(
    client, user_token_headers, add_asr_config
):
    add_asr_config("openai", "whisper-1", active=True)
    view = _view(client, user_token_headers)
    transcription = _step(view, "transcription")
    assert transcription["engine"] == "cloud"
    assert transcription["provider"] == "openai"
    assert transcription["provider_display"] == "OpenAI"
    assert transcription["returns_speakers"] is False
    assert transcription["timing"] == "segment"
    assert transcription["audio_leaves_server"] is True
    codes = {i["code"]: i for i in view["issues"]}
    assert codes["automatic_uses_server"]["severity"] == "info"
    assert codes["segment_timing_only"]["step"] == "diarization"
    assert codes["segment_timing_only"]["message"].startswith("Your transcription model")


def test_a_stale_stored_selection_is_reported_not_hidden(
    client, user_token_headers, normal_user, db_session
):
    _set_source(db_session, normal_user, "pyannote")  # no key stored
    view = _view(client, user_token_headers)
    errors = [i for i in view["issues"] if i["severity"] == "error"]
    assert [e["code"] for e in errors] == ["pyannote_key_missing"]


def test_lite_reports_local_speaker_detection_as_unavailable(
    client, user_token_headers, normal_user, db_session, add_asr_config, pipeline_env
):
    pipeline_env("lite")
    add_asr_config("deepgram", "nova-3", active=True)
    _set_source(db_session, normal_user, "local")
    view = _view(client, user_token_headers)
    assert view["deployment_mode"] == "lite"
    errors = {i["code"] for i in view["issues"] if i["severity"] == "error"}
    assert errors == {"local_diarization_unavailable"}


def test_locked_source_is_reported_and_applied(
    client, user_token_headers, normal_user, db_session, caps_off
):
    _set_source(db_session, normal_user, "off")
    caps_off("transcription.diarization_source")
    view = _view(client, user_token_headers)
    assert view["diarization_source_locked"] is True
    assert _step(view, "diarization")["engine"] == "local"  # the default, not the stored off
    assert "source_locked" in {i["code"] for i in view["issues"]}


def test_hidden_with_the_transcription_preferences(client, user_token_headers, caps_off):
    caps_off("transcription.prefs")
    assert client.get(_URL, headers=user_token_headers).status_code == 404


def test_requires_authentication(client):
    assert client.get(_URL).status_code == status.HTTP_401_UNAUTHORIZED


def test_llm_step_matches_the_llm_resolution(client, user_token_headers, normal_user, db_session):
    from app.services.llm_service import LLMService

    llm = _step(_view(client, user_token_headers), "llm")
    configured = LLMService.is_configured_for_user(db_session, normal_user.id)
    assert llm["enabled"] is configured
    assert llm["audio_leaves_server"] is False
    assert llm["settings_section"] == "llm-provider"


def test_no_network_call_is_made(
    client, user_token_headers, normal_user, db_session, monkeypatch, store_pyannote_key
):
    """The view reads the DB only: no vendor test, no LLM probe, no outbound connection.

    (The test client is in-process, and the DB connection is already open, so any new
    TCP connection is the view reaching out.) The LLM step's locality check may still
    resolve a bare hostname, which is ``llm_guard.is_local_provider``'s documented rule.
    """
    import socket

    import httpx
    import requests

    calls: list[str] = []

    def _refuse(*_args, **_kwargs):
        calls.append("network")
        raise OSError("network disabled in this test")

    store_pyannote_key()
    _set_source(db_session, normal_user, "pyannote")
    monkeypatch.setattr(requests.Session, "request", _refuse)
    monkeypatch.setattr(httpx, "get", _refuse)
    monkeypatch.setattr(httpx, "post", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    response = client.get(_URL, headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _step(response.json(), "diarization")["engine"] == "pyannote_ai"
    assert calls == []
