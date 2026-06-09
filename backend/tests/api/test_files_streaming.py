"""Characterization tests for the files streaming surfaces.

Covers the thumbnail streaming endpoint (``files/streaming.py`` via
``GET /api/files/{uuid}/thumbnail``), the presigned ``/stream-url`` and
``/prepare-download`` routes, and the SSE ``download-stream`` contract wired in
``files/__init__.py``.

These pin the CURRENT observable behavior (status code + ``detail`` + key
headers) so later refactors can't change the API by accident. Rows are created
on the savepoint-isolated ``db_session`` and roll back at teardown — dev data is
never mutated. No real MinIO object is required: the thumbnail/stream-url code
take a ``SKIP_S3`` mock branch, and the SSE test only reads headers (it never
consumes the stream).
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

_MINIO_REACHABLE = os.environ.get("SKIP_S3", "False").lower() != "true"


def _make_file(db_session, owner, **overrides) -> MediaFile:
    """Create and persist a MediaFile row owned by ``owner`` on the test session."""
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "stream_test.wav",
        "title": "stream_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
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
# GET /api/files/{uuid}/thumbnail
# ---------------------------------------------------------------------------


def test_thumbnail_unauthenticated_private_file_401(client, normal_user, db_session):
    """A private file's thumbnail requires authentication."""
    media_file = _make_file(db_session, normal_user, thumbnail_path="thumbs/x.webp")
    response = client.get(f"/api/files/{media_file.uuid}/thumbnail")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Authentication required" in response.json()["detail"]


