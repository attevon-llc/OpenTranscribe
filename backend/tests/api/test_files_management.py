"""Characterization tests for the file-management endpoints.

Covers ``files/management.py`` (mounted under ``/api/files``):

- ``GET    /api/files/{uuid}/status-detail``
- ``POST   /api/files/{uuid}/cancel``         (processing cancel, not upload cancel)
- ``POST   /api/files/{uuid}/retry``
- ``POST   /api/files/{uuid}/recover``
- ``DELETE /api/files/{uuid}/force``          (admin only)
- ``GET    /api/files/management/stuck``
- ``POST   /api/files/management/bulk-action``
- ``POST   /api/files/management/cleanup-orphaned`` (admin only; recovers STUCK
  files despite the path — real orphan cleanup is ``POST /admin/data-integrity``)

Celery dispatch is stubbed (``SKIP_CELERY=True``), so the *task-firing* branches
take their "test mode" paths and we assert DB/status transitions and the
response envelope rather than task side effects. Rows are created on the
savepoint-isolated session and roll back at teardown.

Notable behavior pinned here: ``bulk-action`` is resilient — a per-file
authorization failure becomes ``{"success": false, "error": "HTTP_ERROR"}`` in
the result list, NOT a top-level 403 (the overall request is still 200).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.media import MediaFile


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "mgmt_test.wav",
        "title": "mgmt_test",
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
# GET /api/files/{uuid}/status-detail
# ---------------------------------------------------------------------------


def test_status_detail_unauthorized(client):
    response = client.get(f"/api/files/{uuid.uuid4()}/status-detail")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_status_detail_owner_200(client, user_token_headers, normal_user, db_session):
    """The owner sees the detailed status envelope for a completed file."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.get(f"/api/files/{media_file.uuid}/status-detail", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["file_uuid"] == str(media_file.uuid)
    # NOTE: status-detail serializes status via str(enum) → the Python repr
    # "FileStatus.COMPLETED", NOT the bare "completed" the file-detail endpoint
    # returns. Pinned as-is (characterization); a later refactor must preserve it.
    assert body["status"] == "FileStatus.COMPLETED"
    # Completed file with no active task is deletable, not cancellable/retryable.
    assert body["can_delete"] is True
    assert body["can_cancel"] is False
    assert body["can_retry"] is False
    assert isinstance(body["actions_available"], list)


def test_status_detail_error_file_can_retry(client, user_token_headers, normal_user, db_session):
    """An ERROR file reports can_retry=True and a 'retry' action."""
    media_file = _make_file(db_session, normal_user, file_status="error")
    response = client.get(f"/api/files/{media_file.uuid}/status-detail", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["can_retry"] is True
    assert "retry" in body["actions_available"]


def test_status_detail_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/status-detail", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_status_detail_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/status-detail", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_status_detail_max_retries_reflects_the_admin_ceiling(
    client, user_token_headers, normal_user, db_session
):
    """`max_retries` (and `is_retryable`/`can_retry` derived from it) must report the
    admin-tunable SystemSettings ceiling, not `MediaFile.max_retries` — a column
    nothing ever writes, so it always read back the ORM default of 3 regardless of
    what an admin configured. That made this endpoint disagree with what
    `/retry` (both of them) actually enforces.
    """
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=1)
    media_file = _make_file(db_session, normal_user, file_status="error", retry_count=1)

    response = client.get(f"/api/files/{media_file.uuid}/status-detail", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["max_retries"] == 1
    # retry_count (1) has reached the lowered ceiling (1): the file must NOT be
    # reported as retryable, even though `can_retry` (status-only) says "retry" is a
    # legal action to attempt.
    assert "This file has reached maximum retry attempts" in " ".join(body["recommendations"])
    assert "This file can be retried for processing." not in body["recommendations"]


def test_status_detail_max_retries_below_ceiling_is_retryable(
    client, user_token_headers, normal_user, db_session
):
    """Control: below the (raised) admin ceiling, status-detail reports retryable."""
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=5)
    media_file = _make_file(db_session, normal_user, file_status="error", retry_count=1)

    response = client.get(f"/api/files/{media_file.uuid}/status-detail", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["max_retries"] == 5
    assert "This file can be retried for processing." in body["recommendations"]


# ---------------------------------------------------------------------------
# POST /api/files/{uuid}/cancel  (cancel ACTIVE PROCESSING)
# ---------------------------------------------------------------------------


def test_cancel_processing_not_processing_400(client, user_token_headers, normal_user, db_session):
    """Cancelling a non-processing file is a 400 with the canonical message."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.post(f"/api/files/{media_file.uuid}/cancel", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "File is not currently processing"


def test_cancel_processing_no_active_task_400(client, user_token_headers, normal_user, db_session):
    """A PROCESSING file with no active_task_id is a distinct 400."""
    media_file = _make_file(db_session, normal_user, file_status="processing", active_task_id=None)
    response = client.post(f"/api/files/{media_file.uuid}/cancel", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "No active task found for this file"


def test_cancel_processing_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user, file_status="processing")
    response = client.post(f"/api/files/{media_file.uuid}/cancel", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


# ---------------------------------------------------------------------------
# POST /api/files/{uuid}/retry
# ---------------------------------------------------------------------------


def test_retry_wrong_status_400(client, user_token_headers, normal_user, db_session):
    """Retrying a completed file (not in ERROR/CANCELLED/ORPHANED) is a 400."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "File cannot be retried in current status" in response.json()["detail"]


def test_retry_error_file_test_mode(client, user_token_headers, normal_user, db_session):
    """Retrying an ERROR file succeeds; under SKIP_CELERY it takes the test-mode
    path (no task) and returns the prepared-message envelope."""
    media_file = _make_file(db_session, normal_user, file_status="error")
    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["file_id"] == str(media_file.uuid)
    assert "retry" in body["message"].lower()


def test_retry_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user, file_status="error")
    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_retry_honours_admin_ceiling_even_reading_the_stale_column(
    client, user_token_headers, normal_user, db_session
):
    """A3: /retry used to read MediaFile.max_retries (never written anywhere, so
    always the ORM default of 3) instead of the admin-tunable system setting that
    /reprocess honours. Set the admin ceiling to 1 and a file already at 2
    retries must be refused, even though the stale column would still say "3"."""
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=1)
    media_file = _make_file(db_session, normal_user, file_status="error", retry_count=2)
    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "maximum retry attempts" in response.json()["detail"]


