"""Tests for the async bulk subtitle export endpoints.

POST /files/bulk-export/prepare validates the request, permission-filters the
UUIDs, and enqueues download.prepare_bulk_subtitles. The actual ZIP build +
presigned delivery happen on the worker / SSE stream (covered by the E2E suite
and the build_subtitle_archive unit tests).
"""

import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile


@pytest.fixture
def completed_file(db_session, sample_user):
    file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="meeting.mp4",
        storage_path="media/test/meeting.mp4",
        content_type="video/mp4",
        file_size=2048,
        user_id=sample_user.id,
        status="completed",
        is_public=False,
    )
    db_session.add(file)
    db_session.commit()
    db_session.refresh(file)
    return file


@pytest.fixture
def processing_file(db_session, sample_user):
    file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="pending.mp4",
        storage_path="media/test/pending.mp4",
        content_type="video/mp4",
        file_size=2048,
        user_id=sample_user.id,
        status="processing",
        is_public=False,
    )
    db_session.add(file)
    db_session.commit()
    db_session.refresh(file)
    return file


@pytest.fixture
def _no_enqueue(monkeypatch):
    """Stub the Celery dispatch so the happy path doesn't need a live broker."""
    captured = {}

    def fake_delay(**kwargs):
        captured.update(kwargs)
        return None

    from app.tasks import media_download

    monkeypatch.setattr(media_download.prepare_bulk_subtitles_task, "delay", fake_delay)
    return captured


class TestPrepareBulkExport:
    def test_requires_authentication(self, client):
        response = client.post("/api/files/bulk-export/prepare", json={"file_uuids": []})
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_empty_uuids_rejected(self, client, auth_headers):
        response = client.post(
            "/api/files/bulk-export/prepare",
            headers=auth_headers,
            json={"file_uuids": [], "subtitle_format": "srt"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_too_many_uuids_rejected(self, client, auth_headers):
        response = client.post(
            "/api/files/bulk-export/prepare",
            headers=auth_headers,
            json={"file_uuids": [str(uuid.uuid4()) for _ in range(101)]},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_format_rejected(self, client, auth_headers, completed_file):
        response = client.post(
            "/api/files/bulk-export/prepare",
            headers=auth_headers,
            json={"file_uuids": [str(completed_file.uuid)], "subtitle_format": "doc"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_no_accessible_completed_files_returns_404(self, client, auth_headers, processing_file):
        # Only a non-completed file -> nothing exportable.
        response = client.post(
            "/api/files/bulk-export/prepare",
            headers=auth_headers,
            json={"file_uuids": [str(processing_file.uuid)], "subtitle_format": "srt"},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_happy_path_enqueues_and_returns_job_id(
        self, client, auth_headers, completed_file, _no_enqueue
    ):
        response = client.post(
            "/api/files/bulk-export/prepare",
            headers=auth_headers,
            json={
                "file_uuids": [str(completed_file.uuid)],
                "subtitle_format": "srt",
                "include_speakers": True,
            },
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "processing"
        assert data["job_id"]

        # The worker receives permission-resolved (file_id, base_filename) specs.
        assert _no_enqueue["job_id"] == data["job_id"]
        assert _no_enqueue["subtitle_format"] == "srt"
        assert _no_enqueue["file_specs"] == [(int(completed_file.id), "meeting")]


class TestBulkExportStreamAuth:
    def test_stream_requires_authentication(self, client):
        response = client.get("/api/files/bulk-export-stream?job=abc123")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
