"""Characterization tests for ``files/waveform.py``.

Covers the per-file waveform/peaks read endpoints, the per-file and bulk
generate endpoints (Celery dispatch no-op'd by the conftest fixture — these
assert the response envelope + admin gating, never worker side effects), and the
admin-only status endpoint.

Savepoint-isolated rows roll back at teardown. The ``/waveform`` GET serves
cached data without touching object storage when a valid cache blob is seeded, so
the happy-path test needs no MinIO object.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import MediaFile


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "wave_test.wav",
        "title": "wave_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": file_status,
        "duration": 5.0,
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _cached_blob(samples: int = 1000) -> dict:
    """A waveform cache entry shaped like the live generator's output."""
    return {
        f"waveform_{samples}": {
            "waveform": [0, 128, 255, 64],
            "duration": 5.0,
            "sample_rate": 16000,
            "extracted_samples": 4,
            "expected_duration": 5.0,
        }
    }


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/waveform  (cached path — no storage access)
# ---------------------------------------------------------------------------


def test_waveform_cached(client, user_token_headers, normal_user, db_session):
    """A seeded cache blob is returned without re-extracting from storage."""
    media_file = _make_file(db_session, normal_user, waveform_data=_cached_blob())
    response = client.get(
        f"/api/files/{media_file.uuid}/waveform",
        headers=user_token_headers,
        params={"samples": 1000},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["cached"] is True
    assert body["file_id"] == str(media_file.uuid)
    assert body["waveform"] == [0, 128, 255, 64]


def test_waveform_not_completed_400(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user, file_status="processing")
    response = client.get(f"/api/files/{media_file.uuid}/waveform", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "not ready for waveform generation" in response.json()["detail"]


def test_waveform_non_media_400(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(
        db_session, normal_user, content_type="application/pdf", waveform_data=_cached_blob()
    )
    response = client.get(f"/api/files/{media_file.uuid}/waveform", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == (
        "File must be audio or video format for waveform generation"
    )


def test_waveform_samples_out_of_range_422(client, user_token_headers, normal_user, db_session):
    """``samples`` is constrained ge=100, le=10000."""
    media_file = _make_file(db_session, normal_user, waveform_data=_cached_blob())
    response = client.get(
        f"/api/files/{media_file.uuid}/waveform",
        headers=user_token_headers,
        params={"samples": 50},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_waveform_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/waveform")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_waveform_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user, waveform_data=_cached_blob())
    response = client.get(f"/api/files/{media_file.uuid}/waveform", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_waveform_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/waveform", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_waveform_malformed_uuid_400(client, user_token_headers):
    response = client.get("/api/files/not-a-uuid/waveform", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/waveform/peaks  (validation + authz)
# ---------------------------------------------------------------------------


def test_waveform_peaks_not_completed_400(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user, file_status="pending")
    response = client.get(
        f"/api/files/{media_file.uuid}/waveform/peaks", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


def test_waveform_peaks_dimensions_422(client, user_token_headers, normal_user, db_session):
    """``height`` is constrained ge=50, le=500."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/waveform/peaks",
        headers=user_token_headers,
        params={"height": 10},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_waveform_peaks_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/waveform/peaks", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# POST /api/files/{uuid}/waveform/generate  (Celery no-op'd)
# ---------------------------------------------------------------------------


def test_waveform_generate_owner(client, user_token_headers, normal_user, db_session):
    """The generate endpoint returns a success envelope (no real task runs)."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/waveform/generate", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is True
    assert body["file_id"] == str(media_file.uuid)
    assert body["force_regenerate"] is False


def test_waveform_generate_non_media_400(client, user_token_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user, content_type="text/plain")
    response = client.post(
        f"/api/files/{media_file.uuid}/waveform/generate", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "File must be audio or video format"


def test_waveform_generate_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/waveform/generate", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_waveform_generate_nonexistent_404(client, user_token_headers):
    response = client.post(
        f"/api/files/{uuid.uuid4()}/waveform/generate", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /api/files/waveforms/generate  +  GET /api/files/waveforms/status (admin)
# ---------------------------------------------------------------------------


def test_bulk_waveform_generate_non_admin_403(client, user_token_headers):
    response = client.post("/api/files/waveforms/generate", headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Only admin users can trigger bulk waveform generation"


def test_bulk_waveform_generate_admin_200(client, admin_token_headers):
    response = client.post("/api/files/waveforms/generate", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["success"] is True


def test_waveform_status_non_admin_403(client, user_token_headers):
    response = client.get("/api/files/waveforms/status", headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Only admin users can view waveform status"


def test_waveform_status_admin_200(client, admin_token_headers):
    response = client.get("/api/files/waveforms/status", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in (
        "total_media_files",
        "completed_media_files",
        "files_with_waveforms",
        "files_without_waveforms",
        "waveform_coverage_percentage",
    ):
        assert key in body


def test_waveform_status_unauthorized(client):
    response = client.get("/api/files/waveforms/status")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
