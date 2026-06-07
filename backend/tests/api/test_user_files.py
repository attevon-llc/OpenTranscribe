"""Functional characterization tests for the user-files endpoints.

Covers ``user_files.py`` (mounted at ``/api/my-files``):

- ``GET  /api/my-files/status``                (status counts + problem + recent)
- ``GET  /api/my-files/{uuid}/status``         (per-file detail + tasks)
- ``POST /api/my-files/{uuid}/retry``          (retry gating)
- ``POST /api/my-files/request-recovery``      (background recovery dispatch)

These are read/listing surfaces scoped to the current user. Rows live on the
savepoint-isolated ``db_session``; Celery / background dispatch is no-opped by
conftest, so retry/recovery exercise the API path only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from fastapi import status

from app.models.media import MediaFile


def _make_file(
    db_session,
    owner,
    *,
    file_status: str = "completed",
    upload_age_hours: float = 0.0,
    filename: str = "myfile.wav",
) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=filename,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=file_status,
        upload_time=datetime.now(timezone.utc) - timedelta(hours=upload_age_hours),
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


# ---------------------------------------------------------------------------
# GET /my-files/status
# ---------------------------------------------------------------------------


def test_status_unauthorized(client):
    response = client.get("/api/my-files/status")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_status_counts_shape(client, user_token_headers, normal_user, db_session):
    _make_file(db_session, normal_user, file_status="completed")
    response = client.get("/api/my-files/status", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in ("status_counts", "problem_files", "recent_files", "has_problems", "timestamp"):
        assert key in body, f"missing key {key!r}"
    counts = body["status_counts"]
    for key in ("total", "pending", "processing", "completed", "error"):
        assert key in counts
    assert counts["completed"] >= 1
    assert counts["total"] >= 1


def test_status_recent_includes_fresh_file(client, user_token_headers, normal_user, db_session):
    """A file uploaded within 24h appears in recent_files."""
    mf = _make_file(db_session, normal_user, upload_age_hours=1.0, filename="recent.wav")
    response = client.get("/api/my-files/status", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    recent_uuids = {f["uuid"] for f in response.json()["recent_files"]["files"]}
    assert str(mf.uuid) in recent_uuids


def test_status_error_file_is_problem(client, user_token_headers, normal_user, db_session):
    """An error-status file is surfaced under problem_files and flips has_problems."""
    mf = _make_file(db_session, normal_user, file_status="error", filename="failed.wav")
    response = client.get("/api/my-files/status", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    problem_uuids = {f["uuid"] for f in body["problem_files"]["files"]}
    assert str(mf.uuid) in problem_uuids
    assert body["has_problems"] is True


def test_status_scoped_to_user(client, other_user_auth_headers, normal_user, db_session):
    """One user's files never appear in another user's status counts/listing."""
    mf = _make_file(db_session, normal_user, file_status="error", filename="theirs.wav")
    response = client.get("/api/my-files/status", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK
    problem_uuids = {f["uuid"] for f in response.json()["problem_files"]["files"]}
    assert str(mf.uuid) not in problem_uuids


# ---------------------------------------------------------------------------
# GET /my-files/{uuid}/status
# ---------------------------------------------------------------------------


def test_file_detailed_status_owner(client, user_token_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user)
    response = client.get(f"/api/my-files/{mf.uuid}/status", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["file"]["uuid"] == str(mf.uuid)
    assert "task_summary" in body
    assert "can_retry" in body
    assert "suggestions" in body


def test_file_detailed_status_other_user_403(
    client, other_user_auth_headers, normal_user, db_session
):
    mf = _make_file(db_session, normal_user)
    response = client.get(f"/api/my-files/{mf.uuid}/status", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_file_detailed_status_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/my-files/{uuid.uuid4()}/status", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


# ---------------------------------------------------------------------------
# POST /my-files/{uuid}/retry
# ---------------------------------------------------------------------------


def test_retry_completed_file_400(client, user_token_headers, normal_user, db_session):
    """A completed file cannot be retried."""
    mf = _make_file(db_session, normal_user, file_status="completed")
    response = client.post(f"/api/my-files/{mf.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Cannot retry file in" in response.json()["detail"]


def test_retry_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    mf = _make_file(db_session, normal_user, file_status="error")
    response = client.post(f"/api/my-files/{mf.uuid}/retry", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_retry_nonexistent_404(client, user_token_headers):
    response = client.post(f"/api/my-files/{uuid.uuid4()}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /my-files/request-recovery
# ---------------------------------------------------------------------------


def test_request_recovery_dispatches(client, user_token_headers):
    response = client.post("/api/my-files/request-recovery", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is True
    assert "user_id" in body


def test_request_recovery_unauthorized(client):
    response = client.post("/api/my-files/request-recovery")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
