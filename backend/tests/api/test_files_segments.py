"""Characterization tests for ``files/segments.py``.

Covers ``GET /api/files/{uuid}/segments`` — the lightweight paginated transcript
endpoint. Savepoint-isolated rows roll back at teardown; fabricated segments
exercise the populated path without touching dev data or object storage.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile
from app.models.media import TranscriptSegment


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "seg_test.wav",
        "title": "seg_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": file_status,
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _add_segments(db_session, media_file, n: int = 3) -> None:
    for i in range(n):
        db_session.add(
            TranscriptSegment(
                media_file_id=media_file.id,
                start_time=float(i),
                end_time=float(i) + 0.9,
                text=f"segment {i}",
            )
        )
    db_session.commit()


def test_segments_owner_populated(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    _add_segments(db_session, media_file, n=3)
    response = client.get(f"/api/files/{media_file.uuid}/segments", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total_segments"] == 3
    assert len(body["transcript_segments"]) == 3


def test_segments_empty(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/segments", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total_segments"] == 0
    assert body["transcript_segments"] == []


def test_segments_pagination(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    _add_segments(db_session, media_file, n=5)
    response = client.get(
        f"/api/files/{media_file.uuid}/segments",
        headers=user_token_headers,
        params={"segment_limit": 2, "segment_offset": 0},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total_segments"] == 5
    assert len(body["transcript_segments"]) == 2


def test_segments_limit_zero_422(client, user_token_headers, normal_user, db_session):
    """``segment_limit`` is constrained ge=1."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/segments",
        headers=user_token_headers,
        params={"segment_limit": 0},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_segments_negative_offset_422(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/segments",
        headers=user_token_headers,
        params={"segment_offset": -1},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_segments_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/segments")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_segments_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/segments", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_segments_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/segments", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_segments_malformed_uuid_422(client, user_token_headers):
    """``segments`` declares ``file_uuid: UUID`` → FastAPI 422 (NOT 400)."""
    response = client.get("/api/files/not-a-uuid/segments", headers=user_token_headers)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
