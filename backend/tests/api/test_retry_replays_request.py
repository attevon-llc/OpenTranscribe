"""A retry replays the file's own request (issue #1203).

Single retry, bulk retry and the recovery routes used to re-dispatch with defaults: the model,
speaker range and "skip diarization" the file was submitted with were gone, and a file using
``local``/``pyannote`` diarization was rewritten to the provider's own. These assert what the
first pipeline stage is built with after a retry (real dispatch, only the publish stubbed) and
what the endpoints hand to dispatch.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile


def _make_file(db_session, owner, *, file_status: str = "error", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "retry_test.wav",
        "title": "retry_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": file_status,
        "is_public": False,
        "retry_count": 0,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


@pytest.fixture
def dispatch_calls(monkeypatch):
    """Record every ``dispatch_transcription_pipeline`` call made by an endpoint."""
    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return "spy-task-id"

    monkeypatch.setenv("SKIP_CELERY", "False")
    monkeypatch.setattr("app.tasks.transcription.dispatch_transcription_pipeline", _spy)
    monkeypatch.setattr("app.api.endpoints.files.management.dispatch_transcription_pipeline", _spy)
    return calls


def test_single_retry_replays_the_stored_request_and_does_not_force_a_source(
    client, user_token_headers, normal_user, db_session, dispatch_calls
):
    media_file = _make_file(
        db_session,
        normal_user,
        file_status="error",
        requested_whisper_model="tiny",
        requested_min_speakers=2,
        diarization_disabled=False,
    )
    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert len(dispatch_calls) == 1
    call = dispatch_calls[0]
    assert call["reuse_requested_options"] is True
    # A bare False here was mapped to diarization_source="provider" downstream.
    assert call.get("disable_diarization") is None
    assert call.get("diarization_source") is None


def test_bulk_retry_replays_the_stored_request(
    client, user_token_headers, normal_user, db_session, dispatch_calls
):
    media_file = _make_file(db_session, normal_user, file_status="error")
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(media_file.uuid)], "action": "retry"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert [c["reuse_requested_options"] for c in dispatch_calls] == [True]


def _retry_single(client, headers, media_file):
    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text


def _retry_bulk(client, headers, media_file):
    response = client.post(
        "/api/files/management/bulk-action",
        headers=headers,
        json={"file_uuids": [str(media_file.uuid)], "action": "retry"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()[0]["success"] is True, response.text


@pytest.mark.parametrize("retry", [_retry_single, _retry_bulk], ids=["single", "bulk"])
class TestRetryReachesThePipelineWithTheFilesOwnRequest:
    """What the first pipeline stage is built with after a retry (issue #1203)."""

    def test_fast_model_and_speaker_range_survive(
        self, client, user_token_headers, normal_user, db_session, pipeline_stages, retry
    ):
        media_file = _make_file(
            db_session,
            normal_user,
            file_status="error",
            requested_whisper_model="tiny",
            requested_min_speakers=2,
            requested_max_speakers=4,
        )
        retry(client, user_token_headers, media_file)
        assert len(pipeline_stages) == 1
        stage = pipeline_stages[0]
        assert stage["whisper_model"] == "tiny"
        assert (stage["min_speakers"], stage["max_speakers"]) == (2, 4)

    def test_a_normal_file_is_not_forced_onto_the_provider_source(
        self, client, user_token_headers, normal_user, db_session, pipeline_stages, retry
    ):
        """None lets the pipeline read the user's saved source (``local``/``pyannote``/...)."""
        media_file = _make_file(db_session, normal_user, file_status="error")
        retry(client, user_token_headers, media_file)
        stage = pipeline_stages[0]
        assert stage["diarization_source"] is None
        assert stage["disable_diarization"] is None
        assert stage["whisper_model"] is None

    def test_skipped_diarization_stays_skipped(
        self, client, user_token_headers, normal_user, db_session, pipeline_stages, retry
    ):
        media_file = _make_file(
            db_session, normal_user, file_status="error", requested_disable_diarization=True
        )
        retry(client, user_token_headers, media_file)
        assert pipeline_stages[0]["diarization_source"] == "off"

    def test_a_run_that_already_had_diarization_off_stays_off(
        self, client, user_token_headers, normal_user, db_session, pipeline_stages, retry
    ):
        media_file = _make_file(
            db_session, normal_user, file_status="error", diarization_disabled=True
        )
        retry(client, user_token_headers, media_file)
        assert pipeline_stages[0]["diarization_source"] == "off"
