"""CUDA error classification and the poisoned-context exit (issue #1081).

Two very different CUDA failures reach a GPU task:

* **Out of memory.** Recoverable inside the process: free the allocator caches and retry with
  a smaller batch (``transcriber.Transcriber.transcribe``; the in-process diarizer has its own
  batch backoff).
* **A broken context.** ``invalid device ordinal``, ``illegal memory access``, a device-side
  assert and their relatives are sticky: every later CUDA call in this process fails too.
  Observed after an OOM on a 24 GB card at thread concurrency 4, where the next task on the
  same worker failed with ``cudaErrorInvalidDevice``. The only fix is a new process.

:func:`mark_context_poisoned` stops the worker taking work and asks it to exit through the
same graceful path a ``docker stop`` uses (issue #782/#809): it arms the shutdown flag, so
every other in-flight task stands down at its next checkpoint and is requeued, then sends
SIGTERM to its own process, so celery performs a warm shutdown, releases the models and exits.
The container's ``restart: always`` (or the pod's restart policy) starts a fresh process.

Classification matches on text and class NAME, never on imported types, so it works for
CTranslate2's plain ``RuntimeError`` and stays importable without torch.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

logger = logging.getLogger(__name__)

_OOM_MARKERS = (
    "out of memory",
    "cuda_error_out_of_memory",
    "cudaerrormemoryallocation",
)

_POISONED_MARKERS = (
    "invalid device ordinal",
    "cudaerrorinvaliddevice",
    "illegal memory access",
    "cudaerrorillegaladdress",
    "illegal instruction was encountered",
    "misaligned address",
    "unspecified launch failure",
    "cudaerrorlaunchfailure",
    "device-side assert",
    "uncorrectable ecc error",
    "context is destroyed",
    "cudaerrorcontextisdestroyed",
)

_POISONED = threading.Event()

# A context-poisoning error the probe calls healthy is usually a one-off (see
# cuda_context_healthy). The probe only exercises torch, though, and CTranslate2 keeps its own
# CUDA state: observed on a 24 GB card at 3 concurrent jobs, an OOM left Whisper failing with
# ``cudaErrorInvalidDevice`` on every later task while the torch probe kept passing, so the
# worker never restarted and failed each job it took. The same error recurring this often in
# one process is treated as poisoned whatever the probe says.
RECURRING_ERROR_LIMIT = 3
RECURRING_ERROR_WINDOW_S = 600.0
_recent_errors: list[float] = []
_recent_lock = threading.Lock()


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen and len(seen) < 16:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def is_cuda_oom(exc: BaseException) -> bool:
    """True for a GPU out-of-memory error from torch or CTranslate2 (not host MemoryError)."""
    for e in _chain(exc):
        if type(e).__name__ == "OutOfMemoryError":
            return True
        text = str(e).lower()
        if any(marker in text for marker in _OOM_MARKERS):
            return True
    return False


def is_context_poisoned_error(exc: BaseException) -> bool:
    """True for a CUDA error that leaves this process's context unusable."""
    for e in _chain(exc):
        text = str(e).lower()
        if any(marker in text for marker in _POISONED_MARKERS):
            return True
    return False


def free_cached_vram() -> None:
    """Return cached allocator blocks to the driver before a retry. Best effort."""
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception as exc:  # noqa: BLE001 - a failed cleanup must not mask the OOM path
        logger.debug("empty_cache skipped: %s", exc)


def cuda_context_healthy(device_index: int = 0) -> bool:
    """Whether a trivial CUDA op still works in this process.

    A poisoning-looking message is not always a broken context. Measured on an RTX 3080 Ti
    at six concurrent decodes: CTranslate2 reported ``parallel_for failed:
    cudaErrorInvalidDevice: invalid device ordinal`` for decodes that collided with an OOM,
    and later tasks in the SAME process then completed normally. A genuinely sticky error
    (illegal address, device-side assert) fails every later CUDA call, so a probe tells the
    two apart. Two attempts, because the first CUDA call after a non-sticky error can still
    report it once. Returns True when it cannot tell (no torch, no CUDA): never take a worker
    out of service on a guess.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return True
    except Exception:  # noqa: BLE001
        return True
    last: Exception | None = None
    for _ in range(2):
        try:
            probe = torch.ones(1, device=f"cuda:{device_index}")
            float((probe + 1).sum().item())
            torch.cuda.synchronize(device_index)
            return True
        except Exception as exc:  # noqa: BLE001 - the probe's failure IS the answer
            last = exc
    logger.error("CUDA context probe failed: %s", last)
    return False


def recurring_context_error(now: float | None = None) -> bool:
    """Record one context-poisoning-looking error; True once it has recurred
    ``RECURRING_ERROR_LIMIT`` times within ``RECURRING_ERROR_WINDOW_S`` in this process."""
    now = time.monotonic() if now is None else now
    with _recent_lock:
        _recent_errors[:] = [t for t in _recent_errors if now - t < RECURRING_ERROR_WINDOW_S]
        _recent_errors.append(now)
        return len(_recent_errors) >= RECURRING_ERROR_LIMIT


def context_poisoned() -> bool:
    return _POISONED.is_set()


def mark_context_poisoned(reason: str) -> None:
    """Take this worker out of service and ask it to exit so it is restarted. Idempotent."""
    if _POISONED.is_set():
        return
    _POISONED.set()
    logger.critical(
        "CUDA context is unusable (%s); this worker stops taking work and exits so its "
        "supervisor restarts it. In-flight tasks are requeued.",
        reason[:300],
    )
    from app.core.worker_shutdown import mark_shutting_down

    mark_shutting_down()
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001 - the flag above already stops new work
        logger.error("Could not signal worker exit after a poisoned CUDA context: %s", exc)


def reset_for_tests() -> None:
    _POISONED.clear()
    with _recent_lock:
        _recent_errors.clear()
