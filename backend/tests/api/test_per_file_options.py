"""Per-file transcription options reach the dispatch, or are refused (issue #1202).

Every test asserts the VALUE the pipeline dispatch receives, not that some function ran.
Dispatch is replaced by a recording stand-in because a real call queues GPU work; the
endpoints, request models and the row each test creates are real. ``SKIP_CELERY`` is
switched off per test so the endpoint takes the dispatching branch instead of the
test-mode shortcut that hid these bugs.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

DEPLOYMENT_MODEL = "large-v3-turbo"


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "per_file.wav",
        "title": "per_file",
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


@pytest.fixture
def rediarize_calls(monkeypatch):
    from app.tasks import rediarize_task

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setenv("SKIP_CELERY", "False")
    monkeypatch.setattr(
        rediarize_task.rediarize_task, "delay", lambda *a, **k: calls.append((a, k))
    )
    return calls


@pytest.fixture
def deployment_model(monkeypatch):
    monkeypatch.setattr(
        "app.utils.whisper_model_choice.deployment_model_name", lambda: DEPLOYMENT_MODEL
    )
    return DEPLOYMENT_MODEL


# ---------------------------------------------------------------------------
# #1202: disable_diarization
# ---------------------------------------------------------------------------


def test_full_reprocess_forwards_disable_diarization(
    client, user_token_headers, normal_user, db_session, dispatch_calls
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"disable_diarization": True},
    )
    assert response.status_code == status.HTTP_200_OK
    assert [c["disable_diarization"] for c in dispatch_calls] == [True]


def test_reprocess_without_the_flag_does_not_force_a_diarization_source(
    client, user_token_headers, normal_user, db_session, dispatch_calls
):
    """False must read as "not asked": the pipeline maps a bare False to ``provider``,
    which would overrule a user whose saved source is ``local`` or ``off``."""
    media_file = _make_file(db_session, normal_user)
    client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"disable_diarization": False},
    )
    assert [c["disable_diarization"] for c in dispatch_calls] == [None]


def test_selective_transcription_reprocess_forwards_disable_diarization(
    client, user_token_headers, normal_user, db_session, dispatch_calls
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"stages": ["transcription", "summarization"], "disable_diarization": True},
    )
    assert response.status_code == status.HTTP_200_OK
    assert [c["disable_diarization"] for c in dispatch_calls] == [True]


@pytest.mark.parametrize("stages", [["rediarize"], ["summarization"], ["rediarize", "analytics"]])
def test_reprocess_rejects_disable_diarization_for_stages_that_cannot_honour_it(
    client, user_token_headers, normal_user, db_session, dispatch_calls, rediarize_calls, stages
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"stages": stages, "disable_diarization": True},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "disable_diarization" in response.text
    assert dispatch_calls == []
    assert rediarize_calls == []


def test_bulk_full_reprocess_forwards_speaker_range_and_disable_diarization(
    client, user_token_headers, normal_user, db_session, dispatch_calls
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={
            "file_uuids": [str(media_file.uuid)],
            "action": "reprocess",
            "min_speakers": 2,
            "max_speakers": 4,
            "num_speakers": 3,
            "disable_diarization": True,
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()[0]["success"] is True
    assert len(dispatch_calls) == 1
    call = dispatch_calls[0]
    assert (call["min_speakers"], call["max_speakers"], call["num_speakers"]) == (2, 4, 3)
    assert call["disable_diarization"] is True


def test_bulk_selective_reprocess_forwards_disable_diarization(
    client, user_token_headers, normal_user, db_session, dispatch_calls
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={
            "file_uuids": [str(media_file.uuid)],
            "action": "reprocess",
            "stages": ["transcription"],
            "min_speakers": 3,
            "disable_diarization": True,
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assert [(c["min_speakers"], c["disable_diarization"]) for c in dispatch_calls] == [(3, True)]


def test_bulk_rejects_disable_diarization_on_other_actions(
    client, user_token_headers, normal_user, db_session, dispatch_calls
):
    media_file = _make_file(db_session, normal_user, file_status="error")
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={
            "file_uuids": [str(media_file.uuid)],
            "action": "retry",
            "disable_diarization": True,
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert dispatch_calls == []


def test_bulk_rejects_disable_diarization_for_rediarize_only(
    client, user_token_headers, normal_user, db_session, rediarize_calls
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={
            "file_uuids": [str(media_file.uuid)],
            "action": "reprocess",
            "stages": ["rediarize"],
            "disable_diarization": True,
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert rediarize_calls == []


def test_selective_rediarize_still_forwards_the_speaker_range(
    client, user_token_headers, normal_user, db_session, rediarize_calls
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"stages": ["rediarize"], "min_speakers": 3, "max_speakers": 5},
    )
    assert response.status_code == status.HTTP_200_OK
    assert [(k["min_speakers"], k["max_speakers"]) for _, k in rediarize_calls] == [(3, 5)]


# ---------------------------------------------------------------------------
# #1202: /prepare records, /complete replays and may override
# ---------------------------------------------------------------------------


@pytest.fixture
def complete_seams(monkeypatch):
    """Let /complete run without MinIO: the object "exists", its header cannot be read."""
    monkeypatch.setattr("app.services.minio_service.object_exists_and_size", lambda path: 4096)
    monkeypatch.setattr(
        "app.api.endpoints.files.complete_upload._fingerprint_object", lambda *a, **k: None
    )

    def _no_header(*a, **k):
        raise OSError("no storage in this test")

    monkeypatch.setattr("app.services.minio_service.range_read", _no_header)


def _prepare(client, headers, **extra) -> str:
    payload = {"filename": "prep.wav", "file_size": 4096, "content_type": "audio/wav", **extra}
    response = client.post("/api/files/prepare", headers=headers, json=payload)
    assert response.status_code == status.HTTP_200_OK, response.text
    return str(response.json()["file_id"])


def test_prepare_records_the_request_on_the_row(client, user_token_headers, db_session):
    file_id = _prepare(
        client,
        user_token_headers,
        min_speakers=2,
        max_speakers=6,
        num_speakers=4,
        disable_diarization=True,
    )
    row = db_session.query(MediaFile).filter(MediaFile.uuid == file_id).one()
    assert (row.requested_min_speakers, row.requested_max_speakers) == (2, 6)
    assert row.requested_num_speakers == 4
    assert row.requested_disable_diarization is True


def test_complete_dispatches_what_prepare_recorded(
    client, user_token_headers, db_session, pipeline_stages, complete_seams
):
    file_id = _prepare(
        client,
        user_token_headers,
        min_speakers=2,
        max_speakers=6,
        disable_diarization=True,
    )
    response = client.post(
        "/api/files/complete", headers=user_token_headers, json={"file_id": file_id}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert len(pipeline_stages) == 1
    stage = pipeline_stages[0]
    assert (stage["min_speakers"], stage["max_speakers"]) == (2, 6)
    assert stage["diarization_source"] == "off"


def test_complete_with_disable_diarization_dispatches_it(
    client, user_token_headers, db_session, pipeline_stages, complete_seams
):
    file_id = _prepare(client, user_token_headers, min_speakers=2)
    response = client.post(
        "/api/files/complete",
        headers=user_token_headers,
        json={"file_id": file_id, "disable_diarization": True, "min_speakers": 3},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert len(pipeline_stages) == 1
    stage = pipeline_stages[0]
    # /complete repeats the range, so its value wins over what /prepare recorded.
    assert stage["min_speakers"] == 3
    assert stage["disable_diarization"] is True
    row = db_session.query(MediaFile).filter(MediaFile.uuid == file_id).one()
    assert row.requested_min_speakers == 3
    assert row.requested_disable_diarization is True


# ---------------------------------------------------------------------------
# #1202: a model nothing can serve is a 422, not a silent fallback
# ---------------------------------------------------------------------------


def test_reprocess_rejects_a_model_that_is_neither_pinned_nor_lightweight(
    client, user_token_headers, normal_user, db_session, dispatch_calls, deployment_model
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"whisper_model": "large-v2"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "large-v2" in response.json()["detail"]
    assert DEPLOYMENT_MODEL in response.json()["detail"]
    assert dispatch_calls == []


@pytest.mark.parametrize("model", ["tiny", "base", DEPLOYMENT_MODEL])
def test_reprocess_accepts_lightweight_and_the_deployment_model(
    client, user_token_headers, normal_user, db_session, dispatch_calls, deployment_model, model
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"whisper_model": model},
    )
    assert response.status_code == status.HTTP_200_OK
    assert [c["whisper_model"] for c in dispatch_calls] == [model]


def test_prepare_and_complete_reject_an_unservable_model(
    client, user_token_headers, deployment_model, complete_seams, dispatch_calls
):
    response = client.post(
        "/api/files/prepare",
        headers=user_token_headers,
        json={
            "filename": "m.wav",
            "file_size": 1,
            "content_type": "audio/wav",
            "whisper_model": "medium",
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    file_id = _prepare(client, user_token_headers)
    response = client.post(
        "/api/files/complete",
        headers=user_token_headers,
        json={"file_id": file_id, "whisper_model": "medium"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert dispatch_calls == []
