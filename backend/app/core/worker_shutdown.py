"""Graceful GPU worker shutdown (issue #782).

`./opentr.sh stop` reached a live CUDA process with docker's bare 10s SIGTERM-then-SIGKILL,
with nothing in the backend releasing the transcriber, diarizer, or CUDA context on the way
out. The compose-level `stop_grace_period` (docker-compose.yml) is the primary fix — it
gives a worker time to exit on its own — but only if something actually releases the models
and tears down the CUDA context during that window. This module is that something.

Kept separate from ``app.core.celery`` (already ~950 lines, and the celery app itself pulls
in torch at import time) so the release logic is unit-testable without importing celery at
all. ``app/core/celery.py`` wires three thin ``@signal.connect`` shims that delegate in here
— the same shape ``preload_models`` already uses toward ``ModelManager``.

Signal responsibilities (do NOT conflate these — see the docstrings below):

- ``worker_shutting_down`` -> :func:`mark_shutting_down` — fires INSIDE celery's signal
  handler, before the task pool has drained. Must do NOTHING but arm a flag.
- ``worker_shutdown`` -> :func:`release_worker_resources` — fires in the MainProcess AFTER
  ``_shutdown(warm=True)`` has joined the pool, so no task is still running. This is where
  the actual release happens.
- ``worker_process_shutdown`` -> :func:`release_embedding_cache_only` — fires per forked
  prefork child at reap. Only the embedding cache is ever populated there
  (celery-embedding-worker is the one prefork service that touches it).

Every release step below is gated on ``sys.modules.get(...)``, never a real ``import`` —
this is LOAD-BEARING, not an optimisation. A worker that never touched torch/pyannote
(celery-nlp-worker, celery-download-worker, a lite deployment) must not have a multi-second
import dragged onto its shutdown path just to discover the subsystem is irrelevant to it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

#: Set by ``mark_shutting_down()`` (the ``worker_shutting_down`` signal, BEFORE the drain).
#: Read-only probe for a future cooperative-abort checkpoint (issue #782 follow-up, out of
#: scope for this change) — deliberately not consulted by anything in this module.
_SHUTDOWN = threading.Event()

#: Guards :func:`release_worker_resources` against running twice (celery can, in principle,
#: deliver `worker_shutdown` more than once on some code paths).
_RELEASED = threading.Event()

#: Hard ceiling on how long the release may take before the watchdog forces an exit. 20s
#: default: generous for an idle-or-between-stages worker, short enough that a genuinely
#: wedged release does not outlive docker's own `stop_grace_period` for long.
_BUDGET_S = float(os.getenv("OT_WORKER_SHUTDOWN_BUDGET_S", "20"))


def mark_shutting_down() -> None:
    """Arm the shutdown flag. Wired to ``worker_shutting_down`` — fires **before** the
    task pool has drained, while a task may still be running.

    Must do NOTHING else. Releasing a model here would free VRAM (and tear down the CUDA
    context) underneath a live CUDA kernel.
    """
    _SHUTDOWN.set()
    logger.info("worker shutdown: signalled (worker_shutting_down)")


def shutdown_requested() -> bool:
    """Whether a shutdown signal has been received.

    Read-only; exists for a future cooperative-abort checkpoint to poll. Nothing in this
    module consults it.
    """
    return _SHUTDOWN.is_set()


def _force_exit() -> None:
    """Watchdog fallback: the release did not complete within its budget.

    Logs and exits immediately rather than leaving the process to be SIGKILLed by docker
    once ``stop_grace_period`` elapses — at that point we are already past the window this
    module exists to use well, so there is nothing left to lose by exiting now.
    """
    logger.error(
        "worker shutdown: release did not complete within %.1fs budget -- forcing exit",
        _BUDGET_S,
    )
    os._exit(0)


def _release_pii_pool() -> bool:
    """Shut down the redaction PII process pool, if it was ever started.

    Returns whether the module was loaded at all (not whether a pool was running --
    ``pii_pool.shutdown()`` is already a no-op when there is nothing to stop).
    """
    mod = sys.modules.get("app.services.redaction.pii_pool")
    if mod is None:
        return False
    mod.shutdown()
    return True


def _release_models() -> dict[str, bool]:
    """Release the transcriber and diarizer via ``ModelManager.release_all()``.

    NOT ``release_transcriber()`` — that frees the transcriber only. ``release_all()``
    additionally frees the diarizer and runs the trailing GPU cleanup.

    Returns which of the two were actually loaded (for the summary log line), or an empty
    dict when the module was never imported or no instance was ever created.
    """
    mod = sys.modules.get("app.transcription.model_manager")
    if mod is None:
        return {}
    manager_cls = mod.ModelManager
    if manager_cls._instance is None:
        return {}
    instance = manager_cls.get_instance()
    released = {
        "transcriber": instance._transcriber is not None,
        "diarizer": instance._diarizer is not None,
    }
    instance.release_all()
    return released


def _release_embedding_cache() -> bool:
    """Clear the cached speaker-embedding service, if one was ever built."""
    mod = sys.modules.get("app.services.speaker_embedding_service")
    if mod is None:
        return False
    was_loaded = mod._cached_embedding_service is not None
    mod.clear_embedding_cache()
    return was_loaded


def _final_cuda_sweep() -> None:
    """Best-effort final CUDA sync + cache empty, only if torch is already resident.

    ``_release_models()`` already calls ``ModelManager._cleanup_gpu()``; this is a second,
    cheap pass in case something outside ModelManager (a stray tensor, a cuDNN workspace)
    is still holding VRAM this worker is about to give up entirely.
    """
    torch = sys.modules.get("torch")
    if torch is None:
        return
    try:
        if torch.cuda.is_initialized():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
    except Exception:
        logger.exception("worker shutdown: final CUDA sweep failed")


def _step(name: str, fn, results: dict[str, object]) -> None:
    """Run one release step, isolated: an exception here must not skip the others."""
    try:
        results[name] = fn()
    except Exception:
        logger.exception("worker shutdown: %s release step failed", name)


def release_worker_resources(*, budget_s: float | None = None) -> None:
    """Release every GPU-holding resource this worker process may hold.

    Wired to the ``worker_shutdown`` signal, which fires in the MainProcess AFTER
    ``_shutdown(warm=True)`` has joined the worker pool — no task is still running by the
    time this runs, which is what makes releasing models here safe (unlike
    ``worker_shutting_down``, see :func:`mark_shutting_down`).

    Idempotent (a second call is a no-op) and bounded by a ``threading.Timer`` watchdog —
    not ``signal.alarm``, which ``core/celery.py`` already documents as unreliable under
    ``--pool=threads`` (every GPU worker here).

    Args:
        budget_s: Override for the watchdog budget, in seconds. Defaults to
            ``OT_WORKER_SHUTDOWN_BUDGET_S`` (20s) when omitted.
    """
    if _RELEASED.is_set():
        return
    _RELEASED.set()

    started = time.monotonic()
    watchdog = threading.Timer(budget_s if budget_s is not None else _BUDGET_S, _force_exit)
    watchdog.daemon = True
    watchdog.start()

    results: dict[str, object] = {}
    try:
        _step("pii_pool", _release_pii_pool, results)
        _step("models", _release_models, results)
        _step("embeddings", _release_embedding_cache, results)
        _step("cuda", _final_cuda_sweep, results)
    finally:
        watchdog.cancel()
        released_models = results.get("models")
        transcriber = int(
            isinstance(released_models, dict) and bool(released_models.get("transcriber"))
        )
        diarizer = int(isinstance(released_models, dict) and bool(released_models.get("diarizer")))
        embeddings = int(bool(results.get("embeddings")))
        logger.info(
            "worker shutdown: released transcriber+diarizer "
            "(transcriber=%d diarizer=%d embeddings=%d elapsed=%.3fs)",
            transcriber,
            diarizer,
            embeddings,
            time.monotonic() - started,
        )


def release_embedding_cache_only() -> None:
    """Per-forked-child cleanup. Wired to ``worker_process_shutdown``.

    Only the embedding cache — ``celery-embedding-worker`` is the sole PREFORK service
    that populates it, so this is the only thing worth doing once per reaped child.
    Exception-isolated for the same reason every step in :func:`release_worker_resources`
    is: a failure here must not crash the reaper.
    """
    try:
        _release_embedding_cache()
    except Exception:
        logger.exception("worker shutdown: per-process embedding cache release failed")
