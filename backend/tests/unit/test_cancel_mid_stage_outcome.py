"""A run that raises after its cancellation was requested ends CANCELLED, not failed (#1163).

A cancel arms the run's flag and moves the file to ``CANCELLING``. The stage stands down at
its next cooperative checkpoint with ``TranscriptionCancelledError`` -- but if it raises
anything else first (a storage fetch that fails because the stage is being torn down, a model
error, a connection reset) the generic ``except Exception`` handler used to run the failure
path: ERROR, an error notification, an ``error_category``, and a re-raise that sent
``on_pipeline_error`` on to write "Transcription pipeline failed unexpectedly". User
cancellations were counted as pipeline failures and the user was told their file failed.

Pinned here, per stage: with the cancel armed, the stage's exception finalizes the run as
cancelled -- file ``CANCELLED``, Task ``error_message == "Task cancelled by user"`` (the
existing convention for a cancelled run), no ``error_category``, no error notification -- and
the stage RETURNS the cancelled payload instead of raising, so Celery records no failure and
``on_pipeline_error`` never runs. The control at the bottom pins that the same exception
without a cancel still fails the file.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from unittest.mock import patch

import pytest

from app.core.task_cancellation import request_cancel
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.error_categorization_service import ErrorCategorizationService
from app.tasks.transcription.cancellation import CANCELLED_BY_USER
from app.utils.error_classification import ErrorCategory
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis

_STAGE_MODULES = (
    "app.services.transcription_retry",
    "app.tasks.transcription.cancellation",
    "app.tasks.transcription.dispatch",
    "app.tasks.transcription.run_ownership",
    "app.tasks.transcription.preprocess",
    "app.tasks.transcription.core",
    "app.tasks.transcription.cpu_task",
    "app.tasks.transcription.diarize_task",
    "app.tasks.transcription.postprocess",
)


class _StorageFetchError(RuntimeError):
    """What a torn-down object-storage read raises: not the cancel checkpoint's exception."""


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    return install_fake_redis(monkeypatch)


@contextmanager
def _yield_session(db):
    yield db
    db.commit()


@pytest.fixture
def notices(monkeypatch, db_session) -> dict[str, list]:
    """Route every stage's own sessions onto the test session; record what users are told."""
    import importlib

    sent: dict[str, list] = {"error": [], "status": []}
    for name in _STAGE_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "session_scope"):
            monkeypatch.setattr(module, "session_scope", lambda: _yield_session(db_session))
        if hasattr(module, "send_progress_notification"):
            monkeypatch.setattr(module, "send_progress_notification", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_error_notification",
        lambda user_id, file_id, message: sent["error"].append(message),
    )
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_progress_notification",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_notification_via_redis",
        lambda user_id, file_id, status, message, progress: sent["status"].append(status),
    )
    monkeypatch.setattr(
        "app.services.task_recovery_service.TaskRecoveryService.schedule_file_retry",
        lambda self, media_file_id, countdown=None: True,
    )
    return sent


