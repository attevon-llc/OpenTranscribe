"""Functional tests for the task-system health, progress and recovery routes.

Six routes ``scripts/audit-route-coverage.py`` listed as referenced by no test:
``GET /api/tasks/progress/active``, ``GET /api/tasks/system/health``, and the four
recovery verbs ``POST /api/tasks/system/{startup-recovery,recover-all-user-files,
recover-user-files/{user_uuid},recover-task/{task_id}}``.

**Nothing here runs a recovery sweep.** The four verbs each dispatch a Celery task
that walks every file (or every file of one user) and rewrites statuses; they are
replaced with small recording stand-ins, so what is asserted is the *dispatch
contract* — whether a task is queued at all, and with which user id. Running one for
real against the dev stack would rewrite live file states. ``recover-task`` is the
exception and is exercised end to end, because its whole effect lands on one ``task``
row created by the test and rolls back with the savepoint.

Two feeds that read alike and are not: ``GET /tasks/progress/active`` reads Redis
``ProgressTracker`` state (live, per-stage, lost on restart) while ``GET
/tasks/{task_id}`` reads the persisted ``task`` row. Conflating them is the #431
shape — a hardcoded mid-point progress that nothing noticed.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.media import Task as TaskModel
from tests.user_owned_rows import make_media_file

HEALTH = "/api/tasks/system/health"
PROGRESS = "/api/tasks/progress/active"
STARTUP_RECOVERY = "/api/tasks/system/startup-recovery"
RECOVER_ALL = "/api/tasks/system/recover-all-user-files"
RECOVER_USER = "/api/tasks/system/recover-user-files"
RECOVER_TASK = "/api/tasks/system/recover-task"

#: A syntactically valid uuid that is never inserted. A literal, not ``uuid4()``:
#: parametrize arguments are evaluated at import time and become part of the test id,
#: so a random one gives each xdist worker a different id and collection fails.
ABSENT_UUID = "00000000-0000-4000-8000-00000000beef"


class _RecordingTask:
    """Stand-in for a Celery recovery task: records dispatches, queues nothing.

    A plain object rather than a ``Mock`` so the assertions are ordinary equality
    checks on ``dispatches`` — real recorded behaviour, not mock bookkeeping.
    """

    def __init__(self, task_id: str = "stand-in-recovery-id") -> None:
        self.task_id = task_id
        self.dispatches: list[tuple] = []

    def delay(self, *args) -> SimpleNamespace:
        self.dispatches.append(args)
        return SimpleNamespace(id=self.task_id)


class _StandInRedis:
    """``scan_iter`` + ``get`` over a dict — what ``get_active_tasks`` uses."""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store = store or {}

    def scan_iter(self, match: str, count: int = 100):  # noqa: ARG002 - signature parity
        prefix, _, suffix = match.partition("*")
        for key in self.store:
            if key.startswith(prefix) and key.endswith(suffix):
                yield key

    def get(self, key: str) -> str | None:
        return self.store.get(key)


@pytest.fixture
def recovery_tasks():
    """Replace both recovery tasks with recorders, keyed by name."""
    startup = _RecordingTask("stand-in-startup-id")
    user_files = _RecordingTask("stand-in-user-files-id")
    with (
        patch("app.tasks.recovery.startup_recovery_task", startup),
        patch("app.tasks.recovery.recover_user_files_task", user_files),
    ):
        yield {"startup": startup, "user_files": user_files}


def _stuck_task(db, user, media_file, *, task_type: str = "summarization") -> TaskModel:
    """A task that ``identify_stuck_tasks`` must classify as stuck.

    Stale (``updated_at`` older than the 300 s staleness threshold) AND past its
    ``MAX_TASK_DURATIONS`` ceiling (``created_at`` two hours ago beats every entry).
    """
    now = datetime.now(UTC)
    task = TaskModel(
        id=f"pytest-stuck-{user.id}-{task_type}",
        user_id=user.id,
        media_file_id=media_file.id,
        task_type=task_type,
        status="in_progress",
        progress=0.4,
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
    )
    db.add(task)
    db.commit()
    return task


# ---------------------------------------------------------------------------
# GET /system/health
# ---------------------------------------------------------------------------
def test_health_reports_a_stuck_task_it_can_see(
    client, db_session, admin_token_headers, normal_user
):
    """A stale, over-duration task must appear in ``stuck_tasks.items``.

    The row is created by the test, so this does not depend on whatever the dev
    deployment happens to hold. Catches the detector's staleness/duration pair being
    inverted — the panel would show zero stuck tasks while transcriptions hung.
    """
    media_file = make_media_file(db_session, int(normal_user.id))
    task = _stuck_task(db_session, normal_user, media_file)

    response = client.get(HEALTH, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    ids = [item["id"] for item in body["stuck_tasks"]["items"]]
    assert task.id in ids
    # The count is the length of the list it ships, not a separate query.
    assert body["stuck_tasks"]["count"] == len(body["stuck_tasks"]["items"])


def test_health_carries_both_count_blocks_and_a_timestamp(client, admin_token_headers):
    """The report's shape: the admin panel indexes every one of these keys."""
    response = client.get(HEALTH, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {
        "task_counts",
        "file_counts",
        "stuck_tasks",
        "inconsistent_files",
        "timestamp",
    }
    assert set(body["task_counts"]) == {
        "pending",
        "in_progress",
        "completed",
        "failed",
        "total",
    }
    assert set(body["file_counts"]) == {"pending", "processing", "completed", "error", "total"}
    assert body["inconsistent_files"]["count"] == len(body["inconsistent_files"]["items"])


def test_health_is_refused_for_a_plain_user(client, user_token_headers):
    """It reports deployment-wide counts and other accounts' filenames."""
    response = client.get(HEALTH, headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_health_requires_authentication(client):
    assert client.get(HEALTH).status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /progress/active — the Redis feed, scoped by user
# ---------------------------------------------------------------------------
def test_progress_returns_only_the_callers_running_states(
    client, user_token_headers, normal_user, other_user
):
    """Running states for this user only — not completed ones, not another account's.

    The key pattern is ``task_progress:*:{user_id}``, so an over-broad glob would
    leak another user's task progress into this response. Redis is substituted; the
    scoping and the status filter under test are the handler's, not the store's.
    """
    store = {
        f"task_progress:reindex:{normal_user.id}": json.dumps(
            {"task_type": "reindex", "user_id": normal_user.id, "total": 10, "status": "running"}
        ),
        f"task_progress:autolabel:{normal_user.id}": json.dumps(
            {"task_type": "autolabel", "user_id": normal_user.id, "total": 3, "status": "completed"}
        ),
        f"task_progress:reindex:{other_user.id}": json.dumps(
            {"task_type": "reindex", "user_id": other_user.id, "total": 99, "status": "running"}
        ),
    }
    with patch("app.services.progress_tracker.get_redis", return_value=_StandInRedis(store)):
        response = client.get(PROGRESS, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [entry["task_type"] for entry in body] == ["reindex"]
    assert body[0]["user_id"] == normal_user.id
    assert body[0]["total"] == 10


def test_progress_is_an_empty_list_with_nothing_tracked(client, user_token_headers):
    """The SPA maps over the result on every poll; ``null`` would break the bars."""
    with patch("app.services.progress_tracker.get_redis", return_value=_StandInRedis()):
        response = client.get(PROGRESS, headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []


def test_progress_requires_authentication(client):
    assert client.get(PROGRESS).status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# The three sweep verbs — dispatch contract only
# ---------------------------------------------------------------------------
def test_startup_recovery_queues_the_recovery_task(client, admin_token_headers, recovery_tasks):
    """Queued in a background task, so the response must not wait on the sweep."""
    response = client.post(STARTUP_RECOVERY, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["success"] is True
    assert recovery_tasks["startup"].dispatches == [()]


def test_recover_all_user_files_dispatches_with_no_user_id(
    client, admin_token_headers, recovery_tasks
):
    """No argument means "every user" — the argument list IS the scope here.

    Passing an id by mistake would silently narrow a deployment-wide recovery to one
    account, and the 200 body would look identical.
    """
    response = client.post(RECOVER_ALL, headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["success"] is True
    assert recovery_tasks["user_files"].dispatches == [()]


def test_recover_user_files_dispatches_the_resolved_internal_id(
    client, admin_token_headers, normal_user, recovery_tasks
):
    """The path carries a uuid; the task must receive the internal integer id.

    Handing the uuid straight through would recover nobody's files while still
    answering 200 — the failure would only ever appear in a worker log.
    """
    response = client.post(f"{RECOVER_USER}/{normal_user.uuid}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is True
    assert body["user_uuid"] == str(normal_user.uuid)
    assert recovery_tasks["user_files"].dispatches == [(normal_user.id,)]


def test_recover_user_files_for_an_unknown_user_is_404_and_dispatches_nothing(
    client, admin_token_headers, recovery_tasks
):
    response = client.post(f"{RECOVER_USER}/{ABSENT_UUID}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert recovery_tasks["user_files"].dispatches == []


@pytest.mark.parametrize(
    "path",
    [
        STARTUP_RECOVERY,
        RECOVER_ALL,
        f"{RECOVER_USER}/{ABSENT_UUID}",
        f"{RECOVER_TASK}/pytest-nonexistent-task",
    ],
)
def test_every_recovery_verb_is_refused_for_a_plain_user(
    client, user_token_headers, recovery_tasks, path
):
    """Recovery rewrites other people's file states — admin only, no exceptions."""
    response = client.post(path, headers=user_token_headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert recovery_tasks["user_files"].dispatches == []
    assert recovery_tasks["startup"].dispatches == []


@pytest.mark.parametrize(
    "path",
    [
        STARTUP_RECOVERY,
        RECOVER_ALL,
        f"{RECOVER_USER}/{ABSENT_UUID}",
        f"{RECOVER_TASK}/pytest-nonexistent-task",
    ],
)
def test_every_recovery_verb_requires_authentication(client, recovery_tasks, path):
    response = client.post(path)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert recovery_tasks["user_files"].dispatches == []
    assert recovery_tasks["startup"].dispatches == []


# ---------------------------------------------------------------------------
# POST /system/recover-task/{task_id} — exercised for real on one owned row
# ---------------------------------------------------------------------------
def test_recovering_a_stuck_task_marks_it_failed(
    client, db_session, admin_token_headers, normal_user
):
    """Recovery means "stop pretending it is running": the row becomes ``failed``.

    Asserted on the row, not on the response flag — a handler that returned
    ``success: true`` and left the task ``in_progress`` would keep the file wedged
    forever, which is the state this endpoint exists to clear.
    """
    media_file = make_media_file(db_session, int(normal_user.id))
    task = _stuck_task(db_session, normal_user, media_file)

    response = client.post(f"{RECOVER_TASK}/{task.id}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is True
    assert body["task_id"] == task.id
    # A non-transcription task has nothing to re-queue.
    assert body["retry_scheduled"] is False
    db_session.expire_all()
    recovered = db_session.query(TaskModel).filter(TaskModel.id == task.id).one()
    assert recovered.status == "failed"
    assert recovered.error_message == "Task recovered after being stuck in processing"


def test_recovering_a_stuck_transcription_requeues_the_pipeline(
    client, db_session, admin_token_headers, normal_user
):
    """Only a transcription is retried, and it is retried by file uuid.

    The dispatcher is a stand-in — a real call queues GPU transcription work on the
    dev stack. Catches the retry branch firing for every task type (which would
    re-transcribe a file whose summarization merely timed out) and catches the
    internal id being passed where the uuid belongs.
    """
    media_file = make_media_file(db_session, int(normal_user.id))
    task = _stuck_task(db_session, normal_user, media_file, task_type="transcription")
    dispatched: list[str] = []

    def _record(*, file_uuid: str) -> str:
        dispatched.append(file_uuid)
        return "stand-in-pipeline-id"

    with patch("app.tasks.transcription.dispatch_transcription_pipeline", _record):
        response = client.post(f"{RECOVER_TASK}/{task.id}", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["retry_scheduled"] is True
    assert dispatched == [str(media_file.uuid)]


def test_recovering_an_unknown_task_is_404(client, admin_token_headers):
    response = client.post(f"{RECOVER_TASK}/pytest-no-such-task", headers=admin_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
