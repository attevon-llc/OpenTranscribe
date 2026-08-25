"""Task endpoint tests.

Only ``test_get_task_for_uploaded_file`` needs MinIO/S3 — it uploads a real file
so the round trip covers the storage path. The other tests exercise the endpoint
contract with no storage at all, so gating the whole module on ``SKIP_S3``
(as it did until #431) skipped four tests that had no such dependency.

Deeper behavioural coverage of the response fields lives in ``test_tasks_read.py``.
"""

import os
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

#: Applied per test, not module-wide: only the upload path needs object storage.
requires_s3 = pytest.mark.skipif(
    os.environ.get("SKIP_S3", "True").lower() == "true",
    reason="S3/MinIO storage is disabled in test environment",
)


def _make_file(db_session, owner, *, file_status: str = "error", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "tasks_retry_test.wav",
        "title": "tasks_retry_test",
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


def test_list_tasks(client, user_token_headers):
    """Test listing user's tasks (paginated response)"""
    response = client.get("/api/tasks", headers=user_token_headers)
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert isinstance(data["items"], list)
    assert "total" in data


def test_list_tasks_unauthorized(client):
    """Test that unauthorized users cannot list tasks"""
    response = client.get("/api/tasks")
    assert response.status_code == 401  # Unauthorized


def test_get_task_not_found(client, user_token_headers):
    """A well-formed task ID for a non-existent file returns 404"""
    response = client.get("/api/tasks/task_999999999", headers=user_token_headers)
    assert response.status_code == 404  # Not found


def test_get_task_invalid_format(client, user_token_headers):
    """A malformed task ID is rejected with 400 (format is task_<media_file_id>)"""
    response = client.get("/api/tasks/not-a-task-id", headers=user_token_headers)
    assert response.status_code == 400


@requires_s3
def test_get_task_for_uploaded_file(client, user_token_headers, upload_test_file):
    """An uploaded file is listed as a task and readable by that task's id.

    This used to guard every assertion behind ``if task:`` with an
    ``else: pytest.skip()``, looking for a task row that only Celery dispatch
    creates — and the autouse fixture patches ``apply_async`` out, so the row
    never existed and the only test of ``GET /tasks/{task_id}`` skipped on every
    run (#431). A freshly uploaded file has no task row yet, which is exactly
    what the legacy ``task_<media_file_id>`` id form is for, so the assertions
    here are unconditional.
    """
    file_data = upload_test_file(user_token_headers, filename="task_test.wav")
    file_uuid = file_data["uuid"]

    tasks_response = client.get("/api/tasks", headers=user_token_headers)
    assert tasks_response.status_code == 200
    tasks = tasks_response.json()["items"]

    matching = [t for t in tasks if t["media_file_id"] == file_uuid]
    assert matching, f"uploaded file {file_uuid} is not listed as a task"
    task_id = matching[0]["id"]

    response = client.get(f"/api/tasks/{task_id}", headers=user_token_headers)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/tasks/retry/{file_uuid}
#
# A third, independent retry entry point besides files/management.py's
# /{uuid}/retry and user_files.py's /my-files/{uuid}/retry. It used to dispatch
# unconditionally with no admin-tunable ceiling check and no retry_count
# tracking at all — an admin lowering the ceiling to stop a runaway retry loop
# had no effect on a user retrying through this route.
# ---------------------------------------------------------------------------


def test_tasks_retry_honours_admin_ceiling(client, user_token_headers, normal_user, db_session):
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=1)
    media_file = _make_file(db_session, normal_user, file_status="error", retry_count=2)
    response = client.post(f"/api/tasks/retry/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "maximum retry attempts" in response.json()["detail"]


def test_tasks_retry_below_ceiling_increments_retry_count(
    client, user_token_headers, normal_user, db_session
):
    """Control: below the ceiling, retry succeeds and retry_count is tracked —
    it used to never be written by this endpoint at all."""
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=5)
    media_file = _make_file(db_session, normal_user, file_status="error", retry_count=1)
    response = client.post(f"/api/tasks/retry/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    db_session.refresh(media_file)
    assert media_file.retry_count == 2


def test_tasks_retry_admin_bypasses_ceiling(client, admin_token_headers, admin_user, db_session):
    """Control: an admin is not subject to the ceiling here either, matching the
    other two retry endpoints."""
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=1)
    media_file = _make_file(db_session, admin_user, file_status="error", retry_count=5)
    response = client.post(f"/api/tasks/retry/{media_file.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
