"""Comment endpoint tests.

These tests require MinIO/S3 storage which is disabled in the test environment.
They are marked as skipped by default. Run with actual storage services for full testing.
"""

import os

import pytest

# Skip all tests in this module if S3 is not available
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_S3", "True").lower() == "true",
    reason="S3/MinIO storage is disabled in test environment",
)


@pytest.fixture
def test_file_with_comment(user_token_headers, upload_test_file):
    """Create a test file that we can add comments to"""
    data = upload_test_file(user_token_headers, filename="comment_test.wav")
    # Return uuid if available, otherwise id
    return data.get("uuid") or data.get("id")


def test_list_comments(client, user_token_headers, test_file_with_comment):
    """Test listing comments for a file"""
    response = client.get(
        f"/api/comments?media_file_id={test_file_with_comment}",
        headers=user_token_headers,
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_create_comment(client, user_token_headers, test_file_with_comment, db_session):
    """Test creating a comment for a file"""
    comment_data = {
        "media_file_id": test_file_with_comment,
        "text": "This is a test comment",
        "timestamp": 30.5,  # Comment at 30.5 seconds in the audio
    }
    response = client.post("/api/comments", headers=user_token_headers, json=comment_data)
    assert response.status_code == 200, f"Create comment failed: {response.json()}"
    comment = response.json()
    comment_id = comment.get("uuid") or comment.get("id")
    assert comment_id is not None
    assert comment["text"] == "This is a test comment"
    assert comment["timestamp"] == 30.5


def test_get_comment(client, user_token_headers, test_file_with_comment, db_session):
    """Test getting a specific comment"""
    # First create a comment
    comment_data = {
        "media_file_id": test_file_with_comment,
        "text": "Comment for get test",
        "timestamp": 45.0,
    }
    create_response = client.post("/api/comments", headers=user_token_headers, json=comment_data)
    assert create_response.status_code == 200, f"Create comment failed: {create_response.json()}"
    comment_id = create_response.json().get("uuid") or create_response.json().get("id")

    # Now get the comment
    response = client.get(f"/api/comments/{comment_id}", headers=user_token_headers)
    assert response.status_code == 200
    comment = response.json()
    response_id = comment.get("uuid") or comment.get("id")
    assert response_id == comment_id
    assert comment["text"] == "Comment for get test"
    assert comment["timestamp"] == 45.0


def test_update_comment(client, user_token_headers, test_file_with_comment, db_session):
    """Test updating a comment"""
    # First create a comment
    comment_data = {
        "media_file_id": test_file_with_comment,
        "text": "Comment for update test",
        "timestamp": 60.0,
    }
    create_response = client.post("/api/comments", headers=user_token_headers, json=comment_data)
    assert create_response.status_code == 200, f"Create comment failed: {create_response.json()}"
    comment_id = create_response.json().get("uuid") or create_response.json().get("id")

    # Now update the comment
    update_data = {"text": "Updated comment text", "timestamp": 65.5}
    response = client.put(
        f"/api/comments/{comment_id}", headers=user_token_headers, json=update_data
    )
    assert response.status_code == 200, f"Update comment failed: {response.json()}"
    comment = response.json()
    response_id = comment.get("uuid") or comment.get("id")
    assert response_id == comment_id
    assert comment["text"] == "Updated comment text"
    assert comment["timestamp"] == 65.5


def test_delete_comment(client, user_token_headers, test_file_with_comment, db_session):
    """Test deleting a comment"""
    # First create a comment
    comment_data = {
        "media_file_id": test_file_with_comment,
        "text": "Comment for delete test",
        "timestamp": 75.0,
    }
    create_response = client.post("/api/comments", headers=user_token_headers, json=comment_data)
    assert create_response.status_code == 200, f"Create comment failed: {create_response.json()}"
    comment_id = create_response.json().get("uuid") or create_response.json().get("id")

    # Now delete the comment
    response = client.delete(f"/api/comments/{comment_id}", headers=user_token_headers)
    assert response.status_code == 204  # No content


def test_list_comments_unauthorized(client):
    """Test that unauthorized users cannot list comments"""
    response = client.get("/api/comments")
    assert response.status_code == 401  # Unauthorized


def test_create_comment_unauthorized(client):
    """Test that unauthorized users cannot create comments"""
    comment_data = {
        "media_file_id": "some-uuid",
        "text": "This should fail",
        "timestamp": 0.0,
    }
    response = client.post("/api/comments", json=comment_data)
    assert response.status_code == 401  # Unauthorized
