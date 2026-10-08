"""Per-process VRAM admission for GPU stages (issue #1081).

A GPU worker runs ``--pool=threads`` with several transcriptions in flight on one card. Each
GPU stage (Whisper decode, in-process diarization) has a large, batch-dependent working set on
top of the models that are already resident. Admitting stages by thread count alone let four
of them peak together and OOM a 24 GB card.

A free-memory check before starting a stage does not fix that: N threads read the same
``mem_get_info()`` figure at the same moment and all start. So each stage RESERVES its
estimated peak from a per-process budget and waits, bounded, when it does not fit::

    capacity = total VRAM x GPU_VRAM_BUDGET_FRACTION - VRAM already in use

"Already in use" is measured once, after the worker preloaded its models, so it covers the
model weights, the CUDA context and anything else sharing the card (another process, a
co-located diarization sidecar that has already warmed up).

A stage larger than the whole capacity is clamped to it: it runs, alone. A wait that exceeds
``GPU_VRAM_ADMISSION_TIMEOUT_S`` raises :class:`VramAdmissionTimeoutError`, which the task
layer requeues like a shutdown abort (the work was never started, so nothing is lost).

Waiters are admitted strictly in arrival order, so a large stage is not starved by a stream of
small ones that would each fit in the gap.

Env (all optional):

``GPU_VRAM_ADMISSION``           ``true`` (default) / ``false`` to bypass admission entirely.
``GPU_VRAM_BUDGET_FRACTION``     share of total VRAM the worker may plan to use (0.85).
``GPU_VRAM_ADMISSION_TIMEOUT_S`` longest a stage waits for room before requeueing (1800).
``GPU_STAGE_VRAM_MB_<STAGE>``    fixed estimate for a stage (``ASR``, ``DIARIZATION``),
                                 replacing the measured default below.

Stdlib-only at import time: torch is imported inside the one function that measures the card,
so CPU-only workers and bare pytest pay nothing for importing this module.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

from app.core.worker_shutdown import TranscriptionAbortedError

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_FRACTION = 0.85
DEFAULT_ADMISSION_TIMEOUT_S = 1800.0

#: Floor for the capacity, so a card that is already over its planned share (a large foreign
#: process on the same GPU) still runs one stage at a time instead of none.
MIN_CAPACITY_MB = 1024

# Per-stage peak estimates ABOVE the resident models, in MB, rounded up from measurements on
# an RTX 3080 Ti (12 GB), large-v3-turbo int8_float16, a 65-minute 6-speaker meeting,
# device-wide cudaMemGetInfo sampled every 20 ms, one stage per process:
#   models resident (Whisper + PyAnnote): 1090 MB, identical at num_workers 1 and 4
#   Whisper decode, batch 16 / 8 / 4:     2382 / 1198 / 622 MB  (~146 MB per batch item)
#   in-process PyAnnote diarization:      1062 MB
# Re-measure for another card or model with `python -m app.scripts.gpu_stage_vram_probe`.
ASR_BASE_MB = 100
ASR_PER_BATCH_ITEM_MB = 150
DIARIZATION_MB = 1200

_STAGE_DEFAULTS_MB = {"diarization": DIARIZATION_MB}


class VramAdmissionTimeoutError(TranscriptionAbortedError):
    """A GPU stage waited longer than ``GPU_VRAM_ADMISSION_TIMEOUT_S`` for VRAM.

    A :class:`TranscriptionAbortedError` on purpose: the stage never started, so this is
    interrupted work, and every GPU task already turns that into ``Reject(requeue=True)``.
    """


class VramBudget:
    """A counting reservation over a fixed VRAM capacity, FIFO-fair and thread-safe.

    ``resource``/``unit`` name what is counted in the log lines and ``timeout_error`` is what a
    wait past its deadline raises, so the same gate can count other things (the host-memory
    task slots of ``host_memory_admission``).
    """

    def __init__(
        self,
        capacity_mb: int,
        *,
        resource: str = "VRAM",
        unit: str = "MB",
        timeout_error: type[TranscriptionAbortedError] | None = None,
    ):
        self._resource = resource
        self._unit = unit
        self._timeout_error = timeout_error or VramAdmissionTimeoutError
        self._capacity = max(0, int(capacity_mb))
        self._reserved = 0
        self._cond = threading.Condition()
        self._queue: deque[object] = deque()
        self.admitted_total = 0
        self.timeouts_total = 0

    @property
    def capacity_mb(self) -> int:
        return self._capacity

    @property
    def reserved_mb(self) -> int:
        with self._cond:
            return self._reserved

    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    @contextmanager
    def reserve(self, stage: str, mb: int, *, timeout_s: float) -> Iterator[int]:
        """Hold ``mb`` of the budget for the duration of the block; yield what was granted."""
        want = max(0, min(int(mb), self._capacity))
        ticket = object()
        started = time.monotonic()
        deadline = started + max(0.0, timeout_s)
        with self._cond:
            self._queue.append(ticket)
            try:
                while not (self._queue[0] is ticket and self._reserved + want <= self._capacity):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.timeouts_total += 1
                        raise self._timeout_error(
                            f"{self._resource} admission for stage {stage!r} timed out after "
                            f"{timeout_s:.0f}s: wanted {want} {self._unit}, {self._reserved}/"
                            f"{self._capacity} {self._unit} reserved"
                        )
                    self._cond.wait(remaining)
            finally:
                self._queue.remove(ticket)
                # The head changed (admitted or gave up): let the next waiter re-check.
                self._cond.notify_all()
            self._reserved += want
            self.admitted_total += 1
            reserved_now = self._reserved
        waited = time.monotonic() - started
        log = logger.info if waited >= 1.0 else logger.debug
        log(
            "%s admit [%s]: %d %s granted after %.1fs wait (reserved %d/%d %s)",
            self._resource,
            stage,
            want,
            self._unit,
            waited,
            reserved_now,
            self._capacity,
            self._unit,
        )
        try:
            yield want
        finally:
            with self._cond:
                self._reserved -= want
                self._cond.notify_all()


_budget: VramBudget | None = None
_configured = False
_config_lock = threading.Lock()
_init_lock = threading.Lock()


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


def admission_enabled() -> bool:
    return os.getenv("GPU_VRAM_ADMISSION", "true").strip().lower() not in ("0", "false", "no")


def admission_timeout_s() -> float:
    return _float_env("GPU_VRAM_ADMISSION_TIMEOUT_S", DEFAULT_ADMISSION_TIMEOUT_S)


def _stage_override_raw(stage: str) -> str:
    # Literal names, one per stage, so the env-documentation checks can see every read.
    if stage == "asr":
        return os.getenv("GPU_STAGE_VRAM_MB_ASR", "").strip()
    if stage == "diarization":
        return os.getenv("GPU_STAGE_VRAM_MB_DIARIZATION", "").strip()
    return ""


def stage_estimate_mb(stage: str, *, batch_size: int | None = None) -> int:
    """Peak VRAM a stage needs above the resident models, in MB."""
    raw = _stage_override_raw(stage)
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning("Invalid GPU_STAGE_VRAM_MB_%s=%r; using the default", stage.upper(), raw)
    if stage == "asr":
        return ASR_BASE_MB + ASR_PER_BATCH_ITEM_MB * max(1, int(batch_size or 1))
    return _STAGE_DEFAULTS_MB.get(stage, 0)


def configure_from_device(device_index: int = 0) -> VramBudget | None:
    """Measure the card now and (re)build the process budget. Returns None without CUDA.

    Call after the worker has preloaded its models, so their footprint counts as "in use".
    """
    global _budget, _configured
    try:
        import torch

        if not torch.cuda.is_available():
            with _config_lock:
                _budget, _configured = None, True
            return None
        free_b, total_b = torch.cuda.mem_get_info(device_index)
    except Exception as exc:  # noqa: BLE001 - admission must never break a worker
        logger.warning("VRAM budget: could not read device memory (%s); admission off", exc)
        with _config_lock:
            _budget, _configured = None, True
        return None

    total_mb = int(total_b / 1024**2)
    used_mb = total_mb - int(free_b / 1024**2)
    fraction = min(1.0, max(0.05, _float_env("GPU_VRAM_BUDGET_FRACTION", DEFAULT_BUDGET_FRACTION)))
    capacity = max(MIN_CAPACITY_MB, int(total_mb * fraction) - used_mb)
    budget = VramBudget(capacity)
    with _config_lock:
        _budget, _configured = budget, True
    logger.info(
        "VRAM budget: total=%d MB, in use at start=%d MB, fraction=%.2f -> capacity=%d MB "
        "(estimates: asr=%d MB at batch 16, diarization=%d MB; timeout=%.0fs)",
        total_mb,
        used_mb,
        fraction,
        capacity,
        stage_estimate_mb("asr", batch_size=16),
        stage_estimate_mb("diarization"),
        admission_timeout_s(),
    )
    return budget


def current_budget() -> VramBudget | None:
    """The configured budget, without configuring one."""
    return _budget


def _get_or_configure(device_index: int) -> VramBudget | None:
    if _configured:
        return _budget
    # Serialise the first measurement: two threads each building a budget would each hand
    # out reservations against their own copy, and admission would silently double.
    with _init_lock:
        if _configured:
            return _budget
        return configure_from_device(device_index)


@contextmanager
def admit(
    stage: str, *, device: str, batch_size: int | None = None, device_index: int = 0
) -> Iterator[int]:
    """Reserve a stage's VRAM estimate for the block. A no-op off CUDA or when disabled."""
    if device != "cuda" or not admission_enabled():
        yield 0
        return
    budget = _get_or_configure(device_index)
    if budget is None:
        yield 0
        return
    with budget.reserve(
        stage, stage_estimate_mb(stage, batch_size=batch_size), timeout_s=admission_timeout_s()
    ) as granted:
        yield granted


def reset_for_tests() -> None:
    global _budget, _configured
    with _config_lock:
        _budget, _configured = None, False
