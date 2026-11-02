"""A held file's speakers and summary cannot be destroyed, the same way it cannot be reprocessed.

Reprocessing a held file is refused (409 ``FILE_UNDER_LEGAL_HOLD``), so deleting its summary or a
speaker (which removes the voiceprint copies) must be refused with the same body, otherwise the
hold could be defeated one edit at a time and the record could not be rebuilt afterwards. Each test
pairs the refusal with the control that the same call works once the hold is released.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.models.media import MediaFile
from app.models.media import Speaker
from app.services.hold_guard import LEGAL_HOLD_ERROR_CODE


def _file(db_session, owner, *, held: bool, summary: bool = False) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="held.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        legal_hold=held,
        summary_data={"bluf": "kept"} if summary else None,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _speaker(db_session, owner, mf) -> Speaker:
    speaker = Speaker(user_id=owner.id, media_file_id=mf.id, name="SPEAKER_00")
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return speaker


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


def _refused(response) -> None:
    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    detail = response.json()["detail"]
    assert detail["error"] == LEGAL_HOLD_ERROR_CODE
    assert detail["held_by"] == ["platform"]


def _refusals(events, action: str) -> list[dict]:
    return [
        e
        for e in events
        if e["event_type"] is AuditEventType.CONTENT_HOLD_REFUSED
        and e["details"]["action"] == action
    ]


def test_deleting_a_held_files_summary_is_refused(
    client, user_token_headers, normal_user, db_session, events
):
    mf = _file(db_session, normal_user, held=True, summary=True)
    _refused(client.delete(f"/api/files/{mf.uuid}/summary", headers=user_token_headers))
    db_session.refresh(mf)
    assert mf.summary_data == {"bluf": "kept"}
    refused = _refusals(events, "summary_delete")
    assert len(refused) == 1 and refused[0]["resource_uuid"] == str(mf.uuid)


def test_deleting_an_unheld_files_summary_works(
    client, user_token_headers, normal_user, db_session
):
    mf = _file(db_session, normal_user, held=False, summary=True)
    response = client.delete(f"/api/files/{mf.uuid}/summary", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    db_session.refresh(mf)
    assert mf.summary_data is None


def test_deleting_a_held_files_speaker_is_refused(
    client, user_token_headers, normal_user, db_session, events
):
    mf = _file(db_session, normal_user, held=True)
    speaker = _speaker(db_session, normal_user, mf)
    speaker_uuid = speaker.uuid
    with patch("app.api.endpoints.speakers.capture_speaker_impact") as capture:
        _refused(client.delete(f"/api/speakers/{speaker_uuid}", headers=user_token_headers))
    capture.assert_not_called()
    assert db_session.query(Speaker).filter(Speaker.uuid == speaker_uuid).count() == 1
    refused = _refusals(events, "speaker_delete")
    assert len(refused) == 1 and refused[0]["resource_uuid"] == str(mf.uuid)


def test_deleting_an_unheld_files_speaker_works(
    client, user_token_headers, normal_user, db_session
):
    mf = _file(db_session, normal_user, held=False)
    speaker = _speaker(db_session, normal_user, mf)
    speaker_uuid = speaker.uuid
    with (
        patch("app.api.endpoints.speakers.capture_speaker_impact"),
        patch("app.api.endpoints.speakers.reconcile_speaker_impact"),
        patch("app.api.endpoints.speakers.raise_if_incomplete"),
    ):
        response = client.delete(f"/api/speakers/{speaker_uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    db_session.expire_all()
    assert db_session.query(Speaker).filter(Speaker.uuid == speaker_uuid).count() == 0


@pytest.mark.parametrize("held", [True, False])
def test_dismissing_topic_suggestions_keeps_the_data_either_way(
    client, user_token_headers, normal_user, db_session, held
):
    """Dismissal only flips a status and keeps the suggestion, so it is not a destroy path."""
    from app.models.topic import TopicSuggestion

    mf = _file(db_session, normal_user, held=held)
    db_session.add(TopicSuggestion(media_file_id=mf.id, user_id=normal_user.id, status="pending"))
    db_session.commit()
    response = client.delete(f"/api/files/{mf.uuid}/suggestions", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    row = db_session.query(TopicSuggestion).filter(TopicSuggestion.media_file_id == mf.id).one()
    db_session.refresh(row)
    assert row.status == "rejected"