def _run(db, user, *, status=FileStatus.PROCESSING) -> tuple[MediaFile, str]:
    media_file = MediaFile(
        uuid=uuid.uuid4(),
        user_id=user.id,
        filename="cancelled-mid-stage.wav",
        storage_path=f"user_{user.id}/cancelled-mid-stage.wav",
        file_size=1024,
        content_type="audio/wav",
        status=status,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    task_id = f"c1163-{uuid.uuid4()}"
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
    return media_file, task_id


def _cancel(db, media_file: MediaFile, task_id: str) -> None:
    """What ``cancel_active_task`` does: arm the run's flag and write CANCELLING."""
    request_cancel(task_id, str(media_file.uuid))
    media_file.cancellation_requested = True
    media_file.status = FileStatus.CANCELLING
    db.commit()


def _cancel_then_raise(db, media_file, task_id, message="object storage fetch failed"):
    """A stand-in for stage work: the cancel lands mid-stage, then the work raises."""

    def _work(*_args, **_kwargs):
        _cancel(db, media_file, task_id)
        raise _StorageFetchError(message)

    return _work


def _context(media_file: MediaFile, task_id: str) -> dict:
    return {
        "task_id": task_id,
        "file_uuid": str(media_file.uuid),
        "file_id": media_file.id,
        "user_id": media_file.user_id,
        "storage_path": media_file.storage_path,
        "file_name": media_file.filename,
        "content_type": media_file.content_type,
        "diarization_source": "provider",
    }


def assert_cancelled(db, media_file: MediaFile, task_id: str, notices: dict) -> None:
    db.expire_all()
    stored = db.query(MediaFile).filter(MediaFile.id == media_file.id).one()
    task = db.query(Task).filter(Task.id == task_id).one()
    assert stored.status == FileStatus.CANCELLED
    assert stored.error_category is None
    assert not stored.last_error_message
    assert task.status == "failed"  # the existing convention for a cancelled run
    assert task.error_message == CANCELLED_BY_USER
    assert task.completed_at is not None
    assert notices["error"] == [], "a cancelled file must not produce an error notification"
    assert notices["status"] == [FileStatus.CANCELLED]


def assert_cancelled_payload(result: dict, media_file: MediaFile, task_id: str) -> None:
    assert result["status"] == "cancelled"
    assert result["task_id"] == task_id
    assert result["file_uuid"] == str(media_file.uuid)


# =============================================================================
# The policy every stage failure goes through
# =============================================================================
class TestTheFailurePolicy:
    def test_a_failure_after_the_cancel_was_armed_is_a_cancellation(
        self, db_session, normal_user, fake_redis, notices
    ):
        from app.services.transcription_retry import RunOutcome
        from app.services.transcription_retry import finish_failed_run

        media_file, task_id = _run(db_session, normal_user)
        _cancel(db_session, media_file, task_id)
        failure = ErrorCategorizationService.classify_failure("Read timed out")

        assert finish_failed_run(task_id, media_file.id, failure) == RunOutcome.CANCELLED

        assert_cancelled(db_session, media_file, task_id, notices)

    def test_a_cancelling_file_counts_even_when_the_flag_has_expired(
        self, db_session, normal_user, fake_redis, notices
    ):
        """The flag has a TTL; the file's own CANCELLING state for this run is enough."""
        from app.services.transcription_retry import RunOutcome
        from app.services.transcription_retry import finish_failed_run

        media_file, task_id = _run(db_session, normal_user)
        _cancel(db_session, media_file, task_id)
        fake_redis.store.clear()
        failure = ErrorCategorizationService.classify_failure("Processing failed")

        assert finish_failed_run(task_id, media_file.id, failure) == RunOutcome.CANCELLED

        assert_cancelled(db_session, media_file, task_id, notices)

    def test_a_stale_error_category_from_an_earlier_retry_is_cleared(
        self, db_session, normal_user, fake_redis, notices
    ):
        from app.services.transcription_retry import finish_failed_run

        media_file, task_id = _run(db_session, normal_user)
        media_file.error_category = ErrorCategory.NETWORK_ERROR.value
        db_session.commit()
        _cancel(db_session, media_file, task_id)

        finish_failed_run(
            task_id, media_file.id, ErrorCategorizationService.classify_failure("boom")
        )

        assert_cancelled(db_session, media_file, task_id, notices)


# =============================================================================
# Each stage
# =============================================================================
class TestEachStage:
    def test_preprocess_download_failure_after_a_cancel(
        self, db_session, normal_user, fake_redis, notices
    ):
        from app.tasks.transcription import preprocess

        media_file, task_id = _run(db_session, normal_user)

        with patch.object(
            preprocess, "_preprocess_audio", _cancel_then_raise(db_session, media_file, task_id)
        ):
            result = preprocess.preprocess_for_transcription.run(
                file_uuid=str(media_file.uuid), task_id=task_id
            )

        assert_cancelled_payload(result, media_file, task_id)
        assert_cancelled(db_session, media_file, task_id, notices)

    def test_the_next_stage_forwards_a_cancelled_payload_untouched(self, fake_redis):
        """Preprocess returned the cancelled payload: the GPU stage must hand it to
        ``finalize_transcription`` (which releases the temp audio), not re-check the run and
        turn it into a superseded marker (which keeps the temp audio)."""
        from app.tasks.transcription import core

        payload = {"status": "cancelled", "task_id": "t", "file_uuid": "u", "file_id": 1}

        assert core.transcribe_gpu_task.run(dict(payload)) == payload

    def test_gpu_model_failure_after_a_cancel(self, db_session, normal_user, fake_redis, notices):
        from app.tasks.transcription import core

        media_file, task_id = _run(db_session, normal_user)

        with patch.object(
            core,
            "_resolve_asr_provider_or_none",
            _cancel_then_raise(db_session, media_file, task_id, "model inference failed"),
        ):
            result = core.transcribe_gpu_task.run(_context(media_file, task_id))

        assert_cancelled_payload(result, media_file, task_id)
        assert_cancelled(db_session, media_file, task_id, notices)

    def test_gpu_diarization_failure_after_a_cancel(
        self, db_session, normal_user, fake_redis, notices
    ):
        from app.tasks.transcription import diarize_task
        from app.transcription.engine.job import RawTranscriptResult

        media_file, task_id = _run(db_session, normal_user)

        with patch.object(
            RawTranscriptResult,
            "deserialize",
            _cancel_then_raise(db_session, media_file, task_id, "diarization model failed"),
        ):
            result = diarize_task.diarize_gpu_task.run({}, _context(media_file, task_id))

        assert_cancelled_payload(result, media_file, task_id)
        assert_cancelled(db_session, media_file, task_id, notices)

    def test_cpu_transcription_fetch_failure_after_a_cancel(
        self, db_session, normal_user, fake_redis, notices
    ):
        from app.tasks.transcription import cpu_task

        media_file, task_id = _run(db_session, normal_user)

        with patch(
            "app.services.minio_service.download_temp_audio",
            _cancel_then_raise(db_session, media_file, task_id),
        ):
            result = cpu_task.transcribe_cpu_task.run(_context(media_file, task_id))

        assert_cancelled_payload(result, media_file, task_id)
        assert_cancelled(db_session, media_file, task_id, notices)

    def test_postprocess_failure_after_a_cancel(self, db_session, normal_user, fake_redis, notices):
        from app.tasks.transcription import postprocess

        media_file, task_id = _run(db_session, normal_user)
        payload = {
            **_context(media_file, task_id),
            "status": "success",
            "use_native_embeddings": True,
            "native_embeddings": {"SPEAKER_00": [0.1]},
            "speaker_mapping": {},
        }

        with (
            patch.object(
                postprocess,
                "_process_native_embeddings",
                _cancel_then_raise(db_session, media_file, task_id, "embedding store failed"),
            ),
            patch.object(postprocess, "_cleanup_temp"),
        ):
            result = postprocess.finalize_transcription.run(payload)

        assert_cancelled_payload(result, media_file, task_id)
        assert_cancelled(db_session, media_file, task_id, notices)


# =============================================================================
# The chain's safety net
# =============================================================================
class TestThePipelineErrorHandler:
    def test_it_finalizes_a_cancelled_run_as_cancelled(
        self, db_session, normal_user, fake_redis, notices
    ):
        """An exception that escaped a stage's own handler still reaches ``link_error``."""
        from app.tasks.transcription.dispatch import on_pipeline_error

        media_file, task_id = _run(db_session, normal_user)
        _cancel(db_session, media_file, task_id)

        on_pipeline_error(str(media_file.uuid), task_id)

        assert_cancelled(db_session, media_file, task_id, notices)

    def test_it_never_overwrites_a_cancelled_file_with_error(
        self, db_session, normal_user, fake_redis, notices
    ):
        from app.tasks.transcription.dispatch import on_pipeline_error

        media_file, task_id = _run(db_session, normal_user)
        media_file.status = FileStatus.CANCELLED
        media_file.active_task_id = None
        task = db_session.query(Task).filter(Task.id == task_id).one()
        task.status = "failed"
        task.error_message = CANCELLED_BY_USER
        db_session.commit()

        on_pipeline_error(str(media_file.uuid), task_id)

        db_session.expire_all()
        stored = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
        assert stored.status == FileStatus.CANCELLED
        assert notices["error"] == []


# =============================================================================
# Control: without a cancel, nothing changed
# =============================================================================
class TestWithoutACancel:
    def test_a_permanent_failure_still_fails_the_file(
        self, db_session, normal_user, fake_redis, notices
    ):
        from app.services.transcription_retry import RunOutcome
        from app.services.transcription_retry import finish_failed_run

        media_file, task_id = _run(db_session, normal_user)
        failure = ErrorCategorizationService.classify_failure(
            "This file appears to be corrupted or is not a valid audio/video file."
        )

        assert finish_failed_run(task_id, media_file.id, failure) == RunOutcome.FAILED

        db_session.expire_all()
        stored = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
        assert stored.status == FileStatus.ERROR
        assert stored.error_category == ErrorCategory.INVALID_MEDIA.value
        assert notices["error"] == [failure.user_message]

    def test_the_gpu_stage_still_raises(self, db_session, normal_user, fake_redis, notices):
        from app.tasks.transcription import core

        media_file, task_id = _run(db_session, normal_user)

        def _fail(*_a, **_k):
            raise _StorageFetchError(
                "This file appears to be corrupted or is not a valid audio/video file."
            )

        with patch.object(core, "_resolve_asr_provider_or_none", _fail):
            with pytest.raises(_StorageFetchError):
                core.transcribe_gpu_task.run(_context(media_file, task_id))

        db_session.expire_all()
        stored = db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one()
        assert stored.status == FileStatus.ERROR
        assert len(notices["error"]) == 1
