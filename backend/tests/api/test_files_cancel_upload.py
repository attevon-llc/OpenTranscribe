"""Characterization tests for the upload-cancel branch of ``DELETE /api/files/{uuid}``.

The single ``DELETE /api/files/{file_uuid}`` route lives in
``files/cancel_upload.py``. It has two arms:

1. **Cancel** — when the target is a ``PENDING`` upload owned by the caller it
   takes the lightweight cleanup path (drop the partial MinIO object + row, 204).
2. **Fall-through** — anything else (not pending, not owned, missing) delegates
   to ``crud.delete_media_file``, which applies the full permission + safety
   checks (403 for a non-owner, 404 for a missing UUID, etc.).

These tests pin the cancel arm and its boundaries. (The general delete arm is
also covered in ``test_files_crud.py``; here we focus on PENDING cancel and the
cross-arm authz/edge behavior owned by this module.) Rows are created on the
savepoint session and roll back; the partial-upload object is fake so the
best-effort MinIO delete is a harmless no-op.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile


def _make_file(db_session, owner, *, file_status: str, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "cancel_test.wav",
        "title": "cancel_test",
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


# ---------------------------------------------------------------------------
# Cancel arm: PENDING upload owned by the caller → 204
# ---------------------------------------------------------------------------


def test_cancel_pending_upload_204(client, user_token_headers, normal_user, db_session):
    """A PENDING upload owned by the caller is cancelled (204) and the row removed."""
    media_file = _make_file(db_session, normal_user, file_status="pending")
    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT
    # The row is gone; a follow-up detail read is 404.
    follow_up = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


def test_cancel_pending_upload_no_storage_path_204(
    client, user_token_headers, normal_user, db_session
):
    """A PENDING upload with no stored object (storage_path empty) still cancels
    cleanly — the storage-delete step is skipped."""
    media_file = _make_file(db_session, normal_user, file_status="pending", storage_path="")
    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT


# ---------------------------------------------------------------------------
# Fall-through arm: not-pending / not-owned / missing
# ---------------------------------------------------------------------------


def test_cancel_completed_file_falls_through_to_delete_204(
    client, user_token_headers, normal_user, db_session
):
    """A COMPLETED file isn't a pending upload → full delete path → 204."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_cancel_other_users_pending_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    """Another user's PENDING upload is NOT matched by the owner-scoped pending
    query, so it falls through to delete_media_file → 403 (the cancel arm never
    leaks another user's pending upload)."""
    media_file = _make_file(db_session, normal_user, file_status="pending")
    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_cancel_other_users_completed_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_cancel_nonexistent_404(client, user_token_headers):
    """A missing UUID falls through to delete_media_file → 404 'File not found'."""
    response = client.delete(f"/api/files/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_cancel_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user, file_status="pending")
    response = client.delete(f"/api/files/{media_file.uuid}")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_cancel_malformed_uuid_404(client, user_token_headers):
    """BUGFIX (this branch, files/cancel_upload.py): a malformed UUID previously
    reached the raw ``MediaFile.uuid == "<garbage>"`` query and triggered an
    unhandled 500 (Postgres ``invalid input syntax for type uuid``) plus a
    poisoned transaction. The route now rejects it up front with 404 'File not
    found', matching every other delete entry point. Valid input is unchanged.
    """
    response = client.delete("/api/files/not-a-uuid", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_cancel_admin_can_delete_others_completed(
    client, admin_token_headers, normal_user, db_session
):
    """An admin's delete of another user's file flows through the fall-through
    arm's admin bypass → 204 (the pending arm is owner-scoped, so admins always
    use the full-delete path here)."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.delete(f"/api/files/{media_file.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT
