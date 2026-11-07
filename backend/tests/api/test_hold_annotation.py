"""A new comment on a held file is allowed and recorded as ``content.hold.annotation``.

A legal hold freezes the file's content, but a comment is an annotation beside it, not a change
to it (plan §3.1, W52/W53). Both create routes commit the comment with exactly one outbox row
for it; a comment on a file nobody holds stages nothing. The edit and delete of an EXISTING
comment are refused instead (``test_hold_freeze_routes.py``).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status
from sqlalchemy import text

from app.models.media import Comment
from app.models.media import MediaFile


def _file(db_session, owner, *, held: bool) -> MediaFile:
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
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _annotations(db_session, mf: MediaFile) -> list:
    return list(
        db_session.execute(
            text(
                "SELECT payload FROM audit_outbox WHERE event_type = 'content.hold.annotation' "
                "AND payload->>'resource_uuid' = :f"
            ),
            {"f": str(mf.uuid)},
        ).scalars()
    )


def _create(client, headers, mf: MediaFile, route: str):
    body = {"text": "a note for the record", "timestamp": 1.5}
    if route == "nested":
        return client.post(f"/api/comments/files/{mf.uuid}/comments", json=body, headers=headers)
    return client.post(
        "/api/comments", json={**body, "media_file_id": str(mf.uuid)}, headers=headers
    )


@pytest.mark.parametrize("route", ["nested", "standalone"])
def test_a_comment_on_a_held_file_is_created_and_recorded(
    route, client, user_token_headers, normal_user, db_session
):
    mf = _file(db_session, normal_user, held=True)

    response = _create(client, user_token_headers, mf, route)

    assert response.status_code == status.HTTP_200_OK, response.text
    comment_uuid = response.json()["uuid"]
    assert db_session.query(Comment).filter(Comment.media_file_id == mf.id).count() == 1
    (payload,) = _annotations(db_session, mf)
    assert payload["resource_type"] == "media_file"
    # ``source_unparsed`` is the request's client address the audit layer adds to every event.
    details = {k: v for k, v in payload["details"].items() if k != "source_unparsed"}
    assert details == {"comment_uuid": comment_uuid, "held_by": ["platform"]}
    assert "a note for the record" not in str(payload)


@pytest.mark.parametrize("route", ["nested", "standalone"])
def test_a_comment_on_a_free_file_records_no_annotation(
    route, client, user_token_headers, normal_user, db_session
):
    mf = _file(db_session, normal_user, held=False)

    response = _create(client, user_token_headers, mf, route)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert _annotations(db_session, mf) == []
