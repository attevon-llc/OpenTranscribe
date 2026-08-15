"""Tests for transcription pipeline dispatch: error handling, batch dispatch, and helpers.

**Written against real rows, deliberately.** The previous version patched
``update_media_file_status``, ``update_task_status`` and ``send_error_notification`` — the
very effects ``on_pipeline_error`` exists to produce — and then asserted
``mock.assert_called_once_with(...)``. Every test therefore proved only that the handler
called the functions it was mocked into calling: ``on_pipeline_error`` could have left a file
stuck in ``processing`` forever, or written the wrong file's id, and 13 of 15 tests stayed
green (issue #431). Each test here now creates a real ``MediaFile``/``Task`` and asserts on
the row afterwards, which is the thing the handler is for.

Only genuinely out-of-process seams are patched, once, in the ``dispatch_seams`` fixture:

* ``session_scope`` — the handler opens its OWN session, which under the savepoint harness
  cannot see the uncommitted fixture rows. Pointing it at ``db_session`` is what makes real
  rows observable at all; it is not a stand-in for any behaviour under test.
* ``cleanup_temp_audio`` (MinIO), ``send_error_notification`` (Redis/WebSocket) and
  ``get_redis`` — network calls. Their *arguments* are part of the contract, so the mocks are
  exposed for assertion, always alongside an assertion on real state.
* ``group`` — ``group(...).apply_async()`` is the only line that needs a live broker. The
  Celery ``chain`` it wraps is built for real: construction touches no broker, and building
  it for real is what proves the signatures are well-formed.
* ``_log_oom_diagnostics`` — **must stay patched.** It imports torch and calls
  ``torch.cuda.mem_get_info(i)`` for EVERY device, which creates a CUDA context on each one.
  This host has three GPUs and two of them are reserved for unrelated work, so a unit test
  may not touch them. Its output is log lines only; the OOM behaviour that matters is the
  user-facing message stored on the task row, which these tests assert directly instead of
  asserting that a logger was called.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task

_DISPATCH = "app.tasks.transcription.dispatch"
_SESSION_SCOPE = f"{_DISPATCH}.session_scope"
_GROUP = f"{_DISPATCH}.group"

# Lazy imports inside on_pipeline_error / dispatch_batch_transcription — patch at source.
_CLEANUP_TEMP = "app.services.minio_service.cleanup_temp_audio"
_SEND_ERROR = "app.tasks.transcription.notifications.send_error_notification"
_GET_REDIS = "app.core.redis.get_redis"
_LOG_OOM = f"{_DISPATCH}._log_oom_diagnostics"

_BATCH_ID = "batch-under-test"
_GENERIC_ERROR = "Transcription pipeline failed unexpectedly"


class TestGetPipelineErrorMessage:
    """Tests for _get_pipeline_error_message()."""

    def test_oom_returns_gpu_message(self):
        from app.tasks.transcription.dispatch import _get_pipeline_error_message

        result = _get_pipeline_error_message("CUDA out of memory", is_oom=True)
        assert "GPU ran out of memory" in result

    def test_non_oom_returns_generic_message(self):
        from app.tasks.transcription.dispatch import _get_pipeline_error_message

        result = _get_pipeline_error_message("some traceback", is_oom=False)
        assert result == _GENERIC_ERROR

    def test_oom_flag_takes_precedence(self):
        from app.tasks.transcription.dispatch import _get_pipeline_error_message

        # Even with empty error message, is_oom flag drives the output
        result = _get_pipeline_error_message("", is_oom=True)
        assert "GPU ran out of memory" in result

    def test_oom_text_without_flag_returns_generic(self):
        from app.tasks.transcription.dispatch import _get_pipeline_error_message

        # Error text mentions OOM but flag is False — flag is what matters
        result = _get_pipeline_error_message("CUDA out of memory", is_oom=False)
        assert result == _GENERIC_ERROR


@contextmanager
def _yield_session(db):
    """Stand-in for ``session_scope()`` that hands out the test's savepoint session."""
    yield db


@pytest.fixture
def dispatch_seams(db_session):
    """Patch the out-of-process seams and expose their mocks. See the module docstring."""
    with (
        patch(_SESSION_SCOPE, lambda: _yield_session(db_session)),
        patch(_CLEANUP_TEMP) as cleanup_temp,
        patch(_SEND_ERROR) as send_error,
        patch(_GROUP) as group,
        patch(_GET_REDIS) as get_redis,
        patch(_LOG_OOM) as log_oom,
    ):
        group.return_value.apply_async.return_value.id = _BATCH_ID
        yield SimpleNamespace(
            cleanup_temp=cleanup_temp,
            send_error=send_error,
            group=group,
            get_redis=get_redis,
            log_oom=log_oom,
        )


