"""Recovery must not fail or duplicate a transcription that is only waiting (issue #1020).

When no GPU worker is consuming ``gpu`` for a while, the transcription message waits in the
broker. Recovery judged "stuck" from DB timestamps and the checking process's start time, so:

* (a) restarting the worker that runs the health check made every queued transcription look
  dead (its ``updated_at`` predated the new process) — recovery failed it and dispatched a
  second pipeline while the first message was still queued, so both ran;
* (b) after an hour, ``identify_stuck_tasks`` failed it, because the budget was measured from
  ``created_at`` (dispatch time) although the run never started;
* (c) after an API restart an hour later, ``identify_orphaned_tasks`` failed it the same way.

Each case below arranges the run's liveness markers (``core/task_liveness.py``) in a fake
Redis and asserts on real rows. Every "not recovered" case has a sibling proving that a
genuinely dead run is still recovered, so a detector that simply stopped firing fails here.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.core.task_cancellation import CANCEL_KEY
from app.core.task_config import TaskRecoveryConfig
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.task_detection_service import TaskDetectionService
from app.services.task_recovery_service import TaskRecoveryService
from tests.unit._fake_liveness_redis import BrokenRedis
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis

CONFIG = TaskRecoveryConfig(
    MAX_TASK_DURATIONS={"transcription": 3600, "default": 1800},
    STALENESS_THRESHOLD=300,
    ORPHANED_TASK_THRESHOLD=1,
)


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    return install_fake_redis(monkeypatch)


@pytest.fixture
def detection() -> TaskDetectionService:
    return TaskDetectionService(config=CONFIG)


@pytest.fixture
def recovery() -> TaskRecoveryService:
    return TaskRecoveryService(config=CONFIG)


def _file(db, user, **kwargs) -> MediaFile:
    defaults = {
        "user_id": user.id,
        "filename": f"q-{uuid.uuid4().hex[:8]}.wav",
        "storage_path": f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        "file_size": 1024,
        "content_type": "audio/wav",
        "status": FileStatus.PROCESSING,
        "task_last_update": datetime.now(UTC) - timedelta(minutes=70),
    }
    media_file = MediaFile(**{**defaults, **kwargs})
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _transcription(db, user, media_file, *, age: timedelta, quiet_for: timedelta) -> Task:
    """A transcription dispatched ``age`` ago whose row was last touched ``quiet_for`` ago."""
    now = datetime.now(UTC)
    task = Task(
        id=f"t1020-{uuid.uuid4()}",
        user_id=user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
        progress=0.2,
        created_at=now - age,
        updated_at=now - quiet_for,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# =============================================================================
# (b) the duration budget is measured from the start, never from dispatch
# =============================================================================
def test_a_queued_transcription_is_not_stuck_after_waiting_past_its_budget(
    detection, db_session, normal_user, fake_redis
):
    """Two hours in the queue uses none of a run's one-hour budget."""
    media_file = _file(db_session, normal_user)
    queued = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )
    fake_redis.mark_queued(queued.id)

    ids = {t.id for t in detection.identify_stuck_tasks(db_session)}

    assert queued.id not in ids


def test_a_dead_transcription_is_still_stuck(detection, db_session, normal_user, fake_redis):
    """CONTROL for the case above: no heartbeat and no queued marker means the run is gone."""
    media_file = _file(db_session, normal_user)
    dead = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )

    ids = {t.id for t in detection.identify_stuck_tasks(db_session)}

    assert dead.id in ids


def test_a_running_transcription_is_judged_from_when_it_started(
    detection, db_session, normal_user, fake_redis
):
    """A run heartbeating for 10 minutes is inside its budget even if dispatched 2 h ago;
    one heartbeating for 2 h is past it (the backstop for a wedged-but-alive stage)."""
    media_file = _file(db_session, normal_user)
    young_run = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )
    old_run = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=3), quiet_for=timedelta(hours=1)
    )
    fake_redis.mark_running(young_run.id, started_seconds_ago=600)
    fake_redis.mark_running(old_run.id, started_seconds_ago=7200)

    ids = {t.id for t in detection.identify_stuck_tasks(db_session)}

    assert young_run.id not in ids
    assert old_run.id in ids