@pytest.mark.skipif(
    _MINIO_REACHABLE, reason="MinIO reachable — real storage path covered elsewhere"
)
def test_thumbnail_owner_mock_branch(client, user_token_headers, normal_user, db_session):
    """When object storage is unavailable (SKIP_S3=True) the owner gets mock bytes.

    The dev stack has MinIO reachable so conftest sets SKIP_S3=False and the real
    download path runs (covered by ``test_thumbnail_missing_in_storage_404``). This
    pins the offline/mock contract — the security headers must remain present.
    """
    media_file = _make_file(db_session, normal_user, thumbnail_path="thumbs/x.webp")
    response = client.get(f"/api/files/{media_file.uuid}/thumbnail", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.skipif(not _MINIO_REACHABLE, reason="MinIO not reachable — download path is mocked")
def test_thumbnail_missing_in_storage_404(client, user_token_headers, normal_user, db_session):
    """A thumbnail_path that points at no real object → 404 from storage.

    Requires MinIO (the real download path). The DB row references a thumbnail that
    was never uploaded, so the object fetch raises FileNotFoundError → 404.
    """
    media_file = _make_file(db_session, normal_user, thumbnail_path="thumbs/missing.webp")
    response = client.get(f"/api/files/{media_file.uuid}/thumbnail", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Thumbnail not found in storage"


def test_thumbnail_missing_thumbnail_404(client, user_token_headers, normal_user, db_session):
    """A file with no thumbnail_path returns 404 with the canonical detail."""
    media_file = _make_file(db_session, normal_user, thumbnail_path=None)
    response = client.get(f"/api/files/{media_file.uuid}/thumbnail", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Thumbnail not available for this file"


def test_thumbnail_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    """A non-owner cannot read a private file's thumbnail."""
    media_file = _make_file(db_session, normal_user, thumbnail_path="thumbs/x.webp")
    response = client.get(
        f"/api/files/{media_file.uuid}/thumbnail", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Access denied to this file"


def test_thumbnail_public_file_no_auth_passes_authz(client, normal_user, db_session):
    """A public file's thumbnail bypasses the auth gate (no 401).

    With MinIO reachable the missing object then yields 404 from storage; the point
    pinned here is that the public-file branch never demands authentication.
    """
    media_file = _make_file(
        db_session, normal_user, is_public=True, thumbnail_path="thumbs/missing.webp"
    )
    response = client.get(f"/api/files/{media_file.uuid}/thumbnail")
    assert response.status_code != status.HTTP_401_UNAUTHORIZED
    if _MINIO_REACHABLE:
        assert response.status_code == status.HTTP_404_NOT_FOUND
    else:
        assert response.status_code == status.HTTP_200_OK
        assert "public" in response.headers["Cache-Control"]


def test_thumbnail_nonexistent_404(client, user_token_headers):
    """An unknown UUID is a 404 'File not found' (via get_file_by_uuid)."""
    response = client.get(f"/api/files/{uuid.uuid4()}/thumbnail", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_thumbnail_malformed_uuid_400(client, user_token_headers):
    """``file_uuid: str`` → bad UUID rejected by get_by_uuid with 400."""
    response = client.get("/api/files/not-a-uuid/thumbnail", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"].startswith("Invalid UUID format")


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/stream-url  (presigned, SKIP_S3 mock branch)
# ---------------------------------------------------------------------------


def test_stream_url_owner_envelope(client, user_token_headers, normal_user, db_session):
    """The owner gets a presigned-URL envelope for the video stream.

    With MinIO reachable (dev stack) the URL is a real presigned ``/s3/...`` link;
    under SKIP_S3 it's the mock ``/api/files/{uuid}/video`` path. Either way the
    envelope keys and the non-URL fields are stable, which is what we pin.
    """
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/stream-url",
        headers=user_token_headers,
        params={"media_type": "video"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) >= {"url", "expires_in", "content_type", "is_public"}
    assert isinstance(body["url"], str) and body["url"]
    assert body["content_type"] == "audio/wav"
    assert body["is_public"] is False
    if not _MINIO_REACHABLE:
        assert body["url"] == f"/api/files/{media_file.uuid}/video"


def test_stream_url_invalid_media_type_400(client, user_token_headers, normal_user, db_session):
    """An unsupported media_type is a 400 before any storage lookup."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/stream-url",
        headers=user_token_headers,
        params={"media_type": "bogus"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid media_type" in response.json()["detail"]


def test_stream_url_thumbnail_without_thumbnail_404(
    client, user_token_headers, normal_user, db_session
):
    """Requesting a thumbnail stream-url when none exists is a 404."""
    media_file = _make_file(db_session, normal_user, thumbnail_path=None)
    response = client.get(
        f"/api/files/{media_file.uuid}/stream-url",
        headers=user_token_headers,
        params={"media_type": "thumbnail"},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Thumbnail not found for this file"


def test_stream_url_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/stream-url")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_stream_url_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/stream-url", headers=other_user_auth_headers
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_stream_url_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/stream-url", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


# ---------------------------------------------------------------------------
# POST /api/files/{uuid}/prepare-download
# ---------------------------------------------------------------------------


def test_prepare_download_invalid_mode_400(client, user_token_headers, normal_user, db_session):
    """An unknown download mode is rejected with 400 before any work."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/prepare-download",
        headers=user_token_headers,
        params={"mode": "totally_invalid"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Invalid download mode: totally_invalid"


def test_prepare_download_missing_mode_422(client, user_token_headers, normal_user, db_session):
    """``mode`` is a required query parameter."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/prepare-download", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_prepare_download_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/prepare-download", params={"mode": "audio_original"}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_prepare_download_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    """Mode is validated before the ownership lookup, so use a valid mode here."""
    media_file = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/files/{media_file.uuid}/prepare-download",
        headers=other_user_auth_headers,
        params={"mode": "audio_original"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_prepare_download_nonexistent_404(client, user_token_headers):
    response = client.post(
        f"/api/files/{uuid.uuid4()}/prepare-download",
        headers=user_token_headers,
        params={"mode": "audio_original"},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/download-stream  (SSE)
# ---------------------------------------------------------------------------


def test_download_stream_invalid_mode_400(client, user_token_headers, normal_user, db_session):
    """The SSE endpoint validates the mode before opening the stream."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/download-stream",
        headers=user_token_headers,
        params={"mode": "nope"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Invalid download mode: nope"


def test_download_stream_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}/download-stream", params={"mode": "audio_original"}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_download_stream_content_type(client, user_token_headers, normal_user, db_session):
    """For an audio_original passthrough the SSE response advertises event-stream.

    We assert only the response contract (status + content type + SSE headers)
    without consuming the (potentially long-lived) stream. The audio_original
    mode hits the ready/passthrough path, which under SKIP_S3 resolves a presigned
    URL synchronously, so the first ``ready`` event is produced immediately and the
    generator returns — but TestClient buffers the body, so we keep the assertion to
    the headers to avoid coupling to MinIO availability.
    """
    media_file = _make_file(db_session, normal_user, filename="clip.wav")
    with client.stream(
        "GET",
        f"/api/files/{media_file.uuid}/download-stream",
        headers=user_token_headers,
        params={"mode": "audio_original"},
    ) as response:
        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["X-Accel-Buffering"] == "no"
