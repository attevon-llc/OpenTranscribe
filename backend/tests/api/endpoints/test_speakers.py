"""Speaker endpoint tests.

These tests require a media file to create speakers, which depends on MinIO/S3 storage.
They are marked as skipped by default in the test environment.
"""

import os

import pytest

# Skip all tests in this module if S3 is not available
# Speakers are associated with media files which require S3 storage
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_S3", "True").lower() == "true",
    reason="S3/MinIO storage is disabled - speakers require media files",
)


@pytest.fixture
def test_media_file(user_token_headers, upload_test_file):
    """Create a test media file that we can add speakers to."""
    data = upload_test_file(user_token_headers, filename="speaker_test.wav")
    return data.get("uuid") or data.get("id")


def test_list_speakers(client, user_token_headers):
    """Test listing all speakers for the user"""
    response = client.get("/api/speakers", headers=user_token_headers)
    assert response.status_code == 200
    data = response.json()
    # Response can be a list or paginated object depending on view
    assert isinstance(data, (list, dict))


def test_list_speakers_unauthorized(client):
    """Test that unauthorized users cannot list speakers"""
    response = client.get("/api/speakers")
    assert response.status_code == 401  # Unauthorized


def test_create_speaker(client, user_token_headers, test_media_file, db_session):
    """Test creating a new speaker"""
    speaker_data = {"name": "Test Speaker"}
    response = client.post(
        f"/api/speakers?media_file_uuid={test_media_file}",
        headers=user_token_headers,
        json=speaker_data,
    )
    assert response.status_code == 200, f"Create speaker failed: {response.json()}"
    speaker = response.json()
    assert "uuid" in speaker
    assert speaker.get("name") == "Test Speaker" or speaker.get("display_name") == "Test Speaker"


def test_get_speaker(client, user_token_headers, test_media_file, db_session):
    """Test getting a specific speaker"""
    # First create a speaker
    speaker_data = {"name": "Speaker for Get Test"}
    create_response = client.post(
        f"/api/speakers?media_file_uuid={test_media_file}",
        headers=user_token_headers,
        json=speaker_data,
    )
    assert create_response.status_code == 200, f"Create speaker failed: {create_response.json()}"
    speaker_uuid = create_response.json()["uuid"]

    # Now get the speaker
    response = client.get(f"/api/speakers/{speaker_uuid}", headers=user_token_headers)
    assert response.status_code == 200
    speaker = response.json()
    assert speaker["uuid"] == speaker_uuid


def test_update_speaker(client, user_token_headers, test_media_file, db_session):
    """Test updating a speaker"""
    # First create a speaker
    speaker_data = {"name": "Speaker for Update Test"}
    create_response = client.post(
        f"/api/speakers?media_file_uuid={test_media_file}",
        headers=user_token_headers,
        json=speaker_data,
    )
    assert create_response.status_code == 200, f"Create speaker failed: {create_response.json()}"
    speaker_uuid = create_response.json()["uuid"]

    # Now update the speaker
    update_data = {"name": "Updated Speaker Name"}
    response = client.put(
        f"/api/speakers/{speaker_uuid}", headers=user_token_headers, json=update_data
    )
    assert response.status_code == 200, f"Update speaker failed: {response.json()}"
    speaker = response.json()
    assert speaker["uuid"] == speaker_uuid


def test_delete_speaker(client, user_token_headers, test_media_file, db_session):
    """Test deleting a speaker"""
    # First create a speaker
    speaker_data = {"name": "Speaker for Delete Test"}
    create_response = client.post(
        f"/api/speakers?media_file_uuid={test_media_file}",
        headers=user_token_headers,
        json=speaker_data,
    )
    assert create_response.status_code == 200, f"Create speaker failed: {create_response.json()}"
    speaker_uuid = create_response.json()["uuid"]

    # Now delete the speaker
    response = client.delete(f"/api/speakers/{speaker_uuid}", headers=user_token_headers)
    assert response.status_code == 204  # No content