def test_an_unreadable_broker_never_marks_a_transcription_stuck(
    detection, db_session, normal_user, monkeypatch
):
    """Redis is the broker: when it cannot be read, whether the message still exists is
    unknown, and recovering on that guess is the duplicate-run defect itself."""
    monkeypatch.setattr("app.core.redis.get_redis", lambda: BrokenRedis())
    media_file = _file(db_session, normal_user)
    task = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )

    ids = {t.id for t in detection.identify_stuck_tasks(db_session)}

    assert task.id not in ids


# =============================================================================
# (c) API restart: startup recovery's orphan and abandoned-file checks
# =============================================================================
def test_a_queued_transcription_is_not_orphaned_after_an_hour(
    detection, db_session, normal_user, fake_redis
):
    media_file = _file(db_session, normal_user)
    queued = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=2)
    )
    dead = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=2)
    )
    fake_redis.mark_queued(queued.id)

    ids = {t.id for t in detection.identify_orphaned_tasks(db_session)}

    assert queued.id not in ids
    assert dead.id in ids


def test_startup_recovery_does_not_abandon_a_file_whose_transcription_is_queued(
    detection, db_session, normal_user, fake_redis
):
    queued_file = _file(db_session, normal_user)
    queued = _transcription(
        db_session, normal_user, queued_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )
    fake_redis.mark_queued(queued.id)
    dead_file = _file(db_session, normal_user)
    dead = _transcription(
        db_session, normal_user, dead_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )

    abandoned, stale_task_ids = detection.identify_abandoned_files(db_session)

    abandoned_ids = {f.id for f in abandoned}
    assert queued_file.id not in abandoned_ids
    assert queued.id not in stale_task_ids
    assert dead_file.id in abandoned_ids
    assert dead.id in stale_task_ids


# =============================================================================
# (a) worker restart: Step 3 must not read "predates this process" as "dead"
# =============================================================================
def test_step3_leaves_a_queued_transcription_alone_after_a_worker_restart(
    detection, db_session, normal_user, fake_redis
):
    """The task was last touched 10 minutes ago -- before this test process loaded the
    detection module, which is exactly what a freshly restarted utility worker sees."""
    media_file = _file(
        db_session, normal_user, task_last_update=datetime.now(UTC) - timedelta(minutes=10)
    )
    queued = _transcription(
        db_session,
        normal_user,
        media_file,
        age=timedelta(minutes=12),
        quiet_for=timedelta(minutes=10),
    )
    fake_redis.mark_queued(queued.id)

    ids = {f.id for f in detection.identify_stuck_files_without_active_celery_tasks(db_session)}

    assert media_file.id not in ids


def test_step3_still_recovers_a_started_transcription_whose_worker_died(
    detection, recovery, db_session, normal_user, fake_redis
):
    """The dead-started path end to end: detected, its Task failed, its run cancelled so a
    redelivered message stands down, and exactly one replacement scheduled."""
    media_file = _file(
        db_session, normal_user, task_last_update=datetime.now(UTC) - timedelta(minutes=10)
    )
    dead = _transcription(
        db_session,
        normal_user,
        media_file,
        age=timedelta(minutes=40),
        quiet_for=timedelta(minutes=10),
    )
    # It started (the dispatch-time queued marker is gone) and its heartbeat has lapsed.

    stuck = detection.identify_stuck_files_without_active_celery_tasks(db_session)
    assert media_file.id in {f.id for f in stuck}

    retried: list[int] = []

    def _record_retry(self, media_file_id: int) -> bool:
        retried.append(media_file_id)
        return True

    with patch.object(TaskRecoveryService, "schedule_file_retry", _record_retry):
        stats = recovery.recover_stuck_files_without_celery_tasks(
            db_session, [f for f in stuck if f.id == media_file.id]
        )

    db_session.refresh(dead)
    assert dead.status == "failed"
    assert CANCEL_KEY.format(task_id=dead.id) in fake_redis.store
    assert retried == [media_file.id]
    assert stats["tasks_retried"] == 1


