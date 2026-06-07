"""Characterization tests for ``files/reprocess.py``.

Covers ``POST /api/files/{uuid}/reprocess``. Under ``SKIP_CELERY=True`` the
dispatch helpers short-circuit (no broker publish), so these assert the API
contract — status reset, response shape, stage validation, authz — without
running any worker. Savepoint-isolated rows roll back at teardown; the reprocess
path mutates only the file row it created, so dev data is never touched.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "reproc_test.wav",
        "title": "reproc_test",
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


def test_reprocess_full_owner(client, user_token_headers, normal_user, db_session):
    """A full reprocess (no body) returns the updated file (Celery no-op'd)."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(f"/api/files/{media_file.uuid}/reprocess", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uuid"] == str(media_file.uuid)


def test_reprocess_selective_stages(client, user_token_headers, normal_user, db_session):
    """A selective reprocess with valid stages succeeds."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"stages": ["summarization"]},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uuid"] == str(media_file.uuid)


def test_reprocess_invalid_stage_422(client, user_token_headers, normal_user, db_session):
    """``stages`` is a constrained Literal list → garbage stage is a 422."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess",
        headers=user_token_headers,
        json={"stages": ["not_a_real_stage"]},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_reprocess_no_storage_path_400(client, user_token_headers, normal_user, db_session):
    """A file with an empty storage_path cannot be reprocessed."""
    media_file = _make_file(db_session, normal_user, storage_path="")
    response = client.post(f"/api/files/{media_file.uuid}/reprocess", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "File storage path not found. Cannot reprocess."


def test_reprocess_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.post(f"/api/files/{media_file.uuid}/reprocess")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_reprocess_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/reprocess", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_reprocess_admin_any_file(client, admin_token_headers, normal_user, db_session):
    """An admin may reprocess another user's file."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(f"/api/files/{media_file.uuid}/reprocess", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK


def test_reprocess_nonexistent_404(client, user_token_headers):
    response = client.post(f"/api/files/{uuid.uuid4()}/reprocess", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_reprocess_malformed_uuid_400(client, user_token_headers):
    """``reprocess`` declares ``file_uuid: str`` → bad UUID = 400 from get_by_uuid."""
    response = client.post("/api/files/not-a-uuid/reprocess", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