def test_retry_below_admin_ceiling_still_succeeds(
    client, user_token_headers, normal_user, db_session
):
    """Control: below the (lowered) ceiling, retry still works."""
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=5)
    media_file = _make_file(db_session, normal_user, file_status="error", retry_count=1)
    response = client.post(f"/api/files/{media_file.uuid}/retry", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK


def test_retry_reset_retry_count_requires_admin(
    client, user_token_headers, normal_user, db_session
):
    """A3: reset_retry_count used to skip the ceiling check entirely for ANY
    caller, including a non-admin file owner. It must now require admin."""
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=1)
    media_file = _make_file(db_session, normal_user, file_status="error", retry_count=5)
    response = client.post(
        f"/api/files/{media_file.uuid}/retry",
        headers=user_token_headers,
        params={"reset_retry_count": "true"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_retry_reset_retry_count_by_admin_succeeds(
    client, admin_token_headers, admin_user, db_session
):
    """Control: an admin resetting the retry count is still allowed."""
    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=1)
    media_file = _make_file(db_session, admin_user, file_status="error", retry_count=5)
    response = client.post(
        f"/api/files/{media_file.uuid}/retry",
        headers=admin_token_headers,
        params={"reset_retry_count": "true"},
    )
    assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# DELETE /api/files/{uuid}/force  (admin only)
# ---------------------------------------------------------------------------


def test_force_delete_non_admin_403(client, user_token_headers, normal_user, db_session):
    """A non-admin is rejected with the admin-only message BEFORE any lookup."""
    media_file = _make_file(db_session, normal_user)
    response = client.delete(f"/api/files/{media_file.uuid}/force", headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Only administrators can force delete files"


def test_force_delete_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.delete(f"/api/files/{media_file.uuid}/force")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_force_delete_admin_200(client, admin_token_headers, normal_user, db_session):
    """An admin force-deletes any user's file and gets the success envelope."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.delete(f"/api/files/{media_file.uuid}/force", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["file_uuid"] == str(media_file.uuid)


# ---------------------------------------------------------------------------
# GET /api/files/management/stuck
# ---------------------------------------------------------------------------


def test_stuck_files_shape(client, user_token_headers):
    """The stuck-files endpoint returns the list envelope (usually empty)."""
    response = client.get("/api/files/management/stuck", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) >= {"stuck_files", "total_count", "threshold_hours"}
    assert isinstance(body["stuck_files"], list)


def test_stuck_files_unauthorized(client):
    response = client.get("/api/files/management/stuck")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# POST /api/files/management/bulk-action
# ---------------------------------------------------------------------------


def test_bulk_action_unauthorized(client):
    response = client.post(
        "/api/files/management/bulk-action",
        json={"file_uuids": [str(uuid.uuid4())], "action": "delete"},
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_bulk_delete_owner_success(client, user_token_headers, normal_user, db_session):
    """Bulk-deleting an owned file returns a per-file success result."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(media_file.uuid)], "action": "delete"},
    )
    assert response.status_code == status.HTTP_200_OK
    results = response.json()
    assert len(results) == 1
    assert results[0]["file_uuid"] == str(media_file.uuid)
    assert results[0]["success"] is True


def test_bulk_action_other_user_file_is_soft_failure(
    client, other_user_auth_headers, normal_user, db_session
):
    """An authz failure on a single file does NOT 403 the whole request — it's
    a per-file soft failure (success=False, error=HTTP_ERROR) with the permission
    detail surfaced as the message."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.post(
        "/api/files/management/bulk-action",
        headers=other_user_auth_headers,
        json={"file_uuids": [str(media_file.uuid)], "action": "delete"},
    )
    assert response.status_code == status.HTTP_200_OK
    result = response.json()[0]
    assert result["success"] is False
    assert result["error"] == "HTTP_ERROR"
    assert result["message"] == "You do not have permission to access this file"


def test_bulk_action_unknown_action(client, user_token_headers, normal_user, db_session):
    """An unrecognized action yields a per-file UNKNOWN_ACTION soft failure."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(media_file.uuid)], "action": "frobnicate"},
    )
    assert response.status_code == status.HTTP_200_OK
    result = response.json()[0]
    assert result["success"] is False
    assert result["error"] == "UNKNOWN_ACTION"


