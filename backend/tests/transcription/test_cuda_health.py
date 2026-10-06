"""CUDA error classification and the poisoned-context exit (issue #1081).

Observed on a 24 GB card at thread concurrency 4: one task failed with
``CUDA failed with error out of memory`` and the NEXT task on the same worker failed with
``parallel_for failed: cudaErrorInvalidDevice: invalid device ordinal``. The OOM is
recoverable (free caches, smaller batch). The second error is not: the process's CUDA context
is unusable, so the worker must stop taking work and let its supervisor restart it.
"""

from __future__ import annotations

import signal
import sys

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


class _ProbeTorch:
    """Stub torch whose tensor ops fail the first ``fail_times`` calls."""

    def __init__(self, fail_times: int):
        self.calls = 0
        self.fail_times = fail_times

        class _Cuda:
            @staticmethod
            def is_available() -> bool:
                return True

            @staticmethod
            def synchronize(_index: int = 0) -> None:
                return None

        class _T:
            def __add__(self, _other):
                return self

            def sum(self):
                return self

            def item(self) -> float:
                return 2.0

        self.cuda = _Cuda()
        self._T = _T

    def ones(self, *_a, **_k):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("CUDA error: an illegal memory access was encountered")
        return self._T()


class TestContextProbe:
    def test_a_working_context_is_healthy(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _ProbeTorch(fail_times=0))
        assert cuda_health.cuda_context_healthy()

    def test_one_stale_error_is_forgiven(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _ProbeTorch(fail_times=1))
        assert cuda_health.cuda_context_healthy()

    def test_a_sticky_error_is_unhealthy(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _ProbeTorch(fail_times=99))
        assert not cuda_health.cuda_context_healthy()


class TestRecurringContextErrors:
    def test_one_off_errors_spread_out_do_not_count_as_recurring(self):
        window = cuda_health.RECURRING_ERROR_WINDOW_S
        results = [
            cuda_health.recurring_context_error(now=i * (window + 1))
            for i in range(cuda_health.RECURRING_ERROR_LIMIT + 2)
        ]
        assert results == [False] * (cuda_health.RECURRING_ERROR_LIMIT + 2)

    def test_errors_recurring_inside_the_window_do(self):
        results = [
            cuda_health.recurring_context_error(now=float(i))
            for i in range(cuda_health.RECURRING_ERROR_LIMIT)
        ]
        assert results[-1] is True
        assert not any(results[:-1])