def test_step2_does_not_report_a_file_with_a_queued_transcription_as_inconsistent(
    detection, db_session, normal_user, fake_redis
):
    queued_file = _file(db_session, normal_user)
    queued = _transcription(
        db_session, normal_user, queued_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )
    fake_redis.mark_queued(queued.id)
    dead_file = _file(db_session, normal_user)
    _transcription(
        db_session, normal_user, dead_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )

    ids = {f.id for f in detection.identify_inconsistent_media_files(db_session)}

    assert queued_file.id not in ids
    assert dead_file.id in ids


# =============================================================================
# Every recovery path that fails a transcription also retires its run
# =============================================================================
def test_recovering_a_stuck_transcription_cancels_the_superseded_run(
    recovery, db_session, normal_user, fake_redis
):
    media_file = _file(db_session, normal_user)
    task = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )

    with (
        patch.object(TaskRecoveryService, "schedule_file_retry", return_value=True),
        patch("app.core.broker_orphans.discard_run_deliveries", return_value=0),
    ):
        assert recovery.recover_stuck_task(db_session, task) is True

    assert CANCEL_KEY.format(task_id=task.id) in fake_redis.store
    # Retired, not still advertised as waiting or running.
    assert f"transcription_queued:{task.id}" not in fake_redis.store


def test_the_admin_path_retires_a_transcription_even_while_it_reads_as_queued(
    recovery, db_session, normal_user, fake_redis
):
    """``requeue=False`` (the admin recover endpoints, which dispatch the replacement
    themselves) retires the run unconditionally and cancels it, as before."""
    media_file = _file(db_session, normal_user)
    task = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )
    fake_redis.mark_queued(task.id)

    assert recovery.recover_stuck_task(db_session, task, requeue=False) is True

    assert CANCEL_KEY.format(task_id=task.id) in fake_redis.store
    assert f"transcription_queued:{task.id}" not in fake_redis.store


def test_the_health_check_leaves_a_run_the_reaper_just_requeued(
    recovery, db_session, normal_user, fake_redis
):
    """Detection saw the run dead; by the time recovery acts the broker reaper has put its
    stage back on the queue (QUEUED). Recovering it now would dispatch a second pipeline."""
    media_file = _file(db_session, normal_user)
    task = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=1)
    )
    fake_redis.mark_queued(task.id)

    with patch.object(TaskRecoveryService, "schedule_file_retry", return_value=True) as retry:
        assert recovery.recover_stuck_task(db_session, task) is False

    retry.assert_not_called()
    db_session.refresh(task)
    assert task.status == "in_progress"


def test_recovering_an_orphaned_transcription_cancels_its_run(
    recovery, db_session, normal_user, fake_redis
):
    media_file = _file(db_session, normal_user)
    task = _transcription(
        db_session, normal_user, media_file, age=timedelta(hours=2), quiet_for=timedelta(hours=2)
    )

    assert recovery.recover_orphaned_tasks(db_session, [task]) == 1

    db_session.refresh(task)
    assert task.status == "failed"
    assert CANCEL_KEY.format(task_id=task.id) in fake_redis.store