def test_bulk_action_nonexistent_file_soft_failure(client, user_token_headers):
    """A nonexistent file in the batch is a per-file HTTP_ERROR (404 → soft)."""
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(uuid.uuid4())], "action": "delete"},
    )
    assert response.status_code == status.HTTP_200_OK
    result = response.json()[0]
    assert result["success"] is False
    assert result["error"] == "HTTP_ERROR"
    assert result["message"] == "File not found"


def test_bulk_action_missing_fields_422(client, user_token_headers):
    """file_uuids and action are required on BulkActionRequest."""
    response = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"file_uuids": [str(uuid.uuid4())]},  # missing action
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# POST /api/files/management/cleanup-orphaned  (admin only)
#
# The path says "orphaned"; the handler recovers STUCK files. The response used
# to advertise a `marked_orphaned` counter that was initialized and never
# incremented, so it reported 0 on every call in every deployment.
# ---------------------------------------------------------------------------


def test_cleanup_orphaned_non_admin_403(client, user_token_headers):
    response = client.post("/api/files/management/cleanup-orphaned", headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Only administrators can recover stuck files"


def test_cleanup_orphaned_admin_dry_run(client, admin_token_headers):
    """An admin dry-run returns the recovery summary without mutating anything."""
    response = client.post(
        "/api/files/management/cleanup-orphaned",
        headers=admin_token_headers,
        params={"dry_run": True},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["dry_run"] is True
    assert body["recovered"] == 0
    assert "stuck_files_found" in body


def test_cleanup_orphaned_response_promises_no_orphan_accounting(client, admin_token_headers):
    """The response carries only counters the handler actually maintains.

    ``marked_orphaned`` was dead: nothing in this handler marks a file orphaned
    (that is ``POST /admin/data-integrity``), so the key could only ever report 0
    and an operator reading it concluded there was nothing to clean up.
    """
    response = client.post(
        "/api/files/management/cleanup-orphaned",
        headers=admin_token_headers,
        params={"dry_run": True},
    )
    assert response.status_code == status.HTTP_200_OK
    assert set(response.json()) == {"stuck_files_found", "recovered", "errors", "dry_run"}


def test_cleanup_orphaned_recovers_a_stuck_file(
    client, admin_token_headers, db_session, admin_user
):
    """A non-dry run recovers what the stuck detector found and counts it.

    Pins the endpoint to its real job — ``check_for_stuck_files`` +
    ``recover_stuck_file`` — rather than to the orphan cleanup its name promises.
    """
    media_file = _make_file(db_session, admin_user, file_status="processing")

    def _one_stuck_file(db, stuck_threshold_hours=6):
        return [media_file.id]

    recovered: list[int] = []

    def _recover(db, file_id):
        recovered.append(file_id)
        return True

    with (
        patch("app.api.endpoints.files.management.check_for_stuck_files", _one_stuck_file),
        patch("app.api.endpoints.files.management.recover_stuck_file", _recover),
    ):
        response = client.post(
            "/api/files/management/cleanup-orphaned", headers=admin_token_headers
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["stuck_files_found"] == 1
    assert body["recovered"] == 1
    assert body["errors"] == []
    assert recovered == [media_file.id]


@pytest.mark.parametrize(
    "method,path_suffix",
    [
        ("post", "cancel"),
        ("post", "retry"),
        ("post", "recover"),
        ("get", "status-detail"),
    ],
)
def test_management_subresources_nonexistent_404(client, user_token_headers, method, path_suffix):
    """Every per-file management action 404s on an unknown UUID via the shared
    ownership helper ('File not found')."""
    fn = getattr(client, method)
    response = fn(f"/api/files/{uuid.uuid4()}/{path_suffix}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"
