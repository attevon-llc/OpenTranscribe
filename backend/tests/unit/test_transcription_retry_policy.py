"""One retry policy for a transcription run that ended early (services/transcription_retry.py).

Owner requirement this pins: no file may hang or silently fail because a worker died, and
every processing failure is sorted into one of two classes --

* PERMANENT (the input is unusable): fail fast, clear reason, never retried automatically;
* TRANSIENT (the infrastructure failed, or the cause is unknown): requeued within minutes,
  ahead of newer submissions, until a bounded budget is spent; only then ERROR, with a reason
  that says it was interrupted and retried.

The headline regression: a run whose worker was killed was "recovered" by marking its Task
row failed, which ran the file-status aggregate and turned the file ERROR before the retry
branch could see it was still PROCESSING. In a load test with workers restarted under load,
84 of 418 files ended in ERROR that way. ``test_a_run_whose_worker_died_is_requeued_not_errored``
fails on the code before this change.

Every test arranges real rows; Redis is the dict-backed stand-in shared by the liveness tests.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.core.task_config import TaskRecoveryConfig
from app.core.task_liveness import INFRA_REQUEUES_KEY
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.task_recovery_service import TaskRecoveryService
from app.utils.error_classification import ErrorCategory
from app.utils.error_classification import is_transient
from app.utils.error_classification import transient_retry_delay
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis

CONFIG = TaskRecoveryConfig(STALENESS_THRESHOLD=300)

PERMANENT_RAW_ERRORS = [
    "This file appears to be corrupted or is not a valid audio/video file.",
    "This video file does not contain any audio tracks.",
    "No speech could be detected in this file. The file may contain only music.",
    "This file format is not supported. Please convert to a common format.",
    "This file is empty and contains no content to process.",
    "Audio file is too short to contain meaningful content",
    "This file appears to be DRM-protected or encrypted and cannot be processed.",
]

TRANSIENT_RAW_ERRORS = [
    ("CUDA out of memory. Tried to allocate 2.00 GiB", ErrorCategory.GPU_OOM),
    ("Worker exited prematurely: signal 9 (SIGKILL). worker lost", ErrorCategory.WORKER_LOST),
    ("Connection reset by peer while uploading to object storage", ErrorCategory.NETWORK_ERROR),
    ("Read timeout on the model server", ErrorCategory.NETWORK_ERROR),
    ("Something nobody anticipated", ErrorCategory.UNKNOWN),
]


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    return install_fake_redis(monkeypatch)


@contextmanager
def _bridged_scope(db_session):
    yield db_session
    db_session.commit()


@pytest.fixture
def policy_db(db_session, monkeypatch):
    """Route the policy's own sessions onto the test's savepoint-backed session."""
    monkeypatch.setattr(
        "app.services.transcription_retry.session_scope", lambda: _bridged_scope(db_session)
    )
    return db_session


@pytest.fixture
def notifications(monkeypatch) -> dict[str, list]:
    sent: dict[str, list] = {"error": [], "progress": []}
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_error_notification",
        lambda user_id, file_id, message: sent["error"].append(message),
    )
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_progress_notification",
        lambda user_id, file_id, progress, message: sent["progress"].append(message),
    )
    return sent


@pytest.fixture
def dispatched(monkeypatch) -> list[tuple[int, int | None]]:
    """Record retry dispatches instead of publishing to a broker."""
    calls: list[tuple[int, int | None]] = []

    def _fake(self, media_file_id, countdown=None):
        calls.append((media_file_id, countdown))
        return True

    monkeypatch.setattr(TaskRecoveryService, "schedule_file_retry", _fake)
    monkeypatch.setattr(
        "app.core.broker_orphans.discard_run_deliveries", lambda run_id, client=None: 0
    )
    return calls


def _file(db, user, **kwargs) -> MediaFile:
    defaults = {
        "uuid": str(uuid.uuid4()),
        "user_id": user.id,
        "filename": f"rp-{uuid.uuid4().hex[:8]}.wav",
        "storage_path": f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        "file_size": 1024,
        "content_type": "audio/wav",
        "status": FileStatus.PROCESSING,
        "retry_count": 0,
        "task_last_update": datetime.now(UTC) - timedelta(minutes=20),
    }
    media_file = MediaFile(**{**defaults, **kwargs})
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _run(db, user, media_file, *, quiet_for: timedelta = timedelta(minutes=20)) -> Task:
    now = datetime.now(UTC)
    task = Task(
        id=f"rp-{uuid.uuid4()}",
        user_id=user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
        progress=0.3,
        created_at=now - timedelta(hours=1),
        updated_at=now - quiet_for,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _reload(db, model, ident):
    db.expire_all()
    return db.query(model).filter(model.id == ident).one()


# =============================================================================
# Classification: the two classes
# =============================================================================
@pytest.mark.parametrize("raw", PERMANENT_RAW_ERRORS)
def test_an_unusable_input_is_classified_permanent(raw):
    failure = ErrorCategorizationService.classify_failure(raw)

    assert failure.retry_category == ErrorCategory.INVALID_MEDIA
    assert not is_transient(failure.retry_category)


@pytest.mark.parametrize(("raw", "category"), TRANSIENT_RAW_ERRORS)
def test_an_infrastructure_failure_is_classified_transient(raw, category):
    failure = ErrorCategorizationService.classify_failure(raw)

    assert failure.retry_category == category
    assert is_transient(failure.retry_category)


def test_an_infrastructure_signal_outranks_an_input_shaped_sentence():
    """'connection reset while decoding' is a lost connection, not a corrupt file."""
    failure = ErrorCategorizationService.classify_failure(
        "cannot decode stream: connection reset by peer"
    )

    assert failure.retry_category == ErrorCategory.NETWORK_ERROR


def test_retry_backoff_grows_is_jittered_and_stays_within_minutes():
    first = [transient_retry_delay(ErrorCategory.GPU_OOM, 0) for _ in range(50)]
    third = [transient_retry_delay(ErrorCategory.GPU_OOM, 2) for _ in range(50)]
    capped = [transient_retry_delay(ErrorCategory.GPU_OOM, 12) for _ in range(50)]

    assert all(30 <= d <= 60 for d in first)
    assert all(120 <= d <= 240 for d in third)
    assert max(capped) <= 600  # within minutes, never hours
    assert len(set(first)) > 1  # jittered: failures that happened together spread out


# =============================================================================
# A stage that raised
# =============================================================================
@pytest.mark.parametrize("raw", PERMANENT_RAW_ERRORS[:3])
def test_a_permanent_failure_fails_fast_and_is_never_retried(
    raw, policy_db, normal_user, fake_redis, notifications, dispatched
):
    from app.services.transcription_retry import RunOutcome
    from app.services.transcription_retry import finish_failed_run

    media_file = _file(policy_db, normal_user)
    run = _run(policy_db, normal_user, media_file)

    outcome = finish_failed_run(
        run.id, media_file.id, ErrorCategorizationService.classify_failure(raw)
    )

    stored = _reload(policy_db, MediaFile, media_file.id)
    assert outcome == RunOutcome.FAILED
    assert stored.status == FileStatus.ERROR
    assert stored.error_category == ErrorCategory.INVALID_MEDIA.value
    assert int(stored.retry_count or 0) == 0
    assert dispatched == []
    assert notifications["error"] == [stored.last_error_message]
    assert raw not in (stored.last_error_message or "")  # the fixed sentence, not the raw text


@pytest.mark.parametrize(("raw", "category"), TRANSIENT_RAW_ERRORS)
def test_a_transient_failure_is_requeued_not_errored(
    raw, category, policy_db, normal_user, fake_redis, notifications, dispatched
):
    from app.services.transcription_retry import RunOutcome
    from app.services.transcription_retry import finish_failed_run

    media_file = _file(policy_db, normal_user)
    run = _run(policy_db, normal_user, media_file)

    outcome = finish_failed_run(
        run.id, media_file.id, ErrorCategorizationService.classify_failure(raw)
    )

    stored = _reload(policy_db, MediaFile, media_file.id)
    old_run = _reload(policy_db, Task, run.id)
    assert outcome == RunOutcome.RETRIED
    assert stored.status == FileStatus.PENDING
    assert stored.error_category == category.value
    assert stored.retry_count == 1
    assert old_run.status == "failed"  # retired, so a late copy of its message stands down
    assert [file_id for file_id, _ in dispatched] == [media_file.id]
    countdown = dispatched[0][1]
    assert countdown is not None and 0 < countdown <= 600
    assert notifications["error"] == []  # the user is not told it failed while it is retried


def test_transient_retries_respect_the_admin_retry_limit(
    policy_db, normal_user, fake_redis, notifications, dispatched
):
    """The budget is the existing admin setting, not a new knob: with max_retries=2 the
    third failure is final, and it says it was interrupted and retried."""
    from app.services import system_settings_service
    from app.services.transcription_retry import RunOutcome
    from app.services.transcription_retry import finish_failed_run

    system_settings_service.set_setting(policy_db, "transcription.max_retries", 2)
    media_file = _file(policy_db, normal_user)
    failure = ErrorCategorizationService.classify_failure("CUDA out of memory")

    outcomes = []
    for _attempt in range(3):
        run = _run(policy_db, normal_user, media_file)
        outcomes.append(finish_failed_run(run.id, media_file.id, failure))

    stored = _reload(policy_db, MediaFile, media_file.id)
    assert outcomes == [RunOutcome.RETRIED, RunOutcome.RETRIED, RunOutcome.FAILED]
    assert len(dispatched) == 2
    assert stored.status == FileStatus.ERROR
    assert stored.error_category == ErrorCategory.RETRIES_EXHAUSTED.value
    assert stored.last_error_message == ErrorCategorizationService.interrupted_message()
    assert ErrorCategorizationService.get_error_info(stored.last_error_message)["category"] == (
        "interrupted"
    )


def test_an_unlimited_admin_retry_setting_keeps_retrying(
    policy_db, normal_user, fake_redis, notifications, dispatched
):
    from app.services import system_settings_service
    from app.services.transcription_retry import RunOutcome
    from app.services.transcription_retry import finish_failed_run

    system_settings_service.set_setting(policy_db, "transcription.max_retries", 0)
    media_file = _file(policy_db, normal_user, retry_count=25)
    run = _run(policy_db, normal_user, media_file)

    outcome = finish_failed_run(
        run.id, media_file.id, ErrorCategorizationService.classify_failure("network timeout")
    )

    assert outcome == RunOutcome.RETRIED


# =============================================================================
# The worker died (health-check fallback): THE regression
# =============================================================================
def _stuck_dead_run(db, user):
    media_file = _file(db, user)
    run = _run(db, user, media_file)
    # An unrelated, older task of the same file that completed: with it present the old
    # aggregate even marked the file COMPLETED -- with no transcript.
    db.add(
        Task(
            id=f"rp-wave-{uuid.uuid4()}",
            user_id=user.id,
            media_file_id=media_file.id,
            task_type="waveform",
            status="completed",
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    db.commit()
    return media_file, run


def test_a_run_whose_worker_died_is_requeued_not_errored(
    db_session, normal_user, fake_redis, notifications, dispatched, monkeypatch
):
    """No lease, no queued marker: the worker died. The file must be requeued, not ERROR."""
    from app.services.task_detection_service import TaskDetectionService

    monkeypatch.setattr(
        "app.services.transcription_retry.session_scope", lambda: _bridged_scope(db_session)
    )
    media_file, run = _stuck_dead_run(db_session, normal_user)

    stuck = TaskDetectionService(config=CONFIG).identify_stuck_tasks(db_session)
    assert run.id in {t.id for t in stuck}
    assert TaskRecoveryService(config=CONFIG).recover_stuck_task(db_session, run) is True

    stored = _reload(db_session, MediaFile, media_file.id)
    assert stored.status == FileStatus.PENDING
    assert [file_id for file_id, _ in dispatched] == [media_file.id]
    assert dispatched[0][1] is None  # nothing was wrong with the file: no backoff
    assert notifications["error"] == []


def test_worker_loss_does_not_spend_the_error_retry_budget(
    db_session, normal_user, fake_redis, notifications, dispatched
):
    media_file, run = _stuck_dead_run(db_session, normal_user)

    TaskRecoveryService(config=CONFIG).recover_stuck_task(db_session, run)

    stored = _reload(db_session, MediaFile, media_file.id)
    assert int(stored.retry_count or 0) == 0
    assert fake_redis.zscore(INFRA_REQUEUES_KEY, str(media_file.uuid)) == 1


def test_a_file_that_keeps_killing_workers_fails_once_the_infra_cap_is_spent(
    db_session, normal_user, fake_redis, notifications, dispatched, monkeypatch
):
    """The loop-breaker: a poison input that OOM-kills every worker must not cycle forever."""
    from app.core.task_config import task_recovery_config

    monkeypatch.setattr(task_recovery_config, "TRANSCRIPTION_MAX_INFRA_REQUEUES", 2)
    media_file = _file(db_session, normal_user)

    for _death in range(3):
        run = _run(db_session, normal_user, media_file)
        TaskRecoveryService(config=CONFIG).recover_stuck_task(db_session, run)

    stored = _reload(db_session, MediaFile, media_file.id)
    assert len(dispatched) == 2
    assert stored.status == FileStatus.ERROR
    assert stored.error_category == ErrorCategory.RETRIES_EXHAUSTED.value
    assert stored.last_error_message == ErrorCategorizationService.interrupted_message()
    assert notifications["error"] == [ErrorCategorizationService.interrupted_message()]
    # Cleared once the file reached a terminal state, so the poison gauge stops counting it.
    assert fake_redis.zscore(INFRA_REQUEUES_KEY, str(media_file.uuid)) is None


def test_recovery_concludes_nothing_when_the_lease_store_is_unreadable(
    db_session, normal_user, notifications, dispatched, monkeypatch
):
    from tests.unit._fake_liveness_redis import BrokenRedis

    monkeypatch.setattr("app.core.redis.get_redis", lambda: BrokenRedis())
    media_file, run = _stuck_dead_run(db_session, normal_user)

    assert TaskRecoveryService(config=CONFIG).recover_stuck_task(db_session, run) is False

    stored = _reload(db_session, MediaFile, media_file.id)
    assert stored.status == FileStatus.PROCESSING
    assert dispatched == []


# =============================================================================
# Automatic retries never go after Step 5's permanent or exhausted files
# =============================================================================
@pytest.mark.parametrize("category", [ErrorCategory.INVALID_MEDIA, ErrorCategory.RETRIES_EXHAUSTED])
def test_the_error_retry_sweep_skips_permanent_and_exhausted_files(
    category, db_session, normal_user
):
    from app.services.task_detection_service import TaskDetectionService

    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        error_category=category.value,
        completed_at=datetime.now(UTC) - timedelta(hours=3),
    )

    youtube, transcription = TaskDetectionService(
        config=CONFIG
    ).identify_retriable_error_files_split(db_session)

    assert media_file.id not in {f.id for f in youtube + transcription}


# =============================================================================
# Fair ordering: a retry goes ahead of newer submissions
# =============================================================================
def _dispatch_and_capture(db_session, user, monkeypatch, **kwargs):
    from celery.canvas import _chain

    from app.tasks.transcription import dispatch as dispatch_module

    media_file = _file(db_session, user, status=FileStatus.PENDING)
    monkeypatch.setattr(dispatch_module, "session_scope", lambda: _bridged_scope(db_session))
    captured = []
    with (
        patch.object(_chain, "apply_async", autospec=True) as apply_async,
        patch.object(dispatch_module, "_resolve_gpu_queue", return_value="gpu"),
    ):
        dispatch_module.dispatch_transcription_pipeline(file_uuid=str(media_file.uuid), **kwargs)
        captured = list(apply_async.call_args[0][0].tasks)
    return captured


def test_a_retry_is_dispatched_ahead_of_new_submissions(
    db_session, normal_user, fake_redis, monkeypatch
):
    """kombu's Redis transport serves the LOWER priority number first, per queue."""
    fresh = _dispatch_and_capture(db_session, normal_user, monkeypatch)
    retry = _dispatch_and_capture(db_session, normal_user, monkeypatch, retry=True, countdown=40)

    assert [s.name for s in fresh] == [s.name for s in retry]
    for new_stage, retried_stage in zip(fresh, retry, strict=True):
        assert new_stage.options["queue"] == retried_stage.options["queue"]
        assert retried_stage.options["priority"] < new_stage.options["priority"]
    assert retry[0].options["countdown"] == 40
    assert "countdown" not in fresh[0].options


