"""Host-RAM admission for GPU task concurrency (issue #1073).

A GPU worker runs ``--pool=threads`` with several transcriptions in flight, and each one holds
its whole decoded file (and, with in-process diarization, more full-length copies) in host
memory. VRAM admission (``vram_budget``) gates the GPU working set, not this. In a multi-GPU
load test, hosts with 16 GB of RAM were OOM-killed repeatedly at 8+ concurrent GPU tasks while
32 GB hosts stayed GPU-bound at 12: per-task host RSS times concurrency was the binding limit,
and nothing checked an explicitly configured concurrency against it (the host-aware sizing in
``TranscriptionConfig._auto_concurrent`` only applies to ``GPU_CONCURRENT_REQUESTS=auto``).

At worker startup :func:`configure` decides::

    host cap  = max(1, (budget - GPU_HOST_BASELINE_MB) // GPU_PER_TASK_HOST_MB)
    effective = min(configured, host cap)

``budget`` is the container's cgroup memory limit (v2 ``memory.max``, else v1) when there is
one, never above ``MemTotal`` from ``/proc/meminfo``. The decision is logged once with every
input and exported on the worker metrics port. Each GPU task then holds one of ``effective``
slots for its whole body (:func:`task_slot`), so a worker started with more threads than the
host can feed never runs more tasks at once than fit. A slot wait longer than
``GPU_VRAM_ADMISSION_TIMEOUT_S`` raises :class:`HostMemoryAdmissionTimeoutError`, which the
task layer requeues like any other interrupted work.

The static VRAM formula is deliberately not part of the cap for an explicit setting: it
assumes ~4 GB per task and would cap a 24 GB card at 4, below what such cards run in practice.
VRAM is admission-controlled per stage by ``vram_budget`` instead. With ``auto``, the
configured value is already ``min(VRAM-based, host-based)``.

Env (all optional):

``GPU_HOST_MEMORY_ADMISSION``  ``true`` (default) / ``false`` to turn the gate off.
``GPU_HOST_BASELINE_MB``       RAM the worker holds before any task runs (default 2560).
``GPU_PER_TASK_HOST_MB``       RAM one concurrent task needs (default 2048).

Not addressed here: each task still decodes its whole file into memory (issue #1073 step 2).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from app.core.worker_shutdown import TranscriptionAbortedError
from app.transcription.vram_budget import VramBudget
from app.transcription.vram_budget import admission_timeout_s

logger = logging.getLogger(__name__)


class HostMemoryAdmissionTimeoutError(TranscriptionAbortedError):
    """A GPU task waited longer than ``GPU_VRAM_ADMISSION_TIMEOUT_S`` for a host-memory slot.

    A :class:`TranscriptionAbortedError` on purpose: the task never started its work, so the
    GPU task layer turns this into ``Reject(requeue=True)`` rather than a failure.
    """


class SlotGate(VramBudget):
    """``VramBudget``'s FIFO-fair counting reservation, counting task slots instead of MB."""

    def __init__(self, slots: int):
        super().__init__(
            slots,
            resource="Host-memory",
            unit="slot(s)",
            timeout_error=HostMemoryAdmissionTimeoutError,
        )

    @property
    def capacity(self) -> int:
        return self.capacity_mb

    @property
    def in_use(self) -> int:
        return self.reserved_mb


@dataclass(frozen=True)
class AdmissionDecision:
    """What :func:`decide` concluded, with every input, for the log line and the gauges."""

    configured: int
    budget_mb: int | None
    budget_source: str
    reserve_mb: int
    per_task_mb: int
    host_cap: int | None
    effective: int


_gate: SlotGate | None = None
_lock = threading.Lock()


def admission_enabled() -> bool:
    raw = os.getenv("GPU_HOST_MEMORY_ADMISSION", "true").strip().lower()
    return raw not in ("0", "false", "no")


def decide(configured: int) -> AdmissionDecision:
    """``min(configured, host cap)`` for this host, from the cgroup limit or ``/proc/meminfo``."""
    from app.transcription.config import DEFAULT_HOST_BASELINE_MB
    from app.transcription.config import DEFAULT_PER_TASK_HOST_MB
    from app.transcription.config import TranscriptionConfig
    from app.transcription.config import _int_env

    configured = max(1, int(configured))
    budget_mb, source = TranscriptionConfig._host_memory_budget()
    reserve_mb = _int_env("GPU_HOST_BASELINE_MB", DEFAULT_HOST_BASELINE_MB)
    per_task_mb = max(1, _int_env("GPU_PER_TASK_HOST_MB", DEFAULT_PER_TASK_HOST_MB))
    host_cap = None if budget_mb is None else max(1, (budget_mb - reserve_mb) // per_task_mb)
    effective = configured if host_cap is None else min(configured, host_cap)
    return AdmissionDecision(
        configured=configured,
        budget_mb=budget_mb,
        budget_source=source,
        reserve_mb=reserve_mb,
        per_task_mb=per_task_mb,
        host_cap=host_cap,
        effective=effective,
    )


def configure(configured: int) -> AdmissionDecision:
    """Decide the cap for this worker process, log it, export it and arm the gate.

    Args:
        configured: How many GPU tasks this process would otherwise run at once (the
            ``--pool=threads`` concurrency).
    """
    global _gate
    decision = decide(configured)
    enabled = admission_enabled()
    with _lock:
        _gate = SlotGate(decision.effective) if enabled else None
    logger.info(
        "GPU task admission by host memory: configured=%d, budget=%s MB (%s), reserve=%d MB, "
        "per-task=%d MB -> host cap=%s, effective=%d%s",
        decision.configured,
        decision.budget_mb if decision.budget_mb is not None else "unknown",
        decision.budget_source,
        decision.reserve_mb,
        decision.per_task_mb,
        decision.host_cap if decision.host_cap is not None else "none",
        decision.effective,
        "" if enabled else " (GPU_HOST_MEMORY_ADMISSION=false: not enforced)",
    )
    if enabled and decision.effective < decision.configured:
        logger.warning(
            "This GPU worker runs %d threads but host memory fits %d concurrent tasks; the "
            "other %d will wait here for a slot instead of running on another worker. Set "
            "--concurrency (and GPU_CONCURRENT_REQUESTS) to %d on this host.",
            decision.configured,
            decision.effective,
            decision.configured - decision.effective,
            decision.effective,
        )
    from app.core import worker_metrics

    worker_metrics.set_gpu_concurrency(
        configured=decision.configured,
        host_cap=decision.host_cap,
        effective=decision.effective,
    )
    return decision


def current_gate() -> SlotGate | None:
    return _gate


@contextmanager
def task_slot(stage: str) -> Iterator[int]:
    """Hold one host-memory slot for the block. A no-op until configured or when disabled."""
    gate = _gate
    if gate is None:
        yield 0
        return
    with gate.reserve(stage, 1, timeout_s=admission_timeout_s()) as granted:
        yield granted


def reset_for_tests() -> None:
    global _gate
    with _lock:
        _gate = None
