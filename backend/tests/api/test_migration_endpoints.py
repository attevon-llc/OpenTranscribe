"""Characterization tests for the three admin migration endpoint modules:

- ``embedding_migration.py``         → ``/api/embeddings/migration/*``
- ``speaker_attribute_migration.py`` → ``/api/speaker-attributes/migration/*``
- ``combined_speaker_migration.py``  → ``/api/speakers/combined-migration/*``

All routes are guarded by ``get_current_active_superuser``, which requires the
``super_admin`` role (role is the authorization source of truth; ``is_superuser``
is its derived mirror). Non-super_admin → 403 "Not enough permissions -
super_admin required"; the ``super_admin_user`` fixture passes, a plain ``admin``
does not.

Celery dispatch is no-opped by the conftest ``_skip_celery_dispatch`` fixture:
``task.delay(...)`` returns a fake ``AsyncResult`` with ``id == "test-task-id"``.
The ``/start`` tests assert that fake envelope is surfaced.

Under tests Redis is unreachable (``SKIP_REDIS``; ``MigrationProgressService``
degrades to ``redis_client is None`` → ``is_running() == False`` and
``get_status()`` returns the default not-running dict), so the dispatch path is
deterministic and ``/stop`` / ``DELETE /progress`` report the not-running paths.

``combined_speaker_migration.py`` recently adopted ``ErrorHandler``; these
tests characterize its committed behavior (the error helper only fires on an
internal exception, which the happy/auth paths below do not trigger).
"""

from __future__ import annotations

import pytest
from fastapi import status

FAKE_TASK_ID = "test-task-id"  # from conftest _skip_celery_dispatch

EMBEDDING = "/api/embeddings/migration"
ATTRIBUTE = "/api/speaker-attributes/migration"
COMBINED = "/api/speakers/combined-migration"


# ---------------------------------------------------------------------------
# Superuser auth gate — shared by all three modules
# ---------------------------------------------------------------------------

# (method, path) tuples covering one representative route per module's verbs.
_GATED_GET = [
    f"{EMBEDDING}/status",
    f"{EMBEDDING}/progress",
    f"{EMBEDDING}/mode",
    f"{ATTRIBUTE}/status",
    f"{COMBINED}/status",
]
_GATED_POST = [
    f"{EMBEDDING}/start",
    f"{EMBEDDING}/stop",
    f"{EMBEDDING}/finalize",
    f"{EMBEDDING}/retry-failed",
    f"{EMBEDDING}/force-complete",
    f"{ATTRIBUTE}/start",
    f"{ATTRIBUTE}/stop",
    f"{COMBINED}/start",
    f"{COMBINED}/stop",
]
_GATED_DELETE = [
    f"{EMBEDDING}/progress",
    f"{ATTRIBUTE}/progress",
    f"{COMBINED}/progress",
]


@pytest.mark.parametrize("path", _GATED_GET)
def test_get_routes_unauthorized(client, path):
    assert client.get(path).status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.parametrize("path", _GATED_POST)
def test_post_routes_unauthorized(client, path):
    assert client.post(path).status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.parametrize("path", _GATED_DELETE)
def test_delete_routes_unauthorized(client, path):
    assert client.delete(path).status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.parametrize("path", _GATED_GET)
