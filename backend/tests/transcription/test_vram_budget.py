"""VRAM admission for GPU stages (issue #1081).

Several GPU stages share one card inside one ``--pool=threads`` worker. A free-memory check
before starting a stage is racy (N threads read the same free figure and all start), so each
stage RESERVES its estimate from a per-process budget and waits, bounded, when it does not fit.

These tests pin the reservation semantics without a GPU: reserve/wait/timeout/release, the
release on an exception inside the block, the clamp that lets an oversized stage run alone,
FIFO order, the per-stage estimates and their env overrides, and how the capacity is derived
from the device.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from typing import Any

import pytest

from app.core.worker_shutdown import TranscriptionAbortedError
from app.transcription import vram_budget
from app.transcription.vram_budget import VramAdmissionTimeoutError
from app.transcription.vram_budget import VramBudget


@pytest.fixture(autouse=True)
def _reset_budget(monkeypatch):
    for name in (
        "GPU_VRAM_ADMISSION",
        "GPU_VRAM_BUDGET_FRACTION",
        "GPU_VRAM_ADMISSION_TIMEOUT_S",
        "GPU_STAGE_VRAM_MB_ASR",
        "GPU_STAGE_VRAM_MB_DIARIZATION",
    ):
        monkeypatch.delenv(name, raising=False)
    vram_budget.reset_for_tests()
    yield
    vram_budget.reset_for_tests()


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached in time")


class TestReservation:
    def test_a_reservation_that_fits_is_admitted_and_released(self):
        budget = VramBudget(capacity_mb=8000)
        with budget.reserve("asr", 3000, timeout_s=1) as granted:
            assert granted == 3000
            assert budget.reserved_mb == 3000
        assert budget.reserved_mb == 0

    def test_a_second_reservation_waits_until_the_first_releases(self):
        budget = VramBudget(capacity_mb=5000)
        admitted = threading.Event()
        release_first = threading.Event()

        def first() -> None:
            with budget.reserve("asr", 4000, timeout_s=5):
                release_first.wait(5)

        def second() -> None:
            with budget.reserve("diarization", 2000, timeout_s=5):
                admitted.set()

        t1 = threading.Thread(target=first)
        t1.start()
        _wait_until(lambda: budget.reserved_mb == 4000)
        t2 = threading.Thread(target=second)
        t2.start()
        _wait_until(lambda: budget.waiting == 1)
        assert not admitted.is_set(), "4000 + 2000 exceeds 5000 MB; the second must wait"

        release_first.set()
        t1.join(5)
        t2.join(5)
        assert admitted.is_set()
        assert budget.reserved_mb == 0

    def test_a_wait_past_the_timeout_raises_and_leaves_no_trace(self):
        budget = VramBudget(capacity_mb=5000)
        with budget.reserve("asr", 4000, timeout_s=1):
            with pytest.raises(VramAdmissionTimeoutError):
                with budget.reserve("diarization", 2000, timeout_s=0.05):
                    pytest.fail("must not be admitted")
            assert budget.reserved_mb == 4000
            assert budget.waiting == 0
        # The timed-out ticket must not block the queue for the next caller.
        with budget.reserve("diarization", 2000, timeout_s=0.05):
            assert budget.reserved_mb == 2000

    def test_the_timeout_is_a_requeueable_abort(self):
        """The task layer already turns TranscriptionAbortedError into Reject(requeue=True).
        A timed-out admission is interrupted work, not broken work, so it rides that path."""
        assert issubclass(VramAdmissionTimeoutError, TranscriptionAbortedError)

    def test_an_exception_inside_the_block_still_releases(self):
        budget = VramBudget(capacity_mb=8000)
        with pytest.raises(RuntimeError, match="boom"):
            with budget.reserve("asr", 3000, timeout_s=1):
                raise RuntimeError("boom")
        assert budget.reserved_mb == 0

    def test_an_oversized_request_is_clamped_so_it_can_run_alone(self):
        budget = VramBudget(capacity_mb=4000)
        with budget.reserve("asr", 9000, timeout_s=1) as granted:
            assert granted == 4000
            assert budget.reserved_mb == 4000

    def test_waiters_are_admitted_in_arrival_order(self):
        """A large stage must not be starved by a stream of small ones that would fit."""
        budget = VramBudget(capacity_mb=5000)
        order: list[str] = []
        hold = threading.Event()

        def holder() -> None:
            with budget.reserve("hold", 3000, timeout_s=5):
                hold.wait(5)

        def big() -> None:
            with budget.reserve("big", 4000, timeout_s=5):
                order.append("big")

        def small() -> None:
            with budget.reserve("small", 1000, timeout_s=5):
                order.append("small")

        th = threading.Thread(target=holder)
        th.start()
        _wait_until(lambda: budget.reserved_mb == 3000)
        tb = threading.Thread(target=big)
        tb.start()
        _wait_until(lambda: budget.waiting == 1)
        ts = threading.Thread(target=small)
        ts.start()
        _wait_until(lambda: budget.waiting == 2)
        # 3000 + 1000 fits, but "big" arrived first and must go first: both are still queued.
        assert order == []
        assert budget.reserved_mb == 3000
        hold.set()
        for t in (th, tb, ts):
            t.join(5)
        assert order[0] == "big"


class TestStageEstimates:
    def test_the_asr_estimate_grows_with_the_batch_size(self):
        assert vram_budget.stage_estimate_mb("asr", batch_size=16) > vram_budget.stage_estimate_mb(
            "asr", batch_size=8
        )

    def test_an_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("GPU_STAGE_VRAM_MB_ASR", "1234")
        assert vram_budget.stage_estimate_mb("asr", batch_size=16) == 1234

    def test_an_invalid_override_falls_back_to_the_default(self, monkeypatch):
        default = vram_budget.stage_estimate_mb("diarization")
        monkeypatch.setenv("GPU_STAGE_VRAM_MB_DIARIZATION", "lots")
        assert vram_budget.stage_estimate_mb("diarization") == default

    def test_every_stage_has_a_positive_default(self):
        for stage in ("asr", "diarization"):
            assert vram_budget.stage_estimate_mb(stage, batch_size=16) > 0


def _fake_torch(*, total_mb: int, free_mb: int, available: bool = True) -> Any:
    class _Cuda:
        @staticmethod
        def is_available() -> bool:
            return available

        @staticmethod
        def mem_get_info(_index: int = 0) -> tuple[int, int]:
            return free_mb * 1024**2, total_mb * 1024**2

    module = types.ModuleType("torch")
    module.cuda = _Cuda()  # type: ignore[attr-defined]
    return module


class TestCapacityFromDevice:
    def test_capacity_is_the_budget_fraction_minus_what_is_already_used(self, monkeypatch):
        # 24000 MB card, 5000 MB in use after the models were preloaded.
        monkeypatch.setitem(sys.modules, "torch", _fake_torch(total_mb=24000, free_mb=19000))
        budget = vram_budget.configure_from_device(0)
        assert budget is not None
        assert budget.capacity_mb == int(24000 * 0.85) - 5000

    def test_the_fraction_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv("GPU_VRAM_BUDGET_FRACTION", "0.5")
        monkeypatch.setitem(sys.modules, "torch", _fake_torch(total_mb=24000, free_mb=20000))
        budget = vram_budget.configure_from_device(0)
        assert budget is not None
        assert budget.capacity_mb == 12000 - 4000

    def test_a_card_already_over_budget_still_admits_one_stage_at_a_time(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _fake_torch(total_mb=12000, free_mb=1000))
        budget = vram_budget.configure_from_device(0)
        assert budget is not None
        assert budget.capacity_mb == vram_budget.MIN_CAPACITY_MB

    def test_no_cuda_means_no_budget(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "torch", _fake_torch(total_mb=0, free_mb=0, available=False)
        )
        assert vram_budget.configure_from_device(0) is None


class TestAdmit:
    def test_cpu_stages_are_never_gated(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _fake_torch(total_mb=24000, free_mb=19000))
        with vram_budget.admit("asr", device="cpu", batch_size=16) as granted:
            assert granted == 0
        assert vram_budget.current_budget() is None

    def test_admission_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("GPU_VRAM_ADMISSION", "false")
        monkeypatch.setitem(sys.modules, "torch", _fake_torch(total_mb=24000, free_mb=19000))
        with vram_budget.admit("asr", device="cuda", batch_size=16) as granted:
            assert granted == 0

    def test_a_cuda_stage_reserves_its_estimate(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "torch", _fake_torch(total_mb=48000, free_mb=44000))
        expected = vram_budget.stage_estimate_mb("asr", batch_size=8)
        with vram_budget.admit("asr", device="cuda", batch_size=8) as granted:
            assert granted == expected
            budget = vram_budget.current_budget()
            assert budget is not None
            assert budget.reserved_mb == expected
        assert budget.reserved_mb == 0
