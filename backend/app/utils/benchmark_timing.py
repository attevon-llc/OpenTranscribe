"""Wall-clock timing instrumentation helpers (Phase 1 of the timing audit plan).

Every call site that wants to record a pipeline-stage wall-clock uses this
module. All writes are gated on ``ENABLE_BENCHMARK_TIMING=true`` so production
pipelines pay zero overhead when the flag is off.

The Redis hash key schema is ``benchmark:{task_id}`` with float-string values
representing UNIX epoch seconds (compatible with the five markers already
written by ``dispatch.py``, ``preprocess.py``, ``core.py``, and
``postprocess.py`` before this module existed).

Persisted data:
    - ``benchmark:{task_id}`` hash — all stage markers, keyed by name.
    - ``gpu:profile:{task_id}`` blob — legacy, written by ``VRAMProfiler``.

Retention is Redis-only until the postprocess task flushes into
``file_pipeline_timing`` (see ``backend/app/models/pipeline_timing.py``).

All helpers swallow their own exceptions: instrumentation failures must never
take down a production pipeline. Debug logs surface unexpected failures so
they can be diagnosed separately.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# Benchmark hash TTL matches the dispatch.py existing convention (24h).
BENCHMARK_HASH_TTL_SECONDS = 24 * 60 * 60

# Cold-start flags flip to False after the first task runs on each worker
# process. Set lazily by ``mark_cold_start``.
_COLD_STATE: dict[str, bool] = {}


@lru_cache(maxsize=1)
def _enabled() -> bool:
    """Cached env-flag read — one string parse per process.

    Called from every marker call site; keep it cheap. Uses lru_cache so the
    environment is read once and reused.
    """
    return os.getenv("ENABLE_BENCHMARK_TIMING", "").lower() in ("1", "true", "yes", "on")


def benchmark_enabled() -> bool:
    """Public accessor for the env-gate — handy for conditional imports."""
    return _enabled()


def _hash_key(task_id: str) -> str:
    return f"benchmark:{task_id}"


def mark(task_id: str | None, name: str, value: float | None = None) -> None:
    """Write one timing marker (epoch seconds) to ``benchmark:{task_id}``.

    No-op when ``task_id`` is falsy or benchmarking is disabled. Safe to call
    from any worker — failures are logged at DEBUG only and never propagate.

    Args:
        task_id: Application-level task ID shared across pipeline stages.
        name: Marker name (e.g. ``http_request_received``, ``ffmpeg_end``).
        value: Override the timestamp; defaults to ``time.time()``.
    """
    if not task_id or not _enabled():
        return
    ts = value if value is not None else time.time()
    try:
        from app.core.redis import get_redis

        client = get_redis()
        key = _hash_key(task_id)
        pipe = client.pipeline(transaction=False)
        pipe.hset(key, name, str(ts))
        pipe.expire(key, BENCHMARK_HASH_TTL_SECONDS)
        pipe.execute()
    except Exception as e:
        logger.debug(f"benchmark mark '{name}' for {task_id} failed: {e}")


def mark_many(task_id: str | None, markers: dict[str, float | str]) -> None:
    """Write multiple markers in one Redis round-trip.

    Values may be floats (written as ``str(value)``) or strings (written
    as-is). Non-scalar values are stringified with ``repr``.
    """
    if not task_id or not _enabled() or not markers:
        return
    try:
        from app.core.redis import get_redis

        client = get_redis()
        key = _hash_key(task_id)
        payload = {k: str(v) if not isinstance(v, str) else v for k, v in markers.items()}
        pipe = client.pipeline(transaction=False)
        pipe.hset(key, mapping=payload)  # type: ignore[arg-type]
        pipe.expire(key, BENCHMARK_HASH_TTL_SECONDS)
        pipe.execute()
    except Exception as e:
        logger.debug(f"benchmark mark_many for {task_id} failed: {e}")


def pipeline_task_id(file_id: int) -> str | None:
    """The task_id of the pipeline run this file belongs to, for enrichment tasks.

    Enrichment (indexing, clustering, summary, redaction) runs in its own Celery tasks with
    their own ids, but their timings belong to the pipeline's hash — otherwise the work that
    happens after the user sees the transcript is invisible, which is exactly what left the
    tail columns dead. MediaFile.active_task_id is the single source of truth for the run
    currently driving a file.
    """
    if not benchmark_enabled():
        return None
    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile

        with session_scope() as db:
            mf = db.query(MediaFile).filter(MediaFile.id == file_id).first()
            return str(mf.active_task_id) if mf and mf.active_task_id else None
    except Exception as e:  # noqa: BLE001 — telemetry must never break the task it measures
        logger.debug(f"pipeline_task_id lookup failed for file {file_id}: {e}")
        return None


@contextmanager
def stage_for_file(file_id: int, name: str) -> Iterator[None]:
    """`stage`, for an enrichment task that knows its file but not the pipeline task."""
    with stage(pipeline_task_id(file_id), name):
        yield


@contextmanager
def stage(task_id: str | None, name: str) -> Iterator[None]:
    """Context manager that emits ``<name>_start`` and ``<name>_end`` markers.

    Example::

        with benchmark_timing.stage(task_id, "ffmpeg"):
            extract_audio_from_video(...)

    Exceptions inside the block still record the ``_end`` marker so we know
    how long the failing run took before raising.
    """
    start = time.time()
    mark(task_id, f"{name}_start", start)
    try:
        yield
    finally:
        mark(task_id, f"{name}_end", time.time())


def capture_queue_depth(
    task_id: str | None,
    queues: Iterable[str] | None = None,
) -> None:
    """Snapshot the current Redis queue depths and in-flight file count.

    Writes ``queue_depth_at_dispatch`` (JSON) and
    ``concurrent_files_at_dispatch`` (int) into the benchmark hash. The count
    of in-flight files includes anything currently in ``PROCESSING``.

    Callable from any CPU worker; tolerates Redis or DB failures silently.
    """
    if not task_id or not _enabled():
        return

    try:
        import json

        from app.core.constants import CeleryQueues
        from app.core.redis import get_redis

        client = get_redis()
        queue_names = list(queues) if queues else list(CeleryQueues.ALL)

        # Celery + kombu use list keys named after the queue for default
        # priority levels and ``<queue>\x06\x163`` etc. for other priorities.
        # We LLEN the base name — good enough as a comparative signal without
        # scanning every priority suffix.
        depths: dict[str, int] = {}
        pipe = client.pipeline(transaction=False)
        for q in queue_names:
            pipe.llen(q)
        try:
            results = pipe.execute()
        except Exception as e:
            logger.debug(f"queue depth LLEN failed: {e}")
            results = [0] * len(queue_names)
        for name, depth in zip(queue_names, results, strict=True):
            depths[name] = int(depth or 0)

        mark_many(task_id, {"queue_depth_at_dispatch": json.dumps(depths)})

        # In-flight files — a separate best-effort DB count
        try:
            from app.db.session_utils import session_scope
            from app.models.media import FileStatus
            from app.models.media import MediaFile

            with session_scope() as db:
                count = (
                    db.query(MediaFile).filter(MediaFile.status == FileStatus.PROCESSING).count()
                )
            mark_many(task_id, {"concurrent_files_at_dispatch": str(int(count))})
        except Exception as db_err:
            logger.debug(f"concurrent files count failed: {db_err}")
    except Exception as e:
        logger.debug(f"capture_queue_depth for {task_id} failed: {e}")


def mark_cold_start(task_id: str | None, worker_key: str) -> None:
    """Record whether this is the first task run on this worker process.

    ``worker_key`` is an arbitrary tag identifying the worker role, e.g.
    ``gpu`` or ``cpu``. Writes ``<worker_key>_worker_cold`` as ``"true"`` on
    the first call per process, ``"false"`` thereafter.
    """
    if not task_id or not _enabled():
        return
    is_cold = not _COLD_STATE.get(worker_key, False)
    _COLD_STATE[worker_key] = True
    mark_many(task_id, {f"{worker_key}_worker_cold": "true" if is_cold else "false"})


def set_context(task_id: str | None, fields: dict[str, Any]) -> None:
    """Attach contextual metadata to the task's benchmark hash.

    Examples: ``audio_duration_s``, ``file_size_bytes``, ``whisper_model``,
    ``asr_provider``, ``gpu_device``. Same storage as ``mark_many`` — use
    this name to make the intent clear at call sites.
    """
    mark_many(task_id, {k: v for k, v in fields.items() if v is not None})


def record_retry(
    task_id: str | None,
    *,
    stage: str,
    attempt: int,
    start: float,
    end: float,
    error: str | None = None,
) -> None:
    """Append one retry attempt's wall-clock onto the task's per_retry_timings list.

    Celery auto-retries (``autoretry_for=...``) and manual ``self.retry(...)``
    calls inflate the observed duration of a stage in ways that a naïve
    ``start/end`` marker can't capture. This helper writes one entry per
    attempt so the analysis layer can separate the first-try wall-clock from
    the retry-inflated wall-clock.

    Stored shape (JSON list)::

        [
          {"stage": "preprocess", "attempt": 1, "start_ms": ..., "end_ms": ..., "error": "..."},
          {"stage": "preprocess", "attempt": 2, "start_ms": ..., "end_ms": ...}
        ]

    Args:
        task_id: Application-level task ID. No-op when falsy.
        stage: Human label for the retried stage (``preprocess``, ``search_index``, etc.).
        attempt: 1-based attempt number (0 for the initial try if you prefer).
        start: Attempt start wall-clock (epoch seconds).
        end: Attempt end wall-clock.
        error: Optional short error string when the attempt failed.
    """
    if not task_id or not _enabled():
        return
    try:
        import json

        from app.core.redis import get_redis

        entry: dict[str, Any] = {
            "stage": stage,
            "attempt": int(attempt),
            "start_ms": int(start * 1000),
            "end_ms": int(end * 1000),
        }
        if error:
            entry["error"] = str(error)[:240]

        client = get_redis()
        key = _hash_key(task_id)
        existing_raw = client.hget(key, "per_retry_timings")
        try:
            existing: list[dict[str, Any]] = (
                json.loads(
                    existing_raw.decode()
                    if isinstance(existing_raw, (bytes, bytearray))
                    else existing_raw
                )
                if existing_raw
                else []
            )
            if not isinstance(existing, list):
                existing = []
        except (TypeError, ValueError):
            existing = []
        existing.append(entry)

        pipe = client.pipeline(transaction=False)
        pipe.hset(key, "per_retry_timings", json.dumps(existing))
        pipe.hset(key, "retry_count", str(len(existing)))
        pipe.expire(key, BENCHMARK_HASH_TTL_SECONDS)
        pipe.execute()
    except Exception as e:
        logger.debug(f"record_retry for {task_id} stage={stage} attempt={attempt} failed: {e}")


def fetch_all(task_id: str) -> dict[str, str]:
    """Read every marker for a given task_id. Returns empty dict on miss.

    Not gated on the env flag — consumers (admin endpoint, finalizer,
    benchmark scripts) always read whatever was written.
    """
    try:
        from app.core.redis import get_redis

        raw = get_redis().hgetall(_hash_key(task_id))
        if not raw:
            return {}
        return {
            (k.decode() if isinstance(k, (bytes, bytearray)) else str(k)): (
                v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            )
            for k, v in raw.items()
        }
    except Exception as e:
        logger.debug(f"fetch_all for {task_id} failed: {e}")
        return {}
