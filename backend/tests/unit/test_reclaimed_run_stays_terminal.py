"""A reclaimed transcription run stays reclaimed (issue #1178).

Seen in a multi-worker load test: recovery reclaimed a transcription Task row 0.1 s after it
was created and dispatched a replacement, which completed normally. Two seconds later the
ORIGINAL run's preprocess stage wrote ``in_progress`` / 0.2 to the reclaimed row, and the row
stayed ``in_progress`` forever -- a permanent false "stuck run" for every metric and alert
that looks at the oldest active task.

Two defects, one test group each:

* **Lost update.** ``update_task_status`` wrote whatever status it was given. A write that
  makes a transcription row ACTIVE must only land while the row is still active.
* **Over-eager reclaim.** ``create_task_record`` commits the row with ``updated_at`` NULL and
  the queued marker is written only after further commits, so the orphan sweep's
  ``recover_lost_runs`` (``updated_at IS NULL OR updated_at < cutoff``, no lease, no marker)
  read a brand-new run as dead. A run is not eligible for reclaim until it is older than
  ``TRANSCRIPTION_RECLAIM_GRACE_SECONDS`` (never shorter than the heartbeat TTL).

Real rows on the savepoint-backed ``db_session``; Redis is the dict-backed liveness stand-in.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.task_config import TaskRecoveryConfig
from app.core.task_config import task_recovery_config
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services import transcription_retry
from app.services.task_recovery_service import TaskRecoveryService
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_task_status
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    return install_fake_redis(monkeypatch)


@contextmanager
def _bridged_scope(db_session):
    yield db_session
    db_session.commit()


@pytest.fixture
def policy_db(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.transcription_retry.session_scope", lambda: _bridged_scope(db_session)
    )
    return db_session


@pytest.fixture
def dispatched(monkeypatch) -> list[int]:
    """Record retry dispatches instead of publishing to a broker."""
    calls: list[int] = []

    def _fake(self, media_file_id, countdown=None):
        calls.append(media_file_id)
        return True

    monkeypatch.setattr(TaskRecoveryService, "schedule_file_retry", _fake)
    monkeypatch.setattr(
        "app.core.broker_orphans.discard_run_deliveries", lambda run_id, client=None: 0
    )
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_progress_notification",
        lambda *a, **k: None,
    )
    return calls


def _file(db, user) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        user_id=user.id,
        filename=f"rr-{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        file_size=1024,
        content_type="audio/wav",
        status=FileStatus.PROCESSING,
        retry_count=0,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _row(
    db,
    user,
    media_file,
    *,
    task_type: str = "transcription",
    status: str = "in_progress",
    created_ago: timedelta = timedelta(hours=1),
    updated_ago: timedelta | None = timedelta(minutes=20),
) -> Task:
    now = datetime.now(UTC)
    task = Task(
        id=f"rr-{uuid.uuid4()}",
        user_id=user.id,
        media_file_id=media_file.id,
        task_type=task_type,
        status=status,
        progress=0.05,
        created_at=now - created_ago,
        updated_at=None if updated_ago is None else now - updated_ago,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _reload(db, ident: str) -> Task:
    db.expire_all()
    task: Task = db.query(Task).filter(Task.id == ident).one()
    return task


# =============================================================================
# Lost update: a late write from a superseded run
# =============================================================================
def test_a_late_progress_write_does_not_resurrect_a_reclaimed_run(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    run = _row(db_session, normal_user, media_file)
    reclaimed_at = datetime.now(UTC)
    run.status = "failed"
    run.error_message = transcription_retry.WORKER_LOST_MESSAGE
    run.completed_at = reclaimed_at
    db_session.commit()

    # The original run's preprocess stage, still executing, reports progress.
    update_task_status(db_session, run.id, "in_progress", progress=0.2)

    stored = _reload(db_session, run.id)
    assert stored.status == "failed"
    assert stored.progress == pytest.approx(0.05)
    assert stored.completed_at is not None
    assert stored.error_message == transcription_retry.WORKER_LOST_MESSAGE


@pytest.mark.parametrize("terminal", ["completed", "failed", "skipped"])
def test_no_terminal_transcription_row_is_moved_back_to_active(db_session, normal_user, terminal):
    media_file = _file(db_session, normal_user)
    run = _row(db_session, normal_user, media_file, status=terminal)

    update_task_status(db_session, run.id, "pending")
    update_task_status(db_session, run.id, "in_progress", progress=0.9)

    assert _reload(db_session, run.id).status == terminal


def test_a_current_run_still_records_progress(db_session, normal_user):
    media_file = _file(db_session, normal_user)
    run = _row(db_session, normal_user, media_file)

    update_task_status(db_session, run.id, "in_progress", progress=0.2)

    stored = _reload(db_session, run.id)
    assert stored.status == "in_progress"
    assert stored.progress == pytest.approx(0.2)


def test_a_same_id_retry_of_another_task_type_may_reopen_its_row(db_session, normal_user):
    """Search indexing / speaker clustering mark their row failed, then ``self.retry()``
    under the SAME task id; the next attempt must still be able to report progress."""
    media_file = _file(db_session, normal_user)
    row = _row(db_session, normal_user, media_file, task_type="search_indexing", status="failed")

    update_task_status(db_session, row.id, "in_progress", progress=0.1)

    assert _reload(db_session, row.id).status == "in_progress"


# =============================================================================
# Over-eager reclaim: a run inside its grace window is not "dead"
# =============================================================================
def test_a_freshly_dispatched_run_is_not_reclaimed_inside_the_grace_window(
    policy_db, normal_user, fake_redis, dispatched
):
    """The exact window from the load test: row committed, ``updated_at`` still NULL, no
    queued marker written yet, no lease."""
    media_file = _file(policy_db, normal_user)
    run = _row(
        policy_db, normal_user, media_file, created_ago=timedelta(seconds=0), updated_ago=None
    )

    acted = transcription_retry.recover_lost_runs(exclude=set())

    assert acted == 0
    assert dispatched == []
    assert _reload(policy_db, run.id).status == "in_progress"


def test_recover_lost_run_refuses_a_run_inside_the_grace_window(
    policy_db, normal_user, fake_redis, dispatched
):
    """Every DEAD path (the orphan sweep and the health check) goes through this one call."""
    media_file = _file(policy_db, normal_user)
    run = _row(
        policy_db,
        normal_user,
        media_file,
        created_ago=timedelta(seconds=5),
        updated_ago=timedelta(seconds=5),
    )

    assert transcription_retry.recover_lost_run(policy_db, run) is None
    assert dispatched == []
    assert _reload(policy_db, run.id).status == "in_progress"


def test_a_run_past_the_grace_window_is_still_reclaimed(
    policy_db, normal_user, fake_redis, dispatched
):
    media_file = _file(policy_db, normal_user)
    run = _row(policy_db, normal_user, media_file)

    acted = transcription_retry.recover_lost_runs(exclude=set())

    assert acted == 1
    assert dispatched == [media_file.id]
    assert _reload(policy_db, run.id).status == "failed"


def test_the_grace_is_never_shorter_than_the_heartbeat_ttl(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_HEARTBEAT_TTL_SECONDS", "300")
    monkeypatch.setenv("TRANSCRIPTION_RECLAIM_GRACE_SECONDS", "10")

    assert TaskRecoveryConfig().TRANSCRIPTION_RECLAIM_GRACE == 300


def test_the_default_grace_covers_the_first_heartbeat():
    assert (
        task_recovery_config.TRANSCRIPTION_RECLAIM_GRACE
        >= task_recovery_config.TRANSCRIPTION_HEARTBEAT_TTL
    )


def test_a_new_task_row_carries_updated_at(db_session, normal_user):
    """A NULL ``updated_at`` is what put a brand-new row into the lost-run query."""
    media_file = _file(db_session, normal_user)
    before = datetime.now(UTC)

    task = create_task_record(
        db_session, f"rr-{uuid.uuid4()}", normal_user.id, media_file.id, "transcription"
    )

    assert task.updated_at is not None
    assert before - timedelta(seconds=5) <= task.updated_at <= datetime.now(UTC)
