"""Host-RAM admission for GPU task concurrency (issue #1073).

Evidence from a multi-GPU load test (2,100+ files): hosts with 16 GB of RAM were OOM-killed
repeatedly at 8+ concurrent GPU tasks and ran at about 6, while 32 GB hosts stayed GPU-bound up
to 12. VRAM admission (#1081) did not prevent those kills: per-task HOST memory times
concurrency was the binding limit, and an explicitly configured concurrency was never checked
against it (the host-aware sizing of #1112 only applies to ``GPU_CONCURRENT_REQUESTS=auto``).

The guard: at worker startup, ``effective = min(configured, host cap)`` with
``host cap = (budget - GPU_HOST_BASELINE_MB) // GPU_PER_TASK_HOST_MB`` and the budget read from
the cgroup limit (v2 ``memory.max``, then v1) or ``/proc/meminfo``; the decision is logged and
exported; each GPU task then holds one of ``effective`` slots for its whole body.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from datetime import UTC
from datetime import datetime
from unittest.mock import patch

import pytest

from app.core.worker_shutdown import TranscriptionAbortedError
from app.transcription import config as config_mod
from app.transcription import host_memory_admission as ham
from app.transcription.host_memory_admission import HostMemoryAdmissionTimeoutError


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (
        "GPU_HOST_BASELINE_MB",
        "GPU_PER_TASK_HOST_MB",
        "GPU_HOST_MEMORY_ADMISSION",
        "GPU_VRAM_ADMISSION_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    ham.reset_for_tests()
    yield
    ham.reset_for_tests()


@pytest.fixture
def host(monkeypatch, tmp_path):
    """Point the host-memory probes at files this test controls."""
    meminfo = tmp_path / "meminfo"
    v2 = tmp_path / "memory.max"
    v1 = tmp_path / "memory.limit_in_bytes"
    monkeypatch.setattr(config_mod, "_PROC_MEMINFO", str(meminfo))
    monkeypatch.setattr(config_mod, "_CGROUP_V2_MEMORY_MAX", str(v2))
    monkeypatch.setattr(config_mod, "_CGROUP_V1_MEMORY_LIMIT", str(v1))

    def _set(*, mem_total_mb: int | None, cgroup_v2: str | None = None) -> None:
        if mem_total_mb is not None:
            meminfo.write_text(f"MemTotal:       {mem_total_mb * 1024} kB\nMemFree: 1 kB\n")
        if cgroup_v2 is not None:
            v2.write_text(cgroup_v2 + "\n")

    return _set


GIB = 1024**3


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached in time")


def _gate() -> ham.SlotGate:
    gate = ham.current_gate()
    assert gate is not None
    return gate


# =============================================================================
# The decision
# =============================================================================
@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("16 GiB container", {"mem_total_mb": 64 * 1024, "cgroup_v2": str(16 * GIB)}),
        ("16 GB VM, no cgroup limit", {"mem_total_mb": 15_700, "cgroup_v2": "max"}),
    ],
)
def test_a_16_gb_host_never_runs_more_than_six(host, label, kwargs):
    host(**kwargs)

    decision = ham.decide(configured=8)

    assert decision.effective <= 6, (label, decision)
    assert decision.effective >= 1


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("32 GiB container", {"mem_total_mb": 128 * 1024, "cgroup_v2": str(32 * GIB)}),
        ("32 GB VM, no cgroup limit", {"mem_total_mb": 31_600, "cgroup_v2": "max"}),
    ],
)
def test_a_32_gb_host_may_reach_twelve(host, label, kwargs):
    host(**kwargs)

    assert ham.decide(configured=12).effective == 12, label


def test_the_configured_value_is_never_raised(host):
    host(mem_total_mb=256 * 1024)

    assert ham.decide(configured=3).effective == 3


def test_the_cgroup_limit_is_preferred_and_named(host):
    host(mem_total_mb=64 * 1024, cgroup_v2=str(16 * GIB))

    decision = ham.decide(configured=12)

    assert decision.budget_mb == 16 * 1024
    assert decision.budget_source == "cgroup"


def test_an_unlimited_cgroup_falls_back_to_meminfo(host):
    host(mem_total_mb=16 * 1024, cgroup_v2="max")

    decision = ham.decide(configured=12)

    assert decision.budget_mb == 16 * 1024
    assert decision.budget_source == "meminfo"


def test_the_estimates_are_env_tunable(host, monkeypatch):
    host(mem_total_mb=64 * 1024, cgroup_v2=str(16 * GIB))
    monkeypatch.setenv("GPU_HOST_BASELINE_MB", "4096")
    monkeypatch.setenv("GPU_PER_TASK_HOST_MB", "4096")

    decision = ham.decide(configured=12)

    assert (decision.reserve_mb, decision.per_task_mb) == (4096, 4096)
    assert decision.effective == (16 * 1024 - 4096) // 4096


def test_an_unreadable_host_keeps_the_configured_value(host):
    host(mem_total_mb=None)

    decision = ham.decide(configured=8)

    assert decision.host_cap is None
    assert decision.effective == 8


def test_a_host_too_small_for_one_task_still_runs_one(host):
    host(mem_total_mb=2048)

    assert ham.decide(configured=4).effective == 1


def test_the_decision_is_logged_with_its_inputs(host, caplog):
    host(mem_total_mb=64 * 1024, cgroup_v2=str(16 * GIB))

    with caplog.at_level(logging.INFO, logger="app.transcription.host_memory_admission"):
        ham.configure(configured=8)

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "configured=8" in text
    assert "effective=6" in text
    assert "cgroup" in text


def test_the_decision_is_exported_as_gauges(host, monkeypatch):
    from app.core import worker_metrics

    recorder = worker_metrics.WorkerTaskMetrics()
    monkeypatch.setattr(worker_metrics, "_metrics", recorder)
    host(mem_total_mb=64 * 1024, cgroup_v2=str(16 * GIB))

    ham.configure(configured=8)

    sample = recorder.registry.get_sample_value
    assert sample("gpu_worker_concurrency_configured", {}) == 8
    assert sample("gpu_worker_concurrency_host_memory_cap", {}) == 6
    assert sample("gpu_worker_concurrency_effective", {}) == 6


# =============================================================================
# The gate
# =============================================================================
def test_tasks_beyond_the_cap_wait_for_a_slot(host):
    host(mem_total_mb=64 * 1024, cgroup_v2=str(GIB * 7))  # (7168 - 2560) // 2048 = 2
    ham.configure(configured=8)
    gate = ham.current_gate()
    assert gate is not None and gate.capacity == 2
    release = threading.Event()
    third_admitted = threading.Event()

    def _hold() -> None:
        with ham.task_slot("gpu_transcribe"):
            release.wait(5)

    def _third() -> None:
        with ham.task_slot("gpu_transcribe"):
            third_admitted.set()

    holders = [threading.Thread(target=_hold) for _ in range(2)]
    for t in holders:
        t.start()
    _wait_until(lambda: gate.in_use == 2)
    waiter = threading.Thread(target=_third)
    waiter.start()
    _wait_until(lambda: gate.waiting == 1)
    assert not third_admitted.is_set()

    release.set()
    waiter.join(5)
    for t in holders:
        t.join(5)
    assert third_admitted.is_set()
    assert gate.in_use == 0


def test_a_slot_wait_that_times_out_is_requeued_not_failed(host, monkeypatch):
    host(mem_total_mb=64 * 1024, cgroup_v2=str(GIB * 5))  # one slot
    monkeypatch.setenv("GPU_VRAM_ADMISSION_TIMEOUT_S", "0.05")
    ham.configure(configured=4)
    release = threading.Event()
    holder = threading.Thread(target=lambda: _hold_until(release))
    holder.start()
    _wait_until(lambda: _gate().in_use == 1)

    with pytest.raises(HostMemoryAdmissionTimeoutError) as raised, ham.task_slot("gpu"):
        pass

    release.set()
    holder.join(5)
    # A TranscriptionAbortedError: the GPU task layer turns it into Reject(requeue=True).
    assert isinstance(raised.value, TranscriptionAbortedError)


def _hold_until(event: threading.Event) -> None:
    with ham.task_slot("gpu"):
        event.wait(5)


def test_the_gate_is_a_no_op_until_configured():
    with ham.task_slot("gpu") as granted:
        assert granted == 0
    assert ham.current_gate() is None


def test_the_gate_can_be_turned_off(host, monkeypatch):
    host(mem_total_mb=64 * 1024, cgroup_v2=str(GIB * 5))
    monkeypatch.setenv("GPU_HOST_MEMORY_ADMISSION", "false")

    ham.configure(configured=4)

    assert ham.current_gate() is None
    with ham.task_slot("gpu") as granted:
        assert granted == 0


# =============================================================================
# Wiring: the GPU task holds a slot for its body
# =============================================================================
class _BodyReachedError(Exception):
    pass


def test_the_gpu_task_holds_a_slot_while_it_runs(host, db_session, normal_user, monkeypatch):
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import Task
    from app.tasks.transcription import core as core_module
    from app.tasks.transcription import run_ownership
    from tests.unit._fake_liveness_redis import install_fake_redis

    install_fake_redis(monkeypatch)

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(run_ownership, "session_scope", _scope)
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        user_id=normal_user.id,
        filename="ham.wav",
        storage_path="ham/ham.wav",
        file_size=1,
        content_type="audio/wav",
        status=FileStatus.PROCESSING,
    )
    db_session.add(media_file)
    db_session.commit()
    run = Task(
        id=f"ham-{uuid.uuid4()}",
        user_id=normal_user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
        created_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.commit()

    host(mem_total_mb=64 * 1024, cgroup_v2=str(GIB * 7))
    ham.configure(configured=2)
    held_during_body: list[int] = []

    def _body(user_id):
        held_during_body.append(_gate().in_use)
        raise _BodyReachedError

    def _reraise(ctx, task_id, file_uuid, wav, exc):
        raise exc

    context = {
        "task_id": run.id,
        "file_uuid": str(media_file.uuid),
        "file_id": media_file.id,
        "user_id": normal_user.id,
        "storage_path": media_file.storage_path,
        "file_name": media_file.filename,
        "content_type": media_file.content_type,
        "diarization_source": "provider",
    }
    with (
        patch.object(core_module, "update_task_status"),
        patch.object(core_module, "_resolve_asr_provider_or_none", _body),
        patch.object(core_module, "_finish_failed_or_aborted", _reraise),
        pytest.raises(_BodyReachedError),
    ):
        core_module.transcribe_gpu_task.run(context)

    assert held_during_body == [1]
    assert _gate().in_use == 0
