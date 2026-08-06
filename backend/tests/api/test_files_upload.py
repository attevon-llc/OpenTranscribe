"""Characterization tests for the direct (legacy) multipart upload route.

Covers ``POST /api/files`` wired through ``files/upload.py``
(``process_file_upload`` → ``validate_file_type`` → magic-byte validation →
storage → pipeline dispatch).

Validation ordering (locked here): content-type check (``validate_file_type``)
runs BEFORE any bytes are read, so a wrong MIME type is a fast 400 that never
touches storage. The magic-byte check runs on the first chunk. The
duplicate-by-hash short-circuit (``X-File-Hash`` header) is a 409.

The happy path actually uploads to MinIO when reachable (``SKIP_S3=False``,
auto-detected); it uses the ``upload_test_file`` fixture which deletes the
created object via the API on teardown so the dev bucket stays clean. The
rejection paths (wrong type, bad magic bytes, duplicate) never reach storage,
so they run ungated.
"""

from __future__ import annotations

import io
import os
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile

S3_LIVE = os.environ.get("SKIP_S3", "True").lower() != "true"


def _multipart(content: bytes, filename: str, content_type: str) -> dict:
    return {"file": (filename, io.BytesIO(content), content_type)}


# ---------------------------------------------------------------------------
# Happy path (MinIO-gated: actually stores bytes)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not S3_LIVE, reason="legacy upload writes to MinIO (SKIP_S3=False)")
def test_upload_wav_happy(client, user_token_headers, upload_test_file):
    """A small valid WAV uploads, returns the file record + X-File-ID header.

    Uses the shared fixture so the MinIO object is cleaned up afterward.
    """
    data = upload_test_file(user_token_headers, filename="legacy_upload.wav")
    assert data["uuid"]
    assert data["filename"] == "legacy_upload.wav"
    # Status is PENDING right after upload (pipeline dispatch is stubbed).
    assert data["status"] in ("pending", "processing")


@pytest.mark.skipif(not S3_LIVE, reason="legacy upload writes to MinIO (SKIP_S3=False)")
def test_upload_sets_file_id_header(client, user_token_headers, test_wav_bytes, db_session):
    """The response carries an X-File-ID header equal to the new file UUID."""
    response = client.post(
        "/api/files",
        headers=user_token_headers,
        files=_multipart(test_wav_bytes, "hdr.wav", "audio/wav"),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    file_uuid = response.json()["uuid"]
    assert response.headers.get("X-File-ID") == file_uuid
    # Clean up the stored object (row rolls back with the savepoint).
    client.delete(f"/api/files/{file_uuid}", headers=user_token_headers)


# ---------------------------------------------------------------------------
# Auth + validation (no storage interaction — ungated)
# ---------------------------------------------------------------------------


def test_upload_unauthorized(client, test_wav_bytes):
    response = client.post("/api/files", files=_multipart(test_wav_bytes, "x.wav", "audio/wav"))
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_upload_no_file_422(client, user_token_headers):
    """The file field is required → FastAPI 422 when omitted."""
    response = client.post("/api/files", headers=user_token_headers)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_upload_wrong_content_type_400(client, user_token_headers):
    """A non audio/video MIME type is rejected up front (before any read)."""
    response = client.post(
        "/api/files",
        headers=user_token_headers,
        files=_multipart(b"hello world this is plain text", "notes.txt", "text/plain"),
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "File must be an audio or video format"


def test_upload_image_content_type_400(client, user_token_headers):
    """Images are also rejected by the audio/video gate."""
    response = client.post(
        "/api/files",
        headers=user_token_headers,
        files=_multipart(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "pic.png", "image/png"),
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "File must be an audio or video format"


def test_upload_magic_byte_mismatch_400(client, user_token_headers):
    """Bytes whose signature doesn't match the declared audio type are rejected
    by magic-byte validation. The surfaced detail is the user-friendly message
    (the 'unknown signature' wording stays in the server log only)."""
    garbage = b"NOTRIFFDATA" + b"\x01\x02\x03\x04" * 8  # >32 bytes, not a RIFF/WAV header
    response = client.post(
        "/api/files",
        headers=user_token_headers,
        files=_multipart(garbage, "fake.wav", "audio/wav"),
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    detail = response.json()["detail"]
    assert "doesn't appear to be a valid audio/video file" in detail


# ---------------------------------------------------------------------------
# Duplicate-by-hash short-circuit (X-File-Hash header → 409, no storage write)
# ---------------------------------------------------------------------------


def test_upload_duplicate_hash_409(client, user_token_headers, normal_user, db_session):
    """A direct upload whose X-File-Hash matches an existing uploaded file is a
    409 with the structured duplicate detail — before any bytes are stored.

    The prior file is seeded on the savepoint session (real storage_path, a
    non-failed status) so ``check_duplicate_by_fingerprint`` finds it.
    """
    digest = uuid.uuid4().hex
    file_uuid = str(uuid.uuid4())
    existing = MediaFile(
        uuid=file_uuid,
        filename="seed.wav",
        title="seed",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=normal_user.id,
        file_hash=digest,
    )
    db_session.add(existing)
    db_session.commit()

    headers = {**user_token_headers, "X-File-Hash": digest}
    response = client.post(
        "/api/files",
        headers=headers,
        files=_multipart(b"RIFF....WAVEfmt new bytes here padding padding", "dup.wav", "audio/wav"),
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    detail = response.json()["detail"]
    assert detail["message"] == "A file with this content already exists."
    assert detail["duplicate_file_uuid"] == str(existing.uuid)