def test_get_routes_non_superuser_forbidden(client, user_token_headers, path):
    response = client.get(path, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not enough permissions - super_admin required"


@pytest.mark.parametrize("path", _GATED_POST)
def test_post_routes_non_superuser_forbidden(client, user_token_headers, path):
    response = client.post(path, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not enough permissions - super_admin required"


@pytest.mark.parametrize("path", _GATED_DELETE)
def test_delete_routes_non_superuser_forbidden(client, user_token_headers, path):
    response = client.delete(path, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not enough permissions - super_admin required"


# ---------------------------------------------------------------------------
# embedding_migration.py
# ---------------------------------------------------------------------------


def test_embedding_status_admin_ok(client, super_admin_token_headers):
    """Status returns 200 with mode/progress fields (OpenSearch degrades to v4)."""
    response = client.get(f"{EMBEDDING}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "progress" in body
    assert "mode_info" in body
    assert "stalled" in body


def test_embedding_progress_admin_ok(client, super_admin_token_headers):
    """Progress returns the not-running default envelope under SKIP_REDIS."""
    response = client.get(f"{EMBEDDING}/progress", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["running"] is False
    for key in ("total_files", "processed_files", "failed_files"):
        assert key in body


def test_embedding_mode_admin_ok(client, super_admin_token_headers):
    """The /mode route returns mode-info (defaults to v4 when OpenSearch is down)."""
    response = client.get(f"{EMBEDDING}/mode", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "mode" in body
    assert "dimension" in body


def test_embedding_start_without_force_skipped(client, super_admin_token_headers):
    """Without ``force`` and already-v4 mode, /start is a no-op skip (not a dispatch)."""
    response = client.post(f"{EMBEDDING}/start", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "skipped"


def test_embedding_start_force_dispatches_fake_task(client, super_admin_token_headers):
    """``force=true`` bypasses the v4 skip and dispatches → fake test-task-id."""
    response = client.post(f"{EMBEDDING}/start?force=true", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "started"
    assert body["task_id"] == FAKE_TASK_ID
    assert body["force"] is True


def test_embedding_stop_not_running(client, super_admin_token_headers):
    """Stop with no migration running reports not_running."""
    response = client.post(f"{EMBEDDING}/stop", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "not_running"


def test_embedding_finalize_dispatches_fake_task(client, super_admin_token_headers):
    """Finalize (no running migration) dispatches the swap task → fake id."""
    response = client.post(f"{EMBEDDING}/finalize", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "started"
    assert body["task_id"] == FAKE_TASK_ID


def test_embedding_clear_progress_when_not_running(client, super_admin_token_headers):
    """DELETE /progress clears when idle (Redis unavailable → clear_status False)."""
    response = client.delete(f"{EMBEDDING}/progress", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    # Redis is down in tests → clear_status() returns False → error envelope.
    assert response.json()["status"] in ("cleared", "error")


# ---------------------------------------------------------------------------
# speaker_attribute_migration.py
# ---------------------------------------------------------------------------


def test_attribute_status_admin_ok(client, super_admin_token_headers):
    """Attribute status returns file counts + progress (DB-backed counts)."""
    response = client.get(f"{ATTRIBUTE}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "total_files" in body
    assert "pending_files" in body
    assert "progress" in body


def test_attribute_start_dispatches_fake_task(client, super_admin_token_headers):
    """Start dispatches the attribute migration task → fake id."""
    response = client.post(f"{ATTRIBUTE}/start", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "started"
    assert body["task_id"] == FAKE_TASK_ID


def test_attribute_stop_not_running(client, super_admin_token_headers):
    response = client.post(f"{ATTRIBUTE}/stop", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "not_running"


def test_attribute_clear_progress(client, super_admin_token_headers):
    response = client.delete(f"{ATTRIBUTE}/progress", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] in ("cleared", "error")


# ---------------------------------------------------------------------------
# combined_speaker_migration.py  (ErrorHandler adoption — characterize committed behavior)
# ---------------------------------------------------------------------------


def test_combined_status_admin_ok(client, super_admin_token_headers):
    """Status wraps the progress dict under a ``progress`` key."""
    response = client.get(f"{COMBINED}/status", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert "progress" in body
    assert body["progress"]["running"] is False


def test_combined_start_dispatches_fake_task(client, super_admin_token_headers):
    """Start dispatches the combined migration task → fake id."""
    response = client.post(f"{COMBINED}/start", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "started"
    assert body["task_id"] == FAKE_TASK_ID


def test_combined_stop_not_running(client, super_admin_token_headers):
    response = client.post(f"{COMBINED}/stop", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "not_running"


def test_combined_clear_progress(client, super_admin_token_headers):
    response = client.delete(f"{COMBINED}/progress", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] in ("cleared", "error")
