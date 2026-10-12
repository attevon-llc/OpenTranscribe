"""``processing_steps`` on the file detail and the file status responses (issue #1229).

Both surfaces serve the same six display-ready rows from ``build_processing_steps``. The
rows ride inside the file responses, so they inherit those responses' gates: a viewer the
file is shared with sees them, a quarantined file stays a 404 for everyone but an admin,
and nothing in them is a URL or a configuration id.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.sharing import CollectionShare
from app.services.file_provenance import record_stage

STEPS = ["transcription", "diarization", "voiceprints", "summary", "topics", "speaker_names"]


def _make_file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": f"provenance_api_{file_uuid[:8]}.wav",
        "title": "provenance_api",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _record_a_run(db_session, media_file) -> None:
    record_stage(
        db_session,
        media_file.id,
        "transcription",
        new_run=True,
        provider="openai",
        model="whisper-1",
        where="cloud",
        engine="cloud",
        timing="segment",
    )
    record_stage(
        db_session,
        media_file.id,
        "diarization",
        provider="pyannote.ai",
        model="precision-3",
        where="cloud",
        engine="pyannote_ai",
    )
    db_session.commit()


def _detail_steps(client, headers, media_file) -> dict:
    response = client.get(f"/api/files/{media_file.uuid}", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    rows = response.json()["processing_steps"]
    assert [row["step"] for row in rows] == STEPS
    return {row["step"]: row for row in rows}


def _status_steps(client, headers, media_file) -> dict:
    response = client.get(f"/api/my-files/{media_file.uuid}/status", headers=headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    rows = response.json()["file"]["processing_steps"]
    assert [row["step"] for row in rows] == STEPS
    return {row["step"]: row for row in rows}


@pytest.mark.parametrize("surface", [_detail_steps, _status_steps])
def test_a_recorded_run_is_served_display_ready(
    client, user_token_headers, normal_user, db_session, surface
):
    media_file = _make_file(db_session, normal_user)
    _record_a_run(db_session, media_file)

    steps = surface(client, user_token_headers, media_file)

    transcription = steps["transcription"]
    assert transcription == {
        "step": "transcription",
        "status": "done",
        "recorded": "full",
        "provider": "openai",
        "provider_display": "OpenAI",
        "model": "whisper-1",
        "where": "cloud",
        "timing": "segment",
    }
    assert steps["diarization"]["provider_display"] == "pyannote.ai"
    assert steps["diarization"]["where"] == "cloud"
    assert steps["summary"]["status"] == "not_run"


@pytest.mark.parametrize("surface", [_detail_steps, _status_steps])
def test_an_old_file_says_not_recorded_instead_of_guessing(
    client, user_token_headers, normal_user, db_session, surface
):
    media_file = _make_file(
        db_session,
        normal_user,
        asr_provider="local",
        asr_model="large-v3",
        diarization_provider="pyannote",
    )

    steps = surface(client, user_token_headers, media_file)

    assert (steps["transcription"]["recorded"], steps["transcription"]["where"]) == (
        "legacy",
        "server",
    )
    # pyannote: the local engine or the cloud service, nobody can tell any more.
    assert steps["diarization"]["where"] is None
    for step in ("summary", "topics", "speaker_names"):
        assert steps[step]["status"] == "not_recorded"


def test_a_viewer_the_file_is_shared_with_sees_the_same_rows(
    client, normal_user, other_user, other_user_auth_headers, db_session
):
    media_file = _make_file(db_session, normal_user)
    _record_a_run(db_session, media_file)
    collection = Collection(user_id=normal_user.id, name=f"provenance-{uuid.uuid4().hex[:8]}")
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=normal_user.id,
            target_type="user",
            target_user_id=other_user.id,
            permission="viewer",
        )
    )
    db_session.commit()

    steps = _detail_steps(client, other_user_auth_headers, media_file)
    assert steps["diarization"]["provider"] == "pyannote.ai"


def test_a_stranger_gets_nothing(client, normal_user, other_user_auth_headers, db_session):
    media_file = _make_file(db_session, normal_user)
    _record_a_run(db_session, media_file)

    response = client.get(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert "processing_steps" not in response.text


@pytest.mark.parametrize("path", ["/api/files/{uuid}", "/api/my-files/{uuid}/status"])
def test_a_quarantined_file_stays_hidden_from_its_owner(
    client, user_token_headers, normal_user, db_session, path
):
    media_file = _make_file(db_session, normal_user, is_quarantined=True)
    _record_a_run(db_session, media_file)

    response = client.get(path.format(uuid=media_file.uuid), headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert "pyannote.ai" not in response.text
