"""Live-consumer probes for the gpu-split queues.

``gpu_split_enabled()`` only reflects the operator's ``ENGINE_GPU_SPLIT`` env var, so the
dispatch and diarize-handoff paths corroborate it with a live ``inspect().active_queues()``
broadcast before publishing into ``gpu-transcribe`` / ``gpu-diarize``. Both probes read
``celery_app`` from this module's globals.
"""

import logging
import time

from app.core.celery import celery_app
from app.core.queues import CeleryQueues

logger = logging.getLogger(__name__)

# TTL cache for the "is anything actually consuming gpu-transcribe" check below.
# Only consulted when gpu_split_enabled() is True (an explicit, rare operator
# choice), so this does not add a broker round-trip to the default dispatch path.
# A short TTL means a worker that comes up or dies is noticed within seconds,
# while a 1000-file batch dispatch loop still pays the round-trip once, not 1000
# times. Module-level and lock-free: a benign race just means two dispatches in
# the same few-hundred-ms window might independently re-probe — never incorrect.
_GPU_TRANSCRIBE_CONSUMER_CACHE_TTL_S = 15.0
_gpu_transcribe_consumer_cache: dict[str, float | bool] = {"checked_at": 0.0, "present": False}


def _gpu_transcribe_consumer_present() -> bool:
    """Whether a live Celery worker is actually bound to CeleryQueues.GPU_TRANSCRIBE.

    ``gpu_split_enabled()`` only reflects the operator's ``ENGINE_GPU_SPLIT`` env var,
    which — per that function's docstring — reaches every container via ``.env``
    regardless of whether ``celery-worker-gpu-transcribe`` (the gpu-split Compose
    profile) was actually started. Routing on the env var alone reproduces the
    dead-queue hazard issue #703 already fixed once for the reservation side; this
    is the corroborating check on the dispatch side.

    Uses a live ``inspect().active_queues()`` broadcast, NOT Flower's cached
    ``/api/workers`` endpoint — that endpoint is a boot-time snapshot with known
    staleness (backend/app/tasks/CLAUDE.md, issue #609), whereas a fresh
    ``celery inspect`` call reaches workers reliably (the same doc notes
    ``celery inspect ping`` works fine even for workers Flower's snapshot misses).

    Fails CLOSED toward the safe direction: any error, timeout, or empty reply is
    treated as "no consumer" so callers fall back to the always-staffed 'gpu' queue
    rather than risk publishing into a queue nothing drains.
    """
    now = time.monotonic()
    if (
        now - float(_gpu_transcribe_consumer_cache["checked_at"])
        < _GPU_TRANSCRIBE_CONSUMER_CACHE_TTL_S
    ):
        return bool(_gpu_transcribe_consumer_cache["present"])

    present = False
    try:
        active = celery_app.control.inspect(timeout=2.0).active_queues()
        if active:
            for queues in active.values():
                if any(q.get("name") == CeleryQueues.GPU_TRANSCRIBE for q in queues or []):
                    present = True
                    break
    except Exception as e:
        logger.warning(
            f"Failed to verify a live '{CeleryQueues.GPU_TRANSCRIBE}' consumer "
            f"(falling back to '{CeleryQueues.GPU}'): {e}"
        )
        present = False

    _gpu_transcribe_consumer_cache["checked_at"] = now
    _gpu_transcribe_consumer_cache["present"] = present
    return present


# TTL cache for "is anything actually consuming gpu-diarize" — same rationale as
# queue_consumers' _GPU_TRANSCRIBE_CONSUMER_CACHE_TTL_S: only consulted when a gpu-split
# run has already produced a transcribe-only result, so this never adds a broker
# round-trip to the non-split default path. Module-level and lock-free: a benign race
# just means two dispatches in the same few-hundred-ms window might independently
# re-probe — never incorrect.
_GPU_DIARIZE_CONSUMER_CACHE_TTL_S = 15.0
_gpu_diarize_consumer_cache: dict[str, float | bool] = {"checked_at": 0.0, "present": False}


def _gpu_diarize_consumer_present() -> bool:
    """Whether a live Celery worker is actually bound to CeleryQueues.GPU_DIARIZE.

    Mirrors ``_gpu_transcribe_consumer_present`` for the diarize leg
    (issue #705): ``ENGINE_GPU_SPLIT`` reaches every container via ``.env``
    regardless of whether ``celery-worker-gpu-diarize`` (the gpu-split Compose
    profile) was actually started, so routing on the env var alone reproduces the
    dead-queue hazard issue #703 fixed for the transcribe leg. This is the
    corroborating check for the diarize leg.

    Uses a live ``inspect().active_queues()`` broadcast, NOT Flower's cached
    ``/api/workers`` endpoint — that endpoint is a boot-time snapshot with known
    staleness (backend/app/tasks/CLAUDE.md, issue #609).

    Fails CLOSED toward the safe direction, same as the transcribe-leg check: any
    error, timeout, or empty reply is treated as "no consumer" so the caller falls
    back to the always-staffed 'gpu' queue rather than risk publishing into a queue
    nothing drains.
    """
    now = time.monotonic()
    if now - float(_gpu_diarize_consumer_cache["checked_at"]) < _GPU_DIARIZE_CONSUMER_CACHE_TTL_S:
        return bool(_gpu_diarize_consumer_cache["present"])

    present = False
    try:
        active = celery_app.control.inspect(timeout=2.0).active_queues()
        if active:
            for queues in active.values():
                if any(q.get("name") == CeleryQueues.GPU_DIARIZE for q in queues or []):
                    present = True
                    break
    except Exception as e:
        logger.warning(
            f"Failed to verify a live '{CeleryQueues.GPU_DIARIZE}' consumer "
            f"(falling back to '{CeleryQueues.GPU}'): {e}"
        )
        present = False

    _gpu_diarize_consumer_cache["checked_at"] = now
    _gpu_diarize_consumer_cache["present"] = present
    return present
