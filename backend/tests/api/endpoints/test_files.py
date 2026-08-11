"""Media file endpoint tests.

These tests require MinIO/S3 storage which is disabled in the test environment.
They are marked as skipped by default. Run with actual storage services for full testing.
"""

import io
import os

import pytest

# Skip all tests in this module if S3 is not available
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_S3", "True").lower() == "true",
    reason="S3/MinIO storage is disabled in test environment",
)


def test_list_files(client, user_token_headers):
    """Test listing user's files"""
    response = client.get("/api/files", headers=user_token_headers)
    assert response.status_code == 200
    data = response.json()
    assert "items" in data  # Paginated response
    assert isinstance(data["items"], list)


def test_list_files_unauthorized(client):
    """Test that unauthorized users cannot list files"""
    response = client.get("/api/files")
    assert response.status_code == 401  # Unauthorized


def test_upload_file(user_token_headers, upload_test_file):
    """Test uploading a file (real WAV — passes magic-byte validation)"""
    file_data = upload_test_file(user_token_headers, filename="test_audio.wav")

    # Basic schema validation - uses uuid not id
    assert "uuid" in file_data
    assert "filename" in file_data
    assert file_data["filename"] == "test_audio.wav"


def test_upload_file_unauthorized(client, sample_wav_bytes):
    """Test that unauthorized users cannot upload files"""
    files = {"file": ("test_audio.wav", io.BytesIO(sample_wav_bytes), "audio/wav")}
    response = client.post("/api/files", files=files)
    assert response.status_code == 401  # Unauthorized


def test_get_file_not_found(client, user_token_headers):
    """Test getting a non-existent file"""
    import uuid as uuid_module

    fake_uuid = str(uuid_module.uuid4())
    response = client.get(f"/api/files/{fake_uuid}", headers=user_token_headers)
    assert response.status_code == 404  # Not found
