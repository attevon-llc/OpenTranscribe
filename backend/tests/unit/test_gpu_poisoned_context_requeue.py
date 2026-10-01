"""The task layer's answer to a broken CUDA context (issue #1081).

When a GPU task fails with a context-poisoning CUDA error (``invalid device ordinal``,
``illegal memory access``, ...), the file is not at fault: the worker is. The task must be
requeued for a healthy worker rather than failed, and this worker must exit so its supervisor
restarts it. A poison message that breaks every worker it lands on must not loop forever, so
requeues per task are capped.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from celery.exceptions import Reject

POISONED = RuntimeError("parallel_for failed: cudaErrorInvalidDevice: invalid device ordinal")


@pytest.fixture
def core():
    from app.tasks.transcription import core as core_mod

    return core_mod


@pytest.fixture
def context():
    from app.tasks.transcription import context as context_mod

    return context_mod


def test_a_poisoned_context_requeues_and_marks_the_worker(core, context):
    with (
        patch.object(core, "_handle_transcription_failure") as failed,
        patch.object(core, "_cleanup_wav_quietly") as cleanup,
        patch.object(context, "_poisoned_requeue_allowed", return_value=True),
        patch.object(context.cuda_health, "cuda_context_healthy", return_value=False),
        patch.object(context.cuda_health, "mark_context_poisoned") as mark,
    ):
        with pytest.raises(Reject) as raised:
            core._finish_failed_or_aborted(MagicMock(), "task-1", "file-1", "wav-path", POISONED)

    assert raised.value.requeue is True
    mark.assert_called_once()
    failed.assert_not_called()
    # The redelivered attempt needs the preprocessed WAV.
    cleanup.assert_not_called()


def test_an_oom_that_survived_backoff_still_fails_the_file(core, context):
    """The control: only a broken context is the worker's fault. A plain OOM after the
    stage's own backoff is a real failure of this attempt."""
    oom = RuntimeError("CUDA failed with error out of memory")
    with (
        patch.object(core, "_handle_transcription_failure") as failed,
        patch.object(core, "_cleanup_wav_quietly"),
        patch.object(context.cuda_health, "mark_context_poisoned") as mark,
    ):
        with pytest.raises(RuntimeError) as raised:
            core._finish_failed_or_aborted(MagicMock(), "task-2", "file-2", "", oom)

    assert raised.value is oom
    failed.assert_called_once()
    mark.assert_not_called()


def test_past_the_requeue_cap_the_file_fails_but_the_worker_still_exits(core, context):
    with (
        patch.object(core, "_handle_transcription_failure") as failed,
        patch.object(core, "_cleanup_wav_quietly"),
        patch.object(context, "_poisoned_requeue_allowed", return_value=False),
        patch.object(context.cuda_health, "cuda_context_healthy", return_value=False),
        patch.object(context.cuda_health, "mark_context_poisoned") as mark,
    ):
        with pytest.raises(RuntimeError):
            core._finish_failed_or_aborted(MagicMock(), "task-3", "file-3", "", POISONED)

    failed.assert_called_once()
    mark.assert_called_once()


def test_a_fatal_looking_message_on_a_healthy_context_is_an_ordinary_failure(core, context):
    """The probe is what decides. Taking a healthy worker out of service would requeue every
    in-flight task on it for nothing."""
    with (
        patch.object(core, "_handle_transcription_failure") as failed,
        patch.object(core, "_cleanup_wav_quietly"),
        patch.object(context.cuda_health, "cuda_context_healthy", return_value=True),
        patch.object(context.cuda_health, "mark_context_poisoned") as mark,
    ):
        with pytest.raises(RuntimeError) as raised:
            core._finish_failed_or_aborted(MagicMock(), "task-4", "file-4", "", POISONED)

    assert raised.value is POISONED
    failed.assert_called_once()
    mark.assert_not_called()


def test_the_requeue_cap_counts_per_task(context, monkeypatch):
    monkeypatch.setenv("GPU_POISONED_MAX_REQUEUES", "2")
    counts: dict[str, int] = {}

    class _Redis:
        def incr(self, key):
            counts[key] = counts.get(key, 0) + 1
            return counts[key]

        def expire(self, key, ttl):
            return True

    monkeypatch.setattr(context, "_redis_client", lambda: _Redis())
    assert context._poisoned_requeue_allowed("t1")
    assert context._poisoned_requeue_allowed("t1")
    assert not context._poisoned_requeue_allowed("t1")
    assert context._poisoned_requeue_allowed("t2")


def test_the_diarize_task_routes_a_failure_through_the_same_helper():
    """diarize_gpu_task (the split path) runs GPU work as well and must answer the same way.

    The helper's own behaviour is pinned above; this pins that the split path calls it, with
    the real exception, before its failure handler.
    """
    from app.tasks.transcription import diarize_task

    def _raise_poisoned(_where):
        raise POISONED

    def _requeue(task_id, file_uuid, exc, *, stage):
        assert exc is POISONED
        raise Reject(requeue=True)

    preprocess_context = {
        "task_id": "task-9",
        "file_uuid": "file-9",
        "file_id": 9,
        "user_id": 1,
        "storage_path": "x",
        "file_name": "x.wav",
        "content_type": "audio/wav",
    }
    with (
        patch.object(diarize_task, "superseded_or_none", return_value=None),
        patch.object(diarize_task, "stand_down_if_requested", _raise_poisoned),
        patch.object(diarize_task, "requeue_if_context_poisoned", side_effect=_requeue) as helper,
        patch.object(diarize_task, "_handle_transcription_failure") as failed,
    ):
        with pytest.raises(Reject) as raised:
            diarize_task.diarize_gpu_task.run({}, preprocess_context)

    assert raised.value.requeue is True
    helper.assert_called_once()
    failed.assert_not_called()
