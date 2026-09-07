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

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.services.task_recovery_service import TaskRecoveryService


@pytest.fixture(autouse=True)
def _patch_task_recovery_session_scope(monkeypatch, db_session):
    """``retry_file_processing`` dispatches through
    ``task_recovery_service.schedule_file_retry``, which opens its OWN session via
    ``TaskRecoveryService._session_scope`` (a fresh ``SessionLocal()``) rather than the
    request's ``get_db``-injected one. That is a different connection than the test's
    savepoint-isolated ``db_session``, so it cannot see a ``MediaFile`` this test created
    — "File N not found for retry" — even though the row is plainly there. Same failure
    mode, same fix, as ``test_chat_endpoints.py`` patching
    ``app.db.session_utils.session_scope``: reuse the test session instead of opening a
    real one. ``staticmethod(...)`` is required — a bare function assigned onto the class
    would bind ``self`` as its first (and only) positional argument.

    A *second* real session sits one hop further down the same call chain:
    ``schedule_file_retry`` dispatches into
    ``tasks/transcription/dispatch.py::dispatch_transcription_pipeline``, which opens its
    own session via ``with session_scope() as db:``. That module did
    ``from app.db.session_utils import session_scope`` at import time, which binds the
    name into `dispatch`'s own namespace — patching
    ``app.db.session_utils.session_scope`` afterwards does not reach a reference already
    bound elsewhere, so the patch target has to be where the name is *used*.
    """

    @contextlib.contextmanager
    def _fake_scope():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(TaskRecoveryService, "_session_scope", staticmethod(_fake_scope))
    monkeypatch.setattr("app.tasks.transcription.dispatch.session_scope", _fake_scope)


def _make_file(
    db_session,
    owner,
    *,
    file_status: str = "completed",
    upload_age_hours: float = 0.0,
    filename: str = "myfile.wav",
    retry_count: int = 0,
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
        upload_time=datetime.now(UTC) - timedelta(hours=upload_age_hours),
        retry_count=retry_count,
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


def test_retry_error_file_succeeds_below_ceiling(
    client, user_token_headers, normal_user, db_session
):
    """Control: retrying an ERROR file below the retry ceiling still works — this is
    the success path the ceiling test below needs a working control for."""
    mf = _make_file(db_session, normal_user, file_status="error", retry_count=1)
    response = client.post(f"/api/my-files/{mf.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["file_id"] == str(mf.uuid)
    assert body["new_status"] == "pending"


def test_retry_increments_retry_count(client, user_token_headers, normal_user, db_session):
    """A dispatched retry must count against the ceiling it was just checked against.

    Before this fix, `schedule_file_retry` dispatched the task but nothing on this
    route ever wrote `retry_count`, so the same file could be retried indefinitely at
    the 5-minute cooldown's rate regardless of any admin-configured ceiling.
    """
    mf = _make_file(db_session, normal_user, file_status="error", retry_count=1)
    response = client.post(f"/api/my-files/{mf.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK

    db_session.refresh(mf)
    assert mf.retry_count == 2


def test_retry_honours_admin_ceiling(client, user_token_headers, normal_user, db_session):
    """The live route (`POST /my-files/{uuid}/retry`, the one `UserFileStatus.svelte`
    actually calls) must refuse a retry once the admin-tunable ceiling is reached —
    not just its unreachable sibling `POST /files/{uuid}/retry`. Before this fix, this
    handler had no ceiling check at all: it called `schedule_file_retry` unconditionally
    and never wrote `retry_count`, so an admin lowering the ceiling to stop a
    runaway-cost retry loop did not stop anything reachable from the product's own
    retry button.
    """
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=1)
    mf = _make_file(db_session, normal_user, file_status="error", retry_count=2)
    response = client.post(f"/api/my-files/{mf.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "maximum retry attempts" in response.json()["detail"]

    # And the refusal must be a genuine refusal to dispatch, not merely a rejected
    # response with the task fired anyway.
    db_session.refresh(mf)
    assert mf.retry_count == 2
    assert mf.status == "error"


def test_retry_below_admin_ceiling_still_succeeds(
    client, user_token_headers, normal_user, db_session
):
    """Control for the ceiling test above: below the (lowered) ceiling, retry works."""
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=5)
    mf = _make_file(db_session, normal_user, file_status="error", retry_count=1)
    response = client.post(f"/api/my-files/{mf.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK


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
