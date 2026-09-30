"""CUDA error classification and the poisoned-context exit (issue #1081).

Observed on a 24 GB card at thread concurrency 4: one task failed with
``CUDA failed with error out of memory`` and the NEXT task on the same worker failed with
``parallel_for failed: cudaErrorInvalidDevice: invalid device ordinal``. The OOM is
recoverable (free caches, smaller batch). The second error is not: the process's CUDA context
is unusable, so the worker must stop taking work and let its supervisor restart it.
"""

from __future__ import annotations

import signal

import pytest

from app.core import worker_shutdown as ws
from app.transcription import cuda_health

CT2_OOM = RuntimeError("CUDA failed with error out of memory")
TORCH_OOM_MSG = "CUDA out of memory. Tried to allocate 2.44 GiB."
INVALID_ORDINAL = RuntimeError(
    "parallel_for failed: cudaErrorInvalidDevice: invalid device ordinal"
)
ILLEGAL_ADDRESS = RuntimeError("CUDA error: an illegal memory access was encountered")


class OutOfMemoryError(RuntimeError):
    """Same class name as ``torch.cuda.OutOfMemoryError``; classification must not import
    torch, so it matches on the name."""


@pytest.fixture(autouse=True)
def _clean_state():
    was_set = ws._SHUTDOWN.is_set()
    ws._SHUTDOWN.clear()
    cuda_health.reset_for_tests()
    yield
    cuda_health.reset_for_tests()
    (ws._SHUTDOWN.set() if was_set else ws._SHUTDOWN.clear())


class TestClassification:
    @pytest.mark.parametrize(
        "exc",
        [CT2_OOM, RuntimeError(TORCH_OOM_MSG), OutOfMemoryError("boom")],
        ids=["ctranslate2", "torch-message", "torch-type"],
    )
    def test_oom_is_recognised(self, exc):
        assert cuda_health.is_cuda_oom(exc)
        assert not cuda_health.is_context_poisoned_error(exc)

    @pytest.mark.parametrize("exc", [INVALID_ORDINAL, ILLEGAL_ADDRESS], ids=["ordinal", "illegal"])
    def test_a_broken_context_is_recognised(self, exc):
        assert cuda_health.is_context_poisoned_error(exc)
        assert not cuda_health.is_cuda_oom(exc)

    def test_the_cause_chain_is_searched(self):
        try:
            try:
                raise ILLEGAL_ADDRESS
            except RuntimeError as inner:
                raise ValueError("diarization failed") from inner
        except ValueError as outer:
            assert cuda_health.is_context_poisoned_error(outer)

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("Shared-volume WAV missing or unreadable"),
            ValueError("bad input"),
            MemoryError(),
        ],
        ids=["runtime", "value", "host-oom"],
    )
    def test_ordinary_errors_are_neither(self, exc):
        assert not cuda_health.is_cuda_oom(exc)
        assert not cuda_health.is_context_poisoned_error(exc)


class TestPoisonedExit:
    def test_marking_poisoned_stops_new_work_and_signals_the_process_once(self, monkeypatch):
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(cuda_health.os, "kill", lambda pid, sig: sent.append((pid, sig)))

        cuda_health.mark_context_poisoned("invalid device ordinal")
        cuda_health.mark_context_poisoned("illegal memory access")

        assert cuda_health.context_poisoned()
        # Other threads on this worker stand down at their next checkpoint and requeue.
        assert ws.shutdown_requested()
        assert sent == [(cuda_health.os.getpid(), signal.SIGTERM)]

    def test_nothing_is_signalled_while_healthy(self, monkeypatch):
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(cuda_health.os, "kill", lambda pid, sig: sent.append((pid, sig)))
        assert not cuda_health.context_poisoned()
        assert sent == []
        assert not ws.shutdown_requested()
