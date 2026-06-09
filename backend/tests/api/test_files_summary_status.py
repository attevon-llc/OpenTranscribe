"""Characterization tests for ``files/summary_status.py``.

Covers ``GET /api/files/{uuid}/summary-status`` and
``POST /api/files/{uuid}/retry-summary``. LLM availability is checked through
``is_llm_available`` which degrades to False when no provider is configured
(the default test environment), so the read endpoint is deterministic and the
retry endpoint exercises the 503 "LLM not available" branch. Savepoint rows roll
back at teardown.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "summary_test.wav",
        "title": "summary_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": file_status,
        "summary_status": "pending",
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
# GET /api/files/{uuid}/summary-status
# ---------------------------------------------------------------------------


def test_summary_status_owner(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/summary-status", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in (
        "file_id",
        "summary_status",
        "summary_exists",
        "llm_available",
        "can_retry",
        "transcription_status",
        "filename",
        "can_generate",
    ):
        assert key in body
    assert body["file_id"] == str(media_file.uuid)
    assert body["summary_status"] == "pending"
    assert body["summary_exists"] is False


def test_summary_status_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/summary-status")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_summary_status_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/summary-status", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_summary_status_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/summary-status", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_summary_status_malformed_uuid_400(client, user_token_headers):
    response = client.get("/api/files/not-a-uuid/summary-status", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# POST /api/files/{uuid}/retry-summary
# ---------------------------------------------------------------------------


def test_retry_summary_disabled_400(client, user_token_headers, normal_user, db_session):
    """A file with summaries disabled cannot be retried."""
    media_file = _make_file(db_session, normal_user, summary_status="disabled")
    response = client.post(
        f"/api/files/{media_file.uuid}/retry-summary", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "summary generation disabled" in response.json()["detail"]


def test_retry_summary_bad_status_400(client, user_token_headers, normal_user, db_session):
    """Only failed/pending statuses are retryable."""
    media_file = _make_file(db_session, normal_user, summary_status="completed")
    response = client.post(
        f"/api/files/{media_file.uuid}/retry-summary", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Cannot retry summary with status" in response.json()["detail"]


def test_retry_summary_transcription_not_complete_400(
    client, user_token_headers, normal_user, db_session
):
    media_file = _make_file(
        db_session, normal_user, file_status="processing", summary_status="pending"
    )
    response = client.post(
        f"/api/files/{media_file.uuid}/retry-summary", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == ("Cannot generate summary until transcription is completed")


def test_retry_summary_no_llm_503(client, user_token_headers, normal_user, db_session):
    """With no LLM provider configured, a valid retry request returns 503."""
    media_file = _make_file(db_session, normal_user, summary_status="pending")
    response = client.post(
        f"/api/files/{media_file.uuid}/retry-summary", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "LLM service is not available. Please try again later."


def test_retry_summary_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.post(f"/api/files/{media_file.uuid}/retry-summary")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_retry_summary_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/retry-summary", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_retry_summary_nonexistent_404(client, user_token_headers):
    response = client.post(f"/api/files/{uuid.uuid4()}/retry-summary", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"
