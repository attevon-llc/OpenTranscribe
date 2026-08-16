"""Embedding consistency repair logic (GPU batch worker).

Handles re-extraction of speaker embeddings for speakers missing from
OpenSearch indices. Split from speaker_embedding_consistency.py to keep
the orchestrator (CPU) and repair (GPU) logic in separate modules.

Functions:
- _v3_result_writer: Write embeddings to v3 main speaker index
- _v4_result_writer: Write embeddings to v4 staging index
- _update_repair_progress: Atomically update progress in Redis
- _run_repair_phase: Run a single repair phase (v3 or v4)
- speaker_embedding_consistency_repair_batch_task: Celery GPU task
- _check_repair_completion: Finalize when all batches are done
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any
from typing import Literal

import redis

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_PROGRESS
from app.core.constants import GPUPriority
from app.core.redis import get_redis
from app.tasks.speaker_embedding_consistency import _LOCK_TTL
from app.tasks.speaker_embedding_consistency import _REDIS_LAST_RUN_KEY
from app.tasks.speaker_embedding_consistency import _REDIS_LOCK_KEY
from app.tasks.speaker_embedding_consistency import _REDIS_PROGRESS_KEY
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

#: Below this L2 norm, a mean/aggregated embedding is treated as degenerate
#: (embeddings that cancel out) rather than normalized and written. A raw
#: zero (or near-zero) vector written to OpenSearch produces meaningless
#: cosine-similarity scores for every future search against that speaker.
_ZERO_NORM_EPSILON = 1e-8


# ---------------------------------------------------------------------------
# Result writers
# ---------------------------------------------------------------------------


def _v3_result_writer(
    prepared,
    results_by_model: dict,
    target_uuids: set[str],
) -> int:
    """Write extracted embeddings to the v3 concrete speaker index.

    IMPORTANT: Writes directly to 'speakers_v3' (not the alias), because
    the alias may point to speakers_v4 post-finalization.  Writing 512-dim
    v3 embeddings to a 256-dim v4 index would fail with a dimension mismatch.
    """
    import numpy as np

    from app.core.constants import get_speaker_index_v3
    from app.services.opensearch_service import add_speaker_embedding

    embedding_results = results_by_model.get("embedding", [])
    if not embedding_results:
        return 0

    # Group by speaker
    speaker_embeddings: dict[int, list] = {}
    for sr in embedding_results:
        speaker_embeddings.setdefault(sr.speaker_id, []).append(sr.value)

    speaker_by_id = {sp.id: sp for sp in prepared.speakers}
    written = 0

    for speaker_id, embs in speaker_embeddings.items():
        speaker = speaker_by_id.get(speaker_id)
        if not speaker or speaker.uuid not in target_uuids:
            continue

        if len(embs) == 1:
            aggregated = embs[0]
        else:
            stacked = np.vstack(embs)
            aggregated = np.mean(stacked, axis=0)
        norm = np.linalg.norm(aggregated)
        if norm <= _ZERO_NORM_EPSILON:
            logger.warning(
                "Skipping write for speaker %s (uuid=%s) in file %s: aggregated "
                "embedding from %d segment(s) has zero/near-zero norm (%.2e) — "
                "embeddings cancelled out. Not writing a degenerate vector to "
                "OpenSearch; retry in a future repair run.",
                speaker.id,
                speaker.uuid,
                prepared.file_uuid,
                len(embs),
                norm,
            )
            continue
        aggregated = aggregated / norm

        # Write to concrete v3 index, not the alias
        result = add_speaker_embedding(
            speaker_id=speaker.id,
            speaker_uuid=speaker.uuid,
            user_id=prepared.user_id,
            name=speaker.name,
            embedding=aggregated.tolist(),
            media_file_id=prepared.media_file_id,
            segment_count=len(embs),
            target_index=get_speaker_index_v3(),
        )
        if result is not None:
            written += 1

    return written


def _v4_result_writer(
    prepared,
    results_by_model: dict,
    target_uuids: set[str],
) -> int:
    """Write extracted embeddings to the v4 staging index."""
    import numpy as np

    from app.services.opensearch_service import add_speaker_embedding_v4

    embedding_results = results_by_model.get("embedding", [])
    if not embedding_results:
        return 0

    speaker_embeddings: dict[int, list] = {}
    for sr in embedding_results:
        speaker_embeddings.setdefault(sr.speaker_id, []).append(sr.value)

    speaker_by_id = {sp.id: sp for sp in prepared.speakers}
    written = 0

    for speaker_id, embs in speaker_embeddings.items():
        speaker = speaker_by_id.get(speaker_id)
        if not speaker or speaker.uuid not in target_uuids:
            continue

        if len(embs) == 1:
            aggregated = embs[0]
        else:
            stacked = np.vstack(embs)
            aggregated = np.mean(stacked, axis=0)
        norm = np.linalg.norm(aggregated)
        if norm <= _ZERO_NORM_EPSILON:
            logger.warning(
                "Skipping write for speaker %s (uuid=%s) in file %s: aggregated "
                "embedding from %d segment(s) has zero/near-zero norm (%.2e) — "
                "embeddings cancelled out. Not writing a degenerate vector to "
                "OpenSearch; retry in a future repair run.",
                speaker.id,
                speaker.uuid,
                prepared.file_uuid,
                len(embs),
                norm,
            )
            continue
        aggregated = aggregated / norm

        result = add_speaker_embedding_v4(
            speaker_id=speaker.id,
            speaker_uuid=speaker.uuid,
            user_id=prepared.user_id,
            name=speaker.name,
            embedding=aggregated.tolist(),
            media_file_id=prepared.media_file_id,
            segment_count=len(embs),
        )
        if result is not None:
            written += 1

    return written


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------


def _update_repair_progress(
    total_files: int,
    success: bool = True,
    file_uuid: str | None = None,
    repaired_count: int = 0,
    user_id: int = 1,
) -> dict[str, Any] | None:
    """Atomically update repair progress in Redis + emit via ProgressTracker.

    Uses Redis WATCH/MULTI for optimistic locking to prevent lost updates
    when multiple GPU batch workers complete concurrently. Only
    ``redis.WatchError`` (raised when another writer touched
    ``_REDIS_PROGRESS_KEY`` between ``watch()`` and ``execute()``) is retried;
    any other exception (e.g. a ``json.JSONDecodeError`` from corrupted
    stored state) propagates immediately instead of being silently retried
    and then dropped by the fallback branch below.
    """
    from app.services.progress_tracker import ProgressTracker
    from app.services.progress_tracker import emit_progress_notification

    r = get_redis()
    progress: dict[str, Any] = {}
    processed = 0

    # Optimistic locking: retry on concurrent modification
    for _attempt in range(5):
        try:
            pipe = r.pipeline(True)  # transactional pipeline
            pipe.watch(_REDIS_PROGRESS_KEY)

            raw_val: str | bytes | None = pipe.get(_REDIS_PROGRESS_KEY)  # type: ignore[assignment]
            if not raw_val:
                pipe.unwatch()
                return None

            progress = json.loads(raw_val)
            progress["processed_files"] = progress.get("processed_files", 0) + 1
            progress["repaired"] = progress.get("repaired", 0) + repaired_count

            if not success and file_uuid:
                failed = progress.get("failed_files", [])
                failed.append(file_uuid)
                progress["failed_files"] = failed

            processed = progress["processed_files"]
            progress["running"] = processed < total_files

            pipe.multi()
            pipe.set(_REDIS_PROGRESS_KEY, json.dumps(progress), ex=_LOCK_TTL)
            pipe.execute()
            break  # success
        except redis.WatchError:
            # Another writer touched _REDIS_PROGRESS_KEY between watch() and
            # execute() -- retry the whole optimistic-lock round-trip.
            continue
    else:
        # All retries exhausted; read current state for notification
        fallback_raw = r.get(_REDIS_PROGRESS_KEY)
        if not fallback_raw:
            return None
        progress = json.loads(fallback_raw)
        processed = progress.get("processed_files", 0)

    # Use ProgressTracker for ETA + queue status integration
    tracker = ProgressTracker(
        task_type="embedding_consistency",
        user_id=user_id,
        total=total_files,
    )
    existing = ProgressTracker.get_state("embedding_consistency", user_id)
    if existing:
        tracker.resume_from_state(existing)

    emit_progress_notification(
        tracker=tracker,
        processed=processed,
        user_id=user_id,
        notification_type=NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_PROGRESS,
        extra_data={
            "processed_files": processed,
            "total_files": total_files,
            "repaired": progress.get("repaired", 0),
            "failed_files": progress.get("failed_files", []),
            "running": progress["running"],
        },
        message=f"Repaired {processed} of {total_files} files",
        failed_item=file_uuid if not success else None,
    )
    return progress  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Repair phase runner
# ---------------------------------------------------------------------------


def _run_repair_phase(
    mode: Literal["v3", "v4"],
    target_uuids: set[str],
    result_writer_fn,
    files_with_speakers: list,
    file_written: dict[str, int],
    is_running_check,
    batch_index: int,
    on_file_done=None,
    on_file_fail=None,
) -> int:
    """Run a single repair phase (v3 or v4) using the migration pipeline."""
    from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
    from app.services.speaker_analysis_models import EmbeddingModelAdapter
    from app.services.speaker_analysis_models import MultiModelRunner
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.migration_pipeline import process_batch_pipelined

    try:
        embedding_service = get_cached_embedding_service(mode=mode)
        runner = MultiModelRunner([EmbeddingModelAdapter(embedding_service)])

        # Track actual embeddings written per file via closure.
        # Safe because process_batch_pipelined processes files sequentially:
        # writer runs, then on_success, before moving to the next file.
        last_write_count = 0

        def writer(prep, results):
            nonlocal last_write_count
            count = result_writer_fn(prep, results, target_uuids)
            last_write_count = count
            return count

        def on_success(fuuid: str) -> None:
            file_written[fuuid] = file_written.get(fuuid, 0) + last_write_count
            if on_file_done:
                on_file_done(fuuid)

        def on_failure(fuuid: str, _err) -> None:
            if on_file_fail:
                on_file_fail(fuuid)

        success, _ = process_batch_pipelined(
            prepared_files=files_with_speakers,
            runner=runner,
            result_writer=writer,
            is_running_check=is_running_check,
            on_file_success=on_success,
            on_file_failure=on_failure,
            min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
        )
        return success
    except Exception as e:
        logger.error("%s repair failed for batch %d: %s", mode, batch_index, e)
        return 0


# ---------------------------------------------------------------------------
# GPU batch Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="speaker_embedding_consistency_repair_batch",
    priority=GPUPriority.ADMIN_MIGRATION,
)
def speaker_embedding_consistency_repair_batch_task(
    self,
    file_uuids: list[str],
    batch_index: int = 0,
    total_batches: int = 1,
    total_files: int = 0,
    missing_v3_uuids: list[str] | None = None,
    missing_v4_uuids: list[str] | None = None,
    v4_exists: bool = False,
) -> dict[str, Any]:
    """Re-extract embeddings for speakers missing from OpenSearch indices."""
    from app.services.embedding_mode_service import MODE_V3
    from app.services.embedding_mode_service import MODE_V4
    from app.tasks.migration_pipeline import prepare_file

    logger.info(
        "Consistency repair batch %d/%d: processing %d files",
        batch_index + 1,
        total_batches,
        len(file_uuids),
    )

    r = get_redis()
    missing_v3_set = set(missing_v3_uuids or [])
    missing_v4_set = set(missing_v4_uuids or [])

    need_v3 = bool(missing_v3_set)
    need_v4 = bool(missing_v4_set) and v4_exists

    if not need_v3 and not need_v4:
        return {"status": "nothing_to_do", "batch_index": batch_index}

    # Retrieve user_id from progress data for notifications
    raw = r.get(_REDIS_PROGRESS_KEY)
    notify_user_id = 1
    if raw:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            notify_user_id = json.loads(raw).get("user_id", 1)

    # Phase 1: Prepare files
    files_with_speakers: list[tuple[str, Any]] = []
    for fuuid in file_uuids:
        if not r.exists(_REDIS_LOCK_KEY):
            logger.warning("Consistency repair stopped — aborting batch")
            break
        try:
            prepared = prepare_file(fuuid)
            if prepared is None:
                _update_repair_progress(total_files, success=True, user_id=notify_user_id)
            else:
                files_with_speakers.append((fuuid, prepared))
        except Exception as e:
            logger.error("Failed to prepare file %s: %s", fuuid, e)
            _update_repair_progress(
                total_files, success=False, file_uuid=fuuid, user_id=notify_user_id
            )

    if not files_with_speakers:
        _check_repair_completion(total_files, user_id=notify_user_id)
        return {"status": "no_files", "batch_index": batch_index}

    file_written: dict[str, int] = {}
    # Track which files have already had progress emitted (avoid double-counting)
    progress_emitted: set[str] = set()

    def is_running():
        return bool(r.exists(_REDIS_LOCK_KEY))

    def _on_file_complete(fuuid: str) -> None:
        """Emit progress notification when a file finishes a repair phase."""
        if fuuid in progress_emitted:
            return  # Already counted this file
        progress_emitted.add(fuuid)
        count = file_written.get(fuuid, 0)
        _update_repair_progress(
            total_files,
            success=count > 0,
            file_uuid=fuuid if count == 0 else None,
            repaired_count=count,
            user_id=notify_user_id,
        )

    def _on_file_failed(fuuid: str) -> None:
        """Emit progress notification for a failed file."""
        if fuuid in progress_emitted:
            return
        progress_emitted.add(fuuid)
        _update_repair_progress(
            total_files,
            success=False,
            file_uuid=fuuid,
            user_id=notify_user_id,
        )

    # Phase 2: V3 repair (always emit progress — if V4 phase fails, V3 is the only signal)
    v3_repaired = 0
    if need_v3:
        v3_repaired = _run_repair_phase(
            MODE_V3,
            missing_v3_set,
            _v3_result_writer,
            files_with_speakers,
            file_written,
            is_running,
            batch_index,
            on_file_done=_on_file_complete,
            on_file_fail=_on_file_failed,
        )

    # Phase 3: V4 repair
    v4_repaired = 0
    if need_v4:
        v4_repaired = _run_repair_phase(
            MODE_V4,
            missing_v4_set,
            _v4_result_writer,
            files_with_speakers,
            file_written,
            is_running,
            batch_index,
            on_file_done=_on_file_complete,
            on_file_fail=_on_file_failed,
        )

    _check_repair_completion(total_files, user_id=notify_user_id)

    # Free intermediate CUDA tensors for follow-on tasks
    from app.tasks.migration_pipeline import cleanup_gpu_memory

    cleanup_gpu_memory()

    return {
        "status": "success",
        "batch_index": batch_index,
        "v3_repaired": v3_repaired,
        "v4_repaired": v4_repaired,
    }


# ---------------------------------------------------------------------------
# Completion check
# ---------------------------------------------------------------------------


def _check_repair_completion(total_files: int, user_id: int = 1) -> None:
    """Check if all batches are done and finalize."""
    from app.services.progress_tracker import ProgressTracker

    r = get_redis()
    raw = r.get(_REDIS_PROGRESS_KEY)
    if not raw:
        return

    progress = json.loads(raw)
    processed = progress.get("processed_files", 0)
    # Prefer user_id from progress data (set by orchestrator)
    notify_user_id = progress.get("user_id", user_id)

    if processed >= total_files:
        duration = round(time.time() - (progress.get("start_time", time.time())), 1)
        last_run = {
            "timestamp": time.time(),
            "status": "repaired",
            "repaired": progress.get("repaired", 0),
            "unrepairable": progress.get("unrepairable", 0),
            "failed_files": progress.get("failed_files", []),
            "total_files": total_files,
            "duration_seconds": duration,
        }
        r.set(_REDIS_LAST_RUN_KEY, json.dumps(last_run), ex=86400 * 7)

        # Complete the ProgressTracker so queue status clears
        tracker = ProgressTracker(
            task_type="embedding_consistency",
            user_id=notify_user_id,
            total=total_files,
        )
        tracker.complete(message="Embedding consistency repair complete")

        send_ws_event(
            notify_user_id,
            NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_COMPLETE,
            {
                "status": "completed",
                "repaired": progress.get("repaired", 0),
                "unrepairable": progress.get("unrepairable", 0),
                "failed_files": progress.get("failed_files", []),
                "total_files": total_files,
                "duration_seconds": duration,
            },
        )

        # Release lock
        r.delete(_REDIS_LOCK_KEY)
        r.delete(_REDIS_PROGRESS_KEY)
        logger.info("Embedding consistency repair complete: %d files processed", total_files)
