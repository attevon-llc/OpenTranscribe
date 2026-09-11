"""Prove the #859/#891 exception-echo fixes actually sanitize responses.

Every test here asserts THREE things: the status code, that a SENTINEL string
planted in the underlying (mocked) failure is ABSENT from the response body, and
that the sentinel IS present in the log output — a test that only checks the
status code would pass against the old, broken code too and proves nothing.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

SENTINEL = "SENTINEL-a3f9-/srv/internal/secret-path"


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "sanitize_test.wav",
        "title": "sanitize_test",
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


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/waveform/peaks — 500
# ---------------------------------------------------------------------------


def test_waveform_peaks_failure_does_not_echo_the_exception(
    client, user_token_headers, normal_user, db_session, monkeypatch, caplog
):
    media_file = _make_file(db_session, normal_user)

    def _raise(*args, **kwargs):
        raise RuntimeError(f"ffmpeg failed reading {SENTINEL}")

    monkeypatch.setattr("app.api.endpoints.files.waveform._extract_waveform_from_file", _raise)

    with caplog.at_level(logging.ERROR):
        response = client.get(
            f"/api/files/{media_file.uuid}/waveform/peaks", headers=user_token_headers
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    body_text = response.text
    assert SENTINEL not in body_text
    assert response.json()["detail"] == "Could not generate the waveform peaks."
    assert SENTINEL in caplog.text


# ---------------------------------------------------------------------------
# DELETE /api/tags/cleanup — 500
# ---------------------------------------------------------------------------


def test_tag_cleanup_failure_does_not_echo_the_exception(
    client, admin_token_headers, monkeypatch, caplog
):
    def _raise(*args, **kwargs):
        raise RuntimeError(f"could not run SQL against {SENTINEL}")

    monkeypatch.setattr("app.api.endpoints.tags.crud.cleanup_unreferenced_tags", _raise)

    with caplog.at_level(logging.ERROR):
        response = client.delete("/api/tags/cleanup", headers=admin_token_headers)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert SENTINEL not in response.text
    assert response.json()["detail"] == "Could not clean up unused tags."
    assert SENTINEL in caplog.text


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/thumbnail — 500
# ---------------------------------------------------------------------------


def test_thumbnail_failure_does_not_echo_the_exception(
    client, user_token_headers, normal_user, db_session, monkeypatch, caplog
):
    media_file = _make_file(db_session, normal_user, thumbnail_path="media/test/thumb.jpg")

    # SKIP_S3 auto-detects from the live dev stack's reachability; force it False
    # so the mock-content short-circuit in get_thumbnail_streaming_response is
    # bypassed and the real download_file()/exception path is exercised.
    monkeypatch.setenv("SKIP_S3", "False")

    def _raise(*args, **kwargs):
        raise RuntimeError(f"MinIO error at {SENTINEL}")

    monkeypatch.setattr("app.api.endpoints.files.streaming.download_file", _raise)

    with caplog.at_level(logging.ERROR):
        response = client.get(f"/api/files/{media_file.uuid}/thumbnail", headers=user_token_headers)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert SENTINEL not in response.text
    assert response.json()["detail"] == "Could not retrieve the thumbnail."
    assert SENTINEL in caplog.text


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/stream-url — 500
# ---------------------------------------------------------------------------


def test_stream_url_failure_does_not_echo_the_exception(
    client, user_token_headers, normal_user, db_session, monkeypatch, caplog
):
    media_file = _make_file(db_session, normal_user)

    # Same SKIP_S3 seam as the thumbnail test above — force real-storage-path
    # handling so the presign failure actually reaches the handler.
    monkeypatch.setenv("SKIP_S3", "False")

    def _raise(*args, **kwargs):
        raise RuntimeError(f"presign failed against internal host {SENTINEL}")

    monkeypatch.setattr("app.services.minio_service.get_file_url", _raise)

    with caplog.at_level(logging.ERROR):
        response = client.get(
            f"/api/files/{media_file.uuid}/stream-url",
            headers=user_token_headers,
            params={"media_type": "video"},
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert SENTINEL not in response.text
    assert response.json()["detail"] == "Could not generate a streaming URL for this file."
    assert SENTINEL in caplog.text


# ---------------------------------------------------------------------------
# POST /api/files/process-url — 400 that doesn't echo yt-dlp's raw output
# ---------------------------------------------------------------------------


def test_process_url_failure_does_not_echo_ytdlp_output(
    client, user_token_headers, monkeypatch, caplog
):
    def _raise(self, *args, **kwargs):
        raise RuntimeError(
            f"ERROR: [generic] ffmpeg config path leaked: {SENTINEL} --cookies /home/user/.cookies"
        )

    monkeypatch.setattr(
        "app.services.media_download_service.MediaDownloadService.extract_video_info", _raise
    )

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/files/process-url",
            headers=user_token_headers,
            json={"url": "https://example.com/watch?v=abc123"},
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert SENTINEL not in response.text
    assert response.json()["detail"] == "Could not read information for that media URL."
    assert SENTINEL in caplog.text


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
