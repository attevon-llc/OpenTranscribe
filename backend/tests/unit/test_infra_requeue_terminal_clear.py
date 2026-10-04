"""The infrastructure-requeue counter is cleared on EVERY terminal transition (issue #1162).

``task_liveness.INFRA_REQUEUES_KEY`` counts, per file, how often a dead worker's run was put
back. It used to be cleared only on success and in ``transcription_retry._fail_file``. A file
that ended CANCELLED (user cancel, ``reconcile_cancellation``, the unarmed fallback) or ERROR
through any other path (``on_pipeline_error``'s "Transcription pipeline failed unexpectedly",
the recovery sweeps) kept its entry forever, so ``transcription_files_infra_requeued`` and the
poison-loop alert stayed up after every file had finished, and a re-run inherited the stale
count.

The clear now happens at the one place every terminal status write passes through -- the ORM
write of ``MediaFile.status`` -- and only once that write commits. The gauge additionally
ignores files that are already terminal.

Every test arranges real rows on the savepoint-backed session; Redis is the dict-backed
stand-in shared by the liveness tests.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.task_cancellation import TranscriptionCancelledError
from app.core.task_liveness import INFRA_REQUEUES_KEY
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    return install_fake_redis(monkeypatch)


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def own_sessions(db_session):
    """Route the modules' own ``session_scope()`` onto the test's savepoint session."""
    scope = lambda: _yield_session(db_session)  # noqa: E731
    with (
        patch("app.tasks.transcription.cancellation.session_scope", scope),
        patch("app.tasks.transcription.dispatch.session_scope", scope),
        patch("app.tasks.transcription.run_ownership.session_scope", scope),
        patch("app.tasks.transcription.notifications.send_notification_via_redis"),
        patch("app.tasks.transcription.notifications.send_error_notification"),
    ):
        yield db_session


def _requeued_run(db, user, *, status=FileStatus.PROCESSING, requeues=2):
    media_file = MediaFile(
        uuid=uuid.uuid4(),
        user_id=user.id,
        filename="requeued.wav",
        storage_path=f"user_{user.id}/requeued.wav",
        file_size=1024,
        content_type="audio/wav",
        status=status,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    task_id = str(uuid.uuid4())
    db.add(
        Task(
            id=task_id,
            user_id=user.id,
            media_file_id=media_file.id,
            task_type="transcription",
            status="in_progress",
            created_at=datetime.now(UTC),
        )
    )
    media_file.active_task_id = task_id
    db.commit()
    db.refresh(media_file)
    from app.core.redis import get_redis

    get_redis().zincrby(INFRA_REQUEUES_KEY, requeues, str(media_file.uuid))
    return media_file, task_id


def _requeues(fake: FakeRedis, media_file: MediaFile) -> float | None:
    return fake.zscore(INFRA_REQUEUES_KEY, str(media_file.uuid))


class TestCancelledFilesAreCleared:
    def test_a_cancel_confirmed_by_the_worker_clears_the_counter(
        self, db_session, normal_user, fake_redis, own_sessions
    ):
        from app.tasks.transcription.cancellation import finish_cancelled

        media_file, task_id = _requeued_run(db_session, normal_user, status=FileStatus.CANCELLING)
        assert _requeues(fake_redis, media_file) == 2

        finish_cancelled(
            MagicMock(file_id=media_file.id, user_id=normal_user.id),
            task_id,
            str(media_file.uuid),
            TranscriptionCancelledError("stood down"),
            stage="GPU transcription",
        )

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.CANCELLED
        assert _requeues(fake_redis, media_file) is None

    def test_reconcile_cancellation_clears_the_counter(
        self, db_session, normal_user, fake_redis, own_sessions
    ):
        """CANCELLING -> CANCELLED with no worker confirmation: the case the issue observed."""
        from app.tasks.transcription.cancellation import reconcile_cancellation

        media_file, task_id = _requeued_run(db_session, normal_user, status=FileStatus.CANCELLING)

        assert reconcile_cancellation(media_file.id, task_id) == {"outcome": "forced"}

        assert _requeues(fake_redis, media_file) is None

    def test_the_unarmed_cancel_fallback_clears_the_counter(
        self, db_session, normal_user, fake_redis
    ):
        """``_finalize_unconfirmed_cancellation`` assigns ``status`` directly, bypassing every
        helper -- the reason the clear lives on the ORM write and not in a helper."""
        from app.utils.task_utils import cancel_active_task

        media_file, _task_id = _requeued_run(db_session, normal_user)

        with patch("app.core.task_cancellation.request_cancel", return_value=False):
            assert cancel_active_task(db_session, media_file.id) is True

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.CANCELLED
        assert _requeues(fake_redis, media_file) is None


class TestFailedFilesAreCleared:
    def test_the_pipeline_error_safety_net_clears_the_counter(
        self, db_session, normal_user, fake_redis, own_sessions
    ):
        """``on_pipeline_error`` -- "Transcription pipeline failed unexpectedly"."""
        from app.tasks.transcription.dispatch import on_pipeline_error

        media_file, task_id = _requeued_run(db_session, normal_user)

        on_pipeline_error(str(media_file.uuid), task_id)

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR
        task = db_session.query(Task).filter(Task.id == task_id).one()
        assert task.error_message == "Transcription pipeline failed unexpectedly"
        assert _requeues(fake_redis, media_file) is None

    def test_a_recovery_error_write_clears_the_counter(self, db_session, normal_user, fake_redis):
        """The recovery sweeps fail files with ``update_media_file_status(ERROR)``."""
        from app.utils.task_utils import update_media_file_status

        media_file, _ = _requeued_run(db_session, normal_user)

        update_media_file_status(db_session, media_file.id, FileStatus.ERROR)

        assert _requeues(fake_redis, media_file) is None

    def test_the_task_status_aggregate_clears_the_counter(
        self, db_session, normal_user, fake_redis
    ):
        """``update_task_status(failed)`` re-derives the file status from its tasks."""
        from app.utils.task_utils import update_task_status

        media_file, task_id = _requeued_run(db_session, normal_user)

        update_task_status(db_session, task_id, "failed", error_message="boom", completed=True)

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR
        assert _requeues(fake_redis, media_file) is None

    def test_completion_clears_the_counter(self, db_session, normal_user, fake_redis):
        from app.utils.task_utils import update_media_file_status

        media_file, _ = _requeued_run(db_session, normal_user)

        update_media_file_status(db_session, media_file.id, FileStatus.COMPLETED)

        assert _requeues(fake_redis, media_file) is None


class TestTheBudgetSurvivesEverythingElse:
    @pytest.mark.parametrize(
        "status", [FileStatus.PENDING, FileStatus.PROCESSING, FileStatus.CANCELLING]
    )
    def test_a_non_terminal_write_keeps_the_counter(
        self, db_session, normal_user, fake_redis, status
    ):
        """A retry puts the file back to PENDING: the poison budget must keep accumulating."""
        from app.utils.task_utils import update_media_file_status

        media_file, _ = _requeued_run(db_session, normal_user)

        update_media_file_status(db_session, media_file.id, status)

        assert _requeues(fake_redis, media_file) == 2

    def test_a_rolled_back_terminal_write_keeps_the_counter(
        self, db_session, normal_user, fake_redis
    ):
        media_file, _ = _requeued_run(db_session, normal_user)
        file_uuid = str(media_file.uuid)

        media_file.status = FileStatus.ERROR
        db_session.flush()
        db_session.rollback()
        db_session.commit()  # the next commit must not replay the rolled-back write's clear

        assert fake_redis.zscore(INFRA_REQUEUES_KEY, file_uuid) == 2

    def test_an_unrelated_column_write_keeps_the_counter(self, db_session, normal_user, fake_redis):
        media_file, _ = _requeued_run(db_session, normal_user, status=FileStatus.ERROR)

        media_file.title = "renamed"
        db_session.commit()

        assert _requeues(fake_redis, media_file) == 2

    def test_an_unreachable_redis_never_fails_the_status_write(
        self, db_session, normal_user, fake_redis
    ):
        from app.utils.task_utils import update_media_file_status
        from tests.unit._fake_liveness_redis import BrokenRedis

        media_file, _ = _requeued_run(db_session, normal_user)

        with patch("app.core.redis.get_redis", lambda: BrokenRedis()):
            update_media_file_status(db_session, media_file.id, FileStatus.ERROR)

        db_session.expire_all()
        stored = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
        assert stored.status == FileStatus.ERROR


class TestTheGaugeIgnoresTerminalFiles:
    def test_only_a_file_still_in_flight_is_counted(
        self, db_session, normal_user, fake_redis, monkeypatch
    ):
        """A stale entry (written before this fix, or by a path that slipped past the clear)
        must not hold the poison alert up once its file has finished."""
        from app.core import celery_metrics
        from app.core.metrics import transcription_files_infra_requeued

        scope = lambda: _yield_session(db_session)  # noqa: E731
        monkeypatch.setattr("app.db.session_utils.session_scope", scope)
        monkeypatch.setenv("TRANSCRIPTION_INFRA_REQUEUE_ALERT_THRESHOLD", "3")

        in_flight, _ = _requeued_run(db_session, normal_user, requeues=4)
        stale = []
        for status in (FileStatus.CANCELLED, FileStatus.ERROR, FileStatus.COMPLETED):
            media_file, _ = _requeued_run(db_session, normal_user, status=status, requeues=4)
            stale.append(media_file)
        below_threshold, _ = _requeued_run(db_session, normal_user, requeues=1)

        celery_metrics.update_transcription_lease_metrics()

        assert transcription_files_infra_requeued._value.get() == 1
        assert {_requeues(fake_redis, m) for m in stale} == {4}, "the gauge is read-only"
        assert _requeues(fake_redis, in_flight) == 4
        assert _requeues(fake_redis, below_threshold) == 1