# =============================================================================
# A replaced run's failure path leaves the file to the replacement
# =============================================================================
def test_the_pipeline_errback_leaves_a_replaced_run_alone(
    db_session, normal_user, fake_redis, monkeypatch
):
    from app.tasks.transcription import dispatch as dispatch_module
    from app.tasks.transcription import run_ownership

    monkeypatch.setattr(run_ownership, "session_scope", lambda: _bridged_scope(db_session))
    monkeypatch.setattr(dispatch_module, "session_scope", lambda: _bridged_scope(db_session))
    media_file = _file(db_session, normal_user)
    old = _run(db_session, normal_user, media_file)
    old.status = "failed"
    db_session.add(
        Task(
            id=f"rp-new-{uuid.uuid4()}",
            user_id=normal_user.id,
            media_file_id=media_file.id,
            task_type="transcription",
            status="in_progress",
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    cleaned: list[str] = []
    monkeypatch.setattr(
        "app.services.minio_service.cleanup_temp_audio", lambda uuid_: cleaned.append(uuid_)
    )

    dispatch_module.on_pipeline_error(str(media_file.uuid), old.id)

    stored = _reload(db_session, MediaFile, media_file.id)
    assert stored.status == FileStatus.PROCESSING
    assert cleaned == []  # the replacement run's temp audio is untouched
