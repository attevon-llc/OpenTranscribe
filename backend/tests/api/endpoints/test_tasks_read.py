"""Tests for the task read endpoints (``GET /tasks`` and ``GET /tasks/{task_id}``).

These endpoints had no test at all, which is how a hardcoded ``progress = 0.5``
survived from #76 (2025-09-25) until the audit in #431: every ``PROCESSING`` file
reported the same fabricated mid-point, rendering as a permanently half-full
progress bar in ``TasksGrid.svelte``. The pipeline records real fractional
progress in the ``task`` table via ``app.utils.task_utils``; these tests pin the
endpoints to that recorded value.

Each test asserts a value the old implementation could not produce, so the suite
fails if the synthesis is ever reintroduced.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel


@pytest.fixture
def processing_file_with_task(db_session, normal_user):
    """A PROCESSING file whose task row records a progress no synthesis would emit."""
    celery_task_id = str(uuid.uuid4())
    media_file = MediaFile(
        user_id=normal_user.id,
        filename="progress_probe.mp4",
        storage_path=f"{normal_user.id}/progress_probe.mp4",
        file_size=2048,
        content_type="video/mp4",
        status=FileStatus.PROCESSING,
        active_task_id=celery_task_id,
    )
    db_session.add(media_file)
    db_session.flush()

    task = TaskModel(
        id=celery_task_id,
        user_id=normal_user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
        # 0.3 is deliberately not 0.0/0.5/1.0 — the three values the old
        # file-status synthesis could produce.
        progress=0.3,
    )
    db_session.add(task)
    db_session.commit()
    return media_file, task


def _find_task(payload: dict, task_id: str) -> dict:
    matches: list[dict] = [item for item in payload["items"] if item["id"] == task_id]
    assert matches, f"task {task_id} missing from {[i['id'] for i in payload['items']]}"
    return matches[0]


class TestListTasksUsesRealTaskRows:
    def test_progress_comes_from_the_task_row_not_the_file_status(
        self, client, user_token_headers, processing_file_with_task
    ):
        """The regression test: a PROCESSING file must report its recorded progress."""
        _, task = processing_file_with_task

        response = client.get("/api/tasks", headers=user_token_headers)

        assert response.status_code == 200
        item = _find_task(response.json(), task.id)
        assert item["progress"] == pytest.approx(0.3)

    def test_id_is_the_celery_task_id_so_recover_task_can_consume_it(
        self, client, user_token_headers, processing_file_with_task
    ):
        """``POST /tasks/system/recover-task/{id}`` queries ``task.id``.

        While this endpoint returned ``task_<media_file.id>``, listing a task and
        feeding its id to recovery could only 404.
        """
        _, task = processing_file_with_task

        response = client.get("/api/tasks", headers=user_token_headers)

        assert response.status_code == 200
        ids = [item["id"] for item in response.json()["items"]]
        assert task.id in ids
        assert not any(i.startswith("task_") for i in ids if i == task.id)

    def test_task_type_is_the_recorded_type_not_always_transcription(
        self, client, user_token_headers, db_session, normal_user
    ):
        """``?task_type=summarization`` could never match a hardcoded type."""
        celery_task_id = str(uuid.uuid4())
        media_file = MediaFile(
            user_id=normal_user.id,
            filename="summarized.mp4",
            storage_path=f"{normal_user.id}/summarized.mp4",
            file_size=1024,
            content_type="video/mp4",
            status=FileStatus.COMPLETED,
            active_task_id=celery_task_id,
        )
        db_session.add(media_file)
        db_session.flush()
        db_session.add(
            TaskModel(
                id=celery_task_id,
                user_id=normal_user.id,
                media_file_id=media_file.id,
                task_type="summarization",
                status="in_progress",
                progress=0.7,
            )
        )
        db_session.commit()

        response = client.get("/api/tasks", headers=user_token_headers)

        assert response.status_code == 200
        item = _find_task(response.json(), celery_task_id)
        assert item["task_type"] == "summarization"
        assert item["progress"] == pytest.approx(0.7)

    def test_error_message_is_the_files_real_error_not_a_literal(
        self, client, user_token_headers, db_session, normal_user
    ):
        """A file in ERROR with no task row must surface its own error text."""
        media_file = MediaFile(
            user_id=normal_user.id,
            filename="broken.mp4",
            storage_path=f"{normal_user.id}/broken.mp4",
            file_size=1024,
            content_type="video/mp4",
            status=FileStatus.ERROR,
            last_error_message="ffmpeg: moov atom not found",
        )
        db_session.add(media_file)
        db_session.commit()

        response = client.get("/api/tasks", headers=user_token_headers)

        assert response.status_code == 200
        item = _find_task(response.json(), f"task_{media_file.id}")
        assert item["error_message"] == "ffmpeg: moov atom not found"

    def test_processing_file_without_a_task_row_reports_unknown_progress(
        self, client, user_token_headers, db_session, normal_user
    ):
        """With nothing recorded, progress must not be invented."""
        media_file = MediaFile(
            user_id=normal_user.id,
            filename="orphan_processing.mp4",
            storage_path=f"{normal_user.id}/orphan_processing.mp4",
            file_size=1024,
            content_type="video/mp4",
            status=FileStatus.PROCESSING,
        )
        db_session.add(media_file)
        db_session.commit()

        response = client.get("/api/tasks", headers=user_token_headers)

        assert response.status_code == 200
        item = _find_task(response.json(), f"task_{media_file.id}")
        assert item["status"] == "in_progress"
        assert item["progress"] == 0.0


class TestListTasksReportsFailure:
    def test_a_failing_query_is_a_500_not_an_empty_page(
        self, client, user_token_headers, monkeypatch
    ):
        """An empty 200 is indistinguishable from "this user has no tasks".

        The handler used to swallow every exception into an empty successful
        page, so a broken query rendered as an empty task list with nothing
        reported anywhere.
        """
        import app.api.endpoints.tasks as tasks_module

        def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated query failure")

        monkeypatch.setattr(tasks_module, "_get_user_media_files", _boom)

        response = client.get("/api/tasks", headers=user_token_headers)

        assert response.status_code == 500


class TestGetTaskAcceptsBothIdForms:
    def test_real_celery_id_resolves_to_the_task_row(
        self, client, user_token_headers, processing_file_with_task
    ):
        media_file, task = processing_file_with_task

        response = client.get(f"/api/tasks/{task.id}", headers=user_token_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == task.id
        assert body["progress"] == pytest.approx(0.3)
        assert body["media_file_id"] == str(media_file.uuid)

    def test_legacy_id_form_still_resolves_and_reports_real_progress(
        self, client, user_token_headers, processing_file_with_task
    ):
        """Callers holding a ``task_<file_id>`` id keep working, with real progress."""
        media_file, _ = processing_file_with_task

        response = client.get(f"/api/tasks/task_{media_file.id}", headers=user_token_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == f"task_{media_file.id}"
        assert body["progress"] == pytest.approx(0.3)

    def test_malformed_id_is_rejected(self, client, user_token_headers):
        response = client.get("/api/tasks/not-a-task-id", headers=user_token_headers)
        assert response.status_code == 400

    def test_another_users_task_is_not_readable(
        self, client, user_token_headers, db_session, other_user
    ):
        """A real Celery id must not bypass the ownership check."""
        celery_task_id = str(uuid.uuid4())
        media_file = MediaFile(
            user_id=other_user.id,
            filename="not_yours.mp4",
            storage_path=f"{other_user.id}/not_yours.mp4",
            file_size=1024,
            content_type="video/mp4",
            status=FileStatus.PROCESSING,
            active_task_id=celery_task_id,
        )
        db_session.add(media_file)
        db_session.flush()
        db_session.add(
            TaskModel(
                id=celery_task_id,
                user_id=other_user.id,
                media_file_id=media_file.id,
                task_type="transcription",
                status="in_progress",
                progress=0.42,
            )
        )
        db_session.commit()

        response = client.get(f"/api/tasks/{celery_task_id}", headers=user_token_headers)

        assert response.status_code == 400
        assert "0.42" not in response.text
