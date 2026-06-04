"""Task endpoint tests.

These tests require MinIO/S3 storage (uploads create the tasks under test).
They activate automatically when the dev stack is reachable (see conftest.py
service auto-detection) and skip otherwise.
"""

import os

import pytest

# Skip all tests in this module if S3 is not available
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_S3", "True").lower() == "true",
    reason="S3/MinIO storage is disabled in test environment",
)


def test_list_tasks(client, user_token_headers):
    """Test listing user's tasks (paginated response)"""
    response = client.get("/api/tasks", headers=user_token_headers)
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert isinstance(data["items"], list)
    assert "total" in data


def test_list_tasks_unauthorized(client):
    """Test that unauthorized users cannot list tasks"""
    response = client.get("/api/tasks")
    assert response.status_code == 401  # Unauthorized


def test_get_task_not_found(client, user_token_headers):
    """A well-formed task ID for a non-existent file returns 404"""
    response = client.get("/api/tasks/task_999999999", headers=user_token_headers)
    assert response.status_code == 404  # Not found


def test_get_task_invalid_format(client, user_token_headers):
    """A malformed task ID is rejected with 400 (format is task_<media_file_id>)"""
    response = client.get("/api/tasks/not-a-task-id", headers=user_token_headers)
    assert response.status_code == 400


def test_get_task(client, user_token_headers, upload_test_file):
    """Test getting a specific task"""
    # First upload a file to create an associated task
    file_data = upload_test_file(user_token_headers, filename="task_test.wav")
    file_uuid = file_data.get("uuid") or file_data.get("id")

    # Get tasks to find the one associated with this file
    tasks_response = client.get("/api/tasks", headers=user_token_headers)
    tasks = tasks_response.json()["items"]

    # Find task associated with the file we just uploaded
    task = next(
        (
            t
            for t in tasks
            if t.get("media_file_uuid") == file_uuid or t.get("media_file_id") == file_uuid
        ),
        None,
    )

    if task:
        task_id = task.get("uuid") or task.get("id")
        # Now test getting the specific task
        response = client.get(f"/api/tasks/{task_id}", headers=user_token_headers)
        assert response.status_code == 200
        task_data = response.json()

        task_data_id = task_data.get("uuid") or task_data.get("id")
        assert task_data_id == task_id
    else:
        # Tasks are only auto-created when Celery dispatch runs (SKIP_CELERY=True here)
        pytest.skip("No task was created for the uploaded file")