# =============================================================================
# Step 5.7: the stuck -> reset -> stuck cycle ends
# =============================================================================
def test_step57_never_resets_a_transcription(detection, db_session, normal_user):
    """Resetting a transcription row to pending dispatches nothing, and a pending row is what
    let a superseded message still waiting in the broker pass its ownership check."""
    media_file = _file(db_session, normal_user)
    task = Task(
        id=f"t1020-{uuid.uuid4()}",
        user_id=normal_user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="failed",
        error_message="Task recovered after being stuck in processing",
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add(task)
    db_session.commit()

    ids = {t.id for t in detection.identify_false_positive_failed_tasks(db_session)}

    assert task.id not in ids


def test_the_step57_reset_loop_terminates(detection, recovery, db_session, normal_user):
    """Drive the real cycle -- Step 1 fails it, Step 5.7 resets it -- and require it to stop.

    The counter used to live in ``error_message`` and be overwritten by the next Step 1
    failure, so every cycle started again from attempt 0 and the task was reset every
    health check for three days.
    """
    media_file = _file(db_session, normal_user, status=FileStatus.COMPLETED)
    task = Task(
        id=f"t1020-{uuid.uuid4()}",
        user_id=normal_user.id,
        media_file_id=media_file.id,
        task_type="summarization",
        status="in_progress",
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add(task)
    db_session.commit()

    resets = 0
    for _tick in range(6):
        # Each health check runs 10 minutes after the last, so the row is stale again.
        task.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        db_session.commit()
        if task.id not in {t.id for t in detection.identify_stuck_tasks(db_session)}:
            break
        recovery.recover_stuck_task(db_session, task)  # Step 1
        found = [
            t for t in detection.identify_false_positive_failed_tasks(db_session) if t.id == task.id
        ]
        resets += recovery.recover_false_positive_failed_tasks(db_session, found)["tasks_reset"]
        db_session.refresh(task)

    db_session.refresh(task)
    assert resets == 2
    assert task.status == "failed"
    assert task.error_message == "Permanently failed after 2 recovery attempts"


# =============================================================================
# Thresholds are configurable from the environment
# =============================================================================
def _threshold(config: TaskRecoveryConfig, name: str) -> int:
    if name.startswith("MAX_TASK_DURATIONS."):
        assert config.MAX_TASK_DURATIONS is not None
        return config.MAX_TASK_DURATIONS[name.split(".", 1)[1]]
    return int(getattr(config, name))


@pytest.mark.parametrize(
    ("env_var", "field_name", "value"),
    [
        ("TASK_MAX_DURATION_TRANSCRIPTION_SECONDS", "MAX_TASK_DURATIONS.transcription", 7200),
        ("TASK_MAX_DURATION_DEFAULT_SECONDS", "MAX_TASK_DURATIONS.default", 900),
        ("TASK_RECOVERY_ORPHANED_HOURS", "ORPHANED_TASK_THRESHOLD", 6),
        ("TASK_RECOVERY_STALENESS_SECONDS", "STALENESS_THRESHOLD", 600),
        ("TRANSCRIPTION_HEARTBEAT_INTERVAL_SECONDS", "TRANSCRIPTION_HEARTBEAT_INTERVAL", 20),
        ("TRANSCRIPTION_HEARTBEAT_TTL_SECONDS", "TRANSCRIPTION_HEARTBEAT_TTL", 120),
        ("TRANSCRIPTION_QUEUE_MAX_WAIT_SECONDS", "TRANSCRIPTION_QUEUE_MAX_WAIT", 86400),
    ],
)
def test_recovery_thresholds_are_read_from_the_environment(monkeypatch, env_var, field_name, value):
    default = _threshold(TaskRecoveryConfig(), field_name)
    assert default != value  # otherwise the assertion below proves nothing
    monkeypatch.setenv(env_var, str(value))

    assert _threshold(TaskRecoveryConfig(), field_name) == value


def test_an_invalid_threshold_falls_back_to_its_default(monkeypatch):
    """A typo must not take every worker down at import, nor silently become 0."""
    monkeypatch.setenv("TASK_MAX_DURATION_TRANSCRIPTION_SECONDS", "one hour")
    monkeypatch.setenv("TASK_RECOVERY_ORPHANED_HOURS", "0")

    config = TaskRecoveryConfig()

    assert config.MAX_TASK_DURATIONS is not None
    assert config.MAX_TASK_DURATIONS["transcription"] == 3600
    assert config.ORPHANED_TASK_THRESHOLD == 1


def test_a_heartbeat_ttl_shorter_than_its_interval_is_widened(monkeypatch):
    """Otherwise the marker expires between two beats and every healthy run reads as dead."""
    monkeypatch.setenv("TRANSCRIPTION_HEARTBEAT_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("TRANSCRIPTION_HEARTBEAT_TTL_SECONDS", "30")

    config = TaskRecoveryConfig()

    assert config.TRANSCRIPTION_HEARTBEAT_TTL == 180