@pytest.fixture
def make_media_file(db_session, normal_user):
    """Factory for a real MediaFile row owned by ``normal_user``."""

    def _make(status: FileStatus = FileStatus.PROCESSING) -> MediaFile:
        media_file = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename="dispatch_test.mp3",
            storage_path="dispatch/dispatch_test.mp3",
            file_size=2048,
            content_type="audio/mpeg",
            status=status,
        )
        db_session.add(media_file)
        db_session.commit()
        db_session.refresh(media_file)
        return media_file

    return _make


@pytest.fixture
def make_task(db_session, normal_user):
    """Factory for a real Task row, as the pipeline's own dispatch would have created it."""

    def _make(media_file: MediaFile, status: str = "in_progress", error_message: str = "") -> Task:
        task = Task(
            id=str(uuid_pkg.uuid4()),
            user_id=normal_user.id,
            media_file_id=media_file.id,
            task_type="transcription",
            status=status,
            progress=0.5,
            error_message=error_message,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        return task

    return _make


class TestOnPipelineError:
    """``on_pipeline_error()`` is the safety net: after it runs, nothing may still look busy."""

    @staticmethod
    def _run(media_file: MediaFile, task_id: str) -> None:
        from app.tasks.transcription.dispatch import on_pipeline_error

        on_pipeline_error(str(media_file.uuid), task_id)

    def test_marks_file_and_task_error(
        self, db_session, dispatch_seams, make_media_file, make_task, normal_user
    ):
        """The whole point of the handler: a file mid-pipeline must not stay 'processing'."""
        media_file = make_media_file(FileStatus.PROCESSING)
        task = make_task(media_file)

        self._run(media_file, task.id)

        db_session.refresh(media_file)
        db_session.refresh(task)
        assert media_file.status == FileStatus.ERROR
        assert task.status == "failed"
        assert task.error_message == _GENERIC_ERROR
        assert task.completed_at is not None
        dispatch_seams.send_error.assert_called_once_with(
            normal_user.id, media_file.id, _GENERIC_ERROR
        )

    @pytest.mark.parametrize(
        "raw_error",
        [
            "CUDA out of memory in allocator",
            "OutOfMemoryError: GPU",
        ],
    )
    def test_oom_is_translated_for_the_user(
        self, db_session, dispatch_seams, make_media_file, make_task, raw_error
    ):
        """OOM detection is observable in the STORED message, not in a mocked log call."""
        media_file = make_media_file(FileStatus.PROCESSING)
        task = make_task(media_file, error_message=raw_error)

        self._run(media_file, task.id)

        db_session.refresh(task)
        assert "GPU ran out of memory" in (task.error_message or "")
        assert task.status == "failed"
        # The VRAM dump is the operator half of the same decision; see the module docstring
        # for why it cannot be allowed to run for real.
        dispatch_seams.log_oom.assert_called_once()

    def test_a_non_oom_failure_does_not_claim_it_was_oom(
        self, db_session, dispatch_seams, make_media_file, make_task
    ):
        """Control for the pair above: same code path, opposite classification."""
        media_file = make_media_file(FileStatus.PROCESSING)
        task = make_task(media_file, error_message="ffmpeg: Invalid data found")

        self._run(media_file, task.id)

        db_session.refresh(task)
        assert task.error_message == _GENERIC_ERROR
        dispatch_seams.log_oom.assert_not_called()

    def test_postprocess_failure_keeps_completed(
        self, db_session, dispatch_seams, make_media_file, make_task
    ):
        """A postprocess failure arrives with the task already completed and segments saved.

        The transcript is intact, so nothing may be downgraded and the user must not be told
        the job failed.
        """
        media_file = make_media_file(FileStatus.COMPLETED)
        task = make_task(media_file, status="completed", error_message="postprocess boom")

        self._run(media_file, task.id)

        db_session.refresh(media_file)
        db_session.refresh(task)
        assert media_file.status == FileStatus.COMPLETED
        assert task.status == "completed"
        assert task.error_message == "postprocess boom"
        dispatch_seams.send_error.assert_not_called()

    def test_already_errored_file_is_left_alone_but_task_is_finalized(
        self, db_session, dispatch_seams, make_media_file, make_task
    ):
        """The failing task's own handler usually got there first; this must be idempotent."""
        media_file = make_media_file(FileStatus.ERROR)
        task = make_task(media_file)

        self._run(media_file, task.id)

        db_session.refresh(media_file)
        db_session.refresh(task)
        assert media_file.status == FileStatus.ERROR
        assert task.status == "failed"

    def test_already_failed_task_keeps_its_original_message(
        self, db_session, dispatch_seams, make_media_file, make_task
    ):
        """A specific diagnosis already recorded must not be overwritten by the generic one."""
        media_file = make_media_file(FileStatus.PROCESSING)
        task = make_task(media_file, status="failed", error_message="ffmpeg exited 1")

        self._run(media_file, task.id)

        db_session.refresh(media_file)
        db_session.refresh(task)
        assert media_file.status == FileStatus.ERROR
        assert task.status == "failed"
        assert task.error_message == "ffmpeg exited 1"
        dispatch_seams.send_error.assert_not_called()

    def test_missing_task_row_still_errors_the_file(
        self, db_session, dispatch_seams, make_media_file
    ):
        """A crash before the task record existed still must not leave the file processing."""
        media_file = make_media_file(FileStatus.PROCESSING)

        self._run(media_file, str(uuid_pkg.uuid4()))

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR
        dispatch_seams.send_error.assert_not_called()

    def test_temp_audio_is_cleaned_up(self, db_session, dispatch_seams, make_media_file, make_task):
        """Temp audio is the handler's other job — an orphan costs storage forever."""
        media_file = make_media_file(FileStatus.PROCESSING)
        task = make_task(media_file)

        self._run(media_file, task.id)

        db_session.refresh(media_file)
        assert media_file.status == FileStatus.ERROR
        dispatch_seams.cleanup_temp.assert_called_once_with(str(media_file.uuid))

    def test_db_failure_is_swallowed_and_changes_nothing(
        self, db_session, dispatch_seams, make_media_file, make_task
    ):
        """The handler runs as a link_error callback: raising would lose the original error."""
        media_file = make_media_file(FileStatus.PROCESSING)
        task = make_task(media_file)

        def _broken_scope():
            raise RuntimeError("DB down")

        with patch(_SESSION_SCOPE, _broken_scope):
            self._run(media_file, task.id)

        db_session.refresh(media_file)
        db_session.refresh(task)
        assert media_file.status == FileStatus.PROCESSING
        assert task.status == "in_progress"


class TestDispatchBatchTranscription:
    """``dispatch_batch_transcription()`` must create real task records, skip real gaps."""

    @staticmethod
    def _dispatch(file_uuids: list[str]) -> dict:
        from app.tasks.transcription.dispatch import dispatch_batch_transcription

        return dispatch_batch_transcription(file_uuids, gpu_queue="gpu")

    def test_returns_dict_format_and_records_every_task(
        self, db_session, dispatch_seams, make_media_file
    ):
        first, second = make_media_file(FileStatus.PENDING), make_media_file(FileStatus.PENDING)

        result = self._dispatch([str(first.uuid), str(second.uuid)])

        assert result["batch_id"] == _BATCH_ID
        assert len(result["task_ids"]) == 2
        # The mocked group proves nothing about the DB; these rows are what the UI polls.
        for file_obj, task_id in zip([first, second], result["task_ids"], strict=True):
            db_session.refresh(file_obj)
            assert file_obj.status == FileStatus.PROCESSING
            task = db_session.query(Task).filter(Task.id == task_id).one()
            assert task.media_file_id == file_obj.id
            assert task.status == "in_progress"
            assert task.task_type == "transcription"

    def test_missing_file_skipped(self, db_session, dispatch_seams, make_media_file):
        """A UUID with no row is skipped without aborting the rest of the batch."""
        first, second = make_media_file(FileStatus.PENDING), make_media_file(FileStatus.PENDING)
        absent = str(uuid_pkg.uuid4())

        result = self._dispatch([str(first.uuid), absent, str(second.uuid)])

        assert len(result["task_ids"]) == 2
        recorded = {
            t.media_file_id
            for t in db_session.query(Task).filter(Task.id.in_(result["task_ids"])).all()
        }
        assert recorded == {first.id, second.id}

    def test_all_missing_returns_empty(self, db_session, dispatch_seams):
        absent = [str(uuid_pkg.uuid4()), str(uuid_pkg.uuid4())]

        result = self._dispatch(absent)

        assert result == {"batch_id": None, "task_ids": []}
        # Nothing dispatched means nothing queued — an empty group would hang a worker.
        dispatch_seams.group.assert_not_called()

    def test_batch_metadata_stored_in_redis(self, db_session, dispatch_seams, make_media_file):
        media_file = make_media_file(FileStatus.PENDING)

        result = self._dispatch([str(media_file.uuid)])

        assert len(result["task_ids"]) == 1
        redis_set = dispatch_seams.get_redis.return_value.set
        redis_set.assert_called_once()
        key, payload = redis_set.call_args[0]
        assert key == f"batch:{_BATCH_ID}"
        assert redis_set.call_args[1]["ex"] == 86400  # 24h TTL
        assert json.loads(payload) == {
            "file_uuids": [str(media_file.uuid)],
            "task_ids": result["task_ids"],
        }

    def test_redis_failure_non_fatal(self, db_session, dispatch_seams, make_media_file):
        """Batch tracking is a convenience; losing it must not lose the transcriptions."""
        media_file = make_media_file(FileStatus.PENDING)
        dispatch_seams.get_redis.side_effect = RuntimeError("Redis down")

        result = self._dispatch([str(media_file.uuid)])

        assert result["batch_id"] == _BATCH_ID
        assert len(result["task_ids"]) == 1
        db_session.refresh(media_file)
        assert media_file.status == FileStatus.PROCESSING
        assert db_session.query(Task).filter(Task.id == result["task_ids"][0]).one() is not None
