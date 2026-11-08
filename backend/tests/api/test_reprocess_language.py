"""Forcing the transcription language when reprocessing (issue #1263).

Validation runs through the real endpoint; the dispatch is captured at the point where the
pipeline would be built, so the assertion is on what the pipeline is actually handed.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile


@pytest.fixture
def dispatched(monkeypatch):
    """The storage probe says the object exists; capture the pipeline dispatch."""
    monkeypatch.setenv("SKIP_S3", "False")
    monkeypatch.setattr(
        "app.services.minio_service.object_exists_and_size", lambda name: (name, 1024)
    )
    monkeypatch.setattr(
        "app.api.endpoints.files.reprocess_dispatch.env_bool", lambda *a, **k: False
    )
    calls: list[dict] = []

    def _capture(**kwargs) -> str:
        calls.append(kwargs)
        return "task"

    monkeypatch.setattr("app.tasks.transcription.dispatch_transcription_pipeline", _capture)
    return calls


def _file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    row = MediaFile(
        uuid=file_uuid,
        filename=f"lang_{file_uuid[:8]}.mp3",
        title="lang",
        storage_path=f"media/test/{file_uuid}.mp3",
        content_type="audio/mpeg",
        file_size=4096,
        status="completed",
        is_public=False,
        retry_count=0,
        user_id=owner.id,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _post(client, headers, row, body):
    return client.post(f"/api/files/{row.uuid}/reprocess", headers=headers, json=body)


def test_unknown_language_is_422_and_nothing_dispatches(
    client, user_token_headers, normal_user, db_session, dispatched
):
    row = _file(db_session, normal_user)
    response = _post(client, user_token_headers, row, {"language": "klingon"})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert dispatched == []


def test_language_on_a_stage_that_does_not_transcribe_is_422(
    client, user_token_headers, normal_user, db_session, dispatched
):
    row = _file(db_session, normal_user)
    response = _post(
        client, user_token_headers, row, {"stages": ["summarization"], "language": "fr"}
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "transcription" in response.text
    assert dispatched == []


@pytest.mark.parametrize(
    "body", [{"language": "fr"}, {"stages": ["transcription"], "language": "fr"}]
)
def test_the_forced_language_reaches_the_pipeline_dispatch(
    client, user_token_headers, normal_user, db_session, dispatched, body
):
    row = _file(db_session, normal_user)
    response = _post(client, user_token_headers, row, body)
    assert response.status_code == status.HTTP_200_OK
    assert [c["source_language"] for c in dispatched] == ["fr"]


def test_no_language_leaves_the_users_setting_in_charge(
    client, user_token_headers, normal_user, db_session, dispatched
):
    row = _file(db_session, normal_user)
    assert _post(client, user_token_headers, row, {}).status_code == status.HTTP_200_OK
    assert [c["source_language"] for c in dispatched] == [None]
