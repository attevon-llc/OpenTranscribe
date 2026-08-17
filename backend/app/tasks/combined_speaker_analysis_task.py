"""
Celery tasks for combined speaker embedding + gender detection.

Used when both models need to process the same files:
- Online ASR per-file path (no native embeddings available)
- Combined batch migration (admin-triggered "reprocess all")

Audio segments are loaded ONCE and fed to both models back-to-back.
"""

import json
import logging

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_COMBINED_MIGRATION_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_COMBINED_MIGRATION_PROGRESS
from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
from app.core.constants import CPUPriority
from app.core.constants import GPUPriority
from app.core.redis import get_redis
from app.db.session_utils import session_scope
from app.services.migration_progress_service import MigrationProgressService
from app.services.progress_tracker import ProgressTracker
from app.services.progress_tracker import emit_progress_notification
from app.services.speaker_analysis_models import SegmentResult
from app.tasks.migration_pipeline import PreparedFile
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

_BATCH_SIZE = 25

# Progress tracker for combined migration
combined_migration_progress = MigrationProgressService(key_prefix="combined_speaker_migration")

# ---------------------------------------------------------------------------
# Combined result writer
# ---------------------------------------------------------------------------


def _combined_result_writer(
    prepared: PreparedFile,
    results_by_model: dict[str, list[SegmentResult]],
) -> int:
    """Write both embedding and gender results from a combined run.

    Each writer is wrapped in try/except so a failure in one does not
    prevent the other from running. Both OpenSearch (embeddings) and
    PostgreSQL (gender) writes are attempted independently.
    """
    count = 0

    # Write embeddings to OpenSearch
    try:
        from app.tasks.embedding_migration_v4 import _embedding_result_writer

        emb_count = _embedding_result_writer(prepared, results_by_model)
        count += emb_count
    except Exception as e:
        logger.error(
            "Embedding write failed for %s (gender will still run): %s",
            prepared.file_uuid[:12],
            e,
        )

    # Write gender to PostgreSQL
    try:
        from app.tasks.speaker_attribute_migration_task import _gender_result_writer

        gender_count = _gender_result_writer(prepared, results_by_model)
        count += gender_count
    except Exception as e:
        logger.error(
            "Gender write failed for %s (embeddings may have succeeded): %s",
            prepared.file_uuid[:12],
            e,
        )

    return count


# ---------------------------------------------------------------------------
# Batch combined task
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="analyze_speakers_combined_batch",
    priority=GPUPriority.ADMIN_MIGRATION,
    soft_time_limit=1800,
    time_limit=2100,
)
def analyze_speakers_combined_batch_task(
    self,
    file_uuids: list[str],
    batch_index: int = 0,
    total_batches: int = 1,
    total_files: int = 0,
    user_id: int | None = None,
):
    """Batch version using pipelined I/O + multi-GPU workers for combined analysis."""
    from app.services.speaker_analysis_models import EmbeddingModelAdapter
    from app.services.speaker_analysis_models import GenderModelAdapter
    from app.services.speaker_analysis_models import MultiModelRunner
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.migration_pipeline import prepare_file
    from app.tasks.migration_pipeline import process_batch_pipelined

    logger.info(
        "Combined batch %d/%d: processing %d files",
        batch_index + 1,
        total_batches,
        len(file_uuids),
    )

    if not combined_migration_progress.is_running():
        logger.warning("Combined migration stopped — aborting batch")
        return {"status": "stopped", "batch_index": batch_index}

    target_user = user_id or 1
    tracker = ProgressTracker(
        task_type="combined_speaker_migration",
        user_id=target_user,
        total=total_files,
    )
    existing_state = ProgressTracker.get_state("combined_speaker_migration", target_user)
    if existing_state:
        tracker.resume_from_state(existing_state)
    else:
        tracker.start(message="Processing combined speaker analysis...")

    # Phase 1: Prepare files
    prepared_files: list[tuple[str, PreparedFile]] = []
    for file_uuid in file_uuids:
        if not combined_migration_progress.is_running():
            break
        try:
            prepared = prepare_file(file_uuid, include_profile=True)
            if prepared is None:
                combined_migration_progress.increment_processed(success=True)
                _emit_combined_progress(tracker, target_user, total_files)
                continue
            prepared_files.append((file_uuid, prepared))
        except Exception as e:
            logger.error("Failed to prepare %s: %s", file_uuid, e)
            combined_migration_progress.increment_processed(success=False, file_uuid=file_uuid)
            _emit_combined_progress(tracker, target_user, total_files, failed_item=file_uuid)

    if not prepared_files:
        return {"status": "empty", "batch_index": batch_index}

    # Phase 2: Pipelined I/O + GPU inference (models cached per process)
    from app.services.speaker_attribute_service import get_cached_attribute_service

    embedding_service = get_cached_embedding_service()
    attr_service = get_cached_attribute_service()

    runner = MultiModelRunner(
        [
            EmbeddingModelAdapter(embedding_service),
            GenderModelAdapter(attr_service),
        ]
    )

    def on_success(fuuid: str) -> None:
        combined_migration_progress.increment_processed(success=True)
        _emit_combined_progress(tracker, target_user, total_files)

    def on_failure(fuuid: str, exc: Exception | None) -> None:
        combined_migration_progress.increment_processed(success=False, file_uuid=fuuid)
        _emit_combined_progress(tracker, target_user, total_files, failed_item=fuuid)

    process_batch_pipelined(
        prepared_files=prepared_files,
        runner=runner,
        result_writer=_combined_result_writer,
        is_running_check=combined_migration_progress.is_running,
        on_file_success=on_success,
        on_file_failure=on_failure,
        min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
    )

    # Check completion
    status = combined_migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0) or total_files
    is_complete = processed >= total and total > 0

    if is_complete and combined_migration_progress.complete_migration(success=True):
        logger.info("All %d files processed for combined speaker analysis", total)
        failed_files = status.get("failed_files", [])
        tracker.complete(message=f"Processed {total} files")
        send_ws_event(
            user_id or 1,
            NOTIFICATION_TYPE_COMBINED_MIGRATION_COMPLETE,
            {
                "status": "complete",
                "total_files": total,
                "failed_files": failed_files,
                "success_count": total - len(failed_files),
            },
        )

    # Free intermediate CUDA tensors for follow-on tasks
    from app.tasks.migration_pipeline import cleanup_gpu_memory

    cleanup_gpu_memory()

    return {"status": "success", "batch_index": batch_index}


# ---------------------------------------------------------------------------
# Orchestrator task
# ---------------------------------------------------------------------------


@celery_app.task(bind=True, name="migrate_speakers_combined", priority=CPUPriority.ADMIN_BATCH)
def migrate_speakers_combined_task(self, user_id: int):
    """Orchestrator: dispatch batches for combined embedding + gender migration.

    Processes all completed files, running both models on each file's audio.
    """
    from app.models.media import FileStatus
    from app.models.media import MediaFile

    task_id = self.request.id
    logger.info("Starting combined speaker migration: task_id=%s", task_id)

    if combined_migration_progress.is_running():
        return {
            "status": "skipped",
            "message": "Combined migration already in progress",
        }

    # Atomic lock to prevent double-dispatch from concurrent /start calls
    r = get_redis()
    lock_key = f"{combined_migration_progress.key_prefix}:orchestrator_lock"
    if not r.set(lock_key, task_id, nx=True, ex=3600):
        return {
            "status": "skipped",
            "message": "Combined migration orchestrator already starting",
        }

    try:
        with session_scope() as db:
            rows = db.query(MediaFile.uuid).filter(MediaFile.status == FileStatus.COMPLETED).all()
            total_files = len(rows)

            if total_files == 0:
                return {"status": "skipped", "message": "No files to process"}

            file_uuids = [str(r[0]) for r in rows]

        combined_migration_progress.start_migration(total_files=total_files, task_id=task_id)

        send_ws_event(
            user_id or 1,
            NOTIFICATION_TYPE_COMBINED_MIGRATION_PROGRESS,
            {
                "processed_files": 0,
                "total_files": total_files,
                "failed_files": [],
                "progress": 0,
                "running": True,
            },
        )

        batches = [file_uuids[i : i + _BATCH_SIZE] for i in range(0, len(file_uuids), _BATCH_SIZE)]

        batch_task_ids: list[str] = []
        batch_task_ids_key = f"{combined_migration_progress.key_prefix}:batch_task_ids"
        for batch_idx, batch in enumerate(batches):
            result = analyze_speakers_combined_batch_task.apply_async(
                kwargs={
                    "file_uuids": batch,
                    "batch_index": batch_idx,
                    "total_batches": len(batches),
                    "total_files": total_files,
                    "user_id": user_id,
                },
                priority=GPUPriority.ADMIN_MIGRATION,
            )
            batch_task_ids.append(result.id)
            # Persist after EVERY dispatch, not just once at the end — if a later
            # apply_async raises, the ids already dispatched must still be
            # revocable by the stop endpoint instead of orphaned.
            try:
                r.set(batch_task_ids_key, json.dumps(batch_task_ids), ex=7200)
            except Exception as e:
                logger.warning("Failed to store batch task IDs: %s", e)

        logger.info(
            "Dispatched %d combined migration batches for %d files",
            len(batches),
            total_files,
        )

        return {
            "status": "started",
            "message": f"Dispatched {len(batches)} batches ({total_files} files)",
            "total_files": total_files,
            "task_id": task_id,
        }

    except Exception as e:
        logger.error("Orchestrator failed: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        r.delete(lock_key)


# ---------------------------------------------------------------------------
# Progress helper
# ---------------------------------------------------------------------------


def _emit_combined_progress(
    tracker: ProgressTracker,
    user_id: int,
    total_files: int,
    failed_item: str | None = None,
) -> None:
    """Emit combined migration progress notification."""
    status = combined_migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0) or total_files
    failed_files = status.get("failed_files", [])

    emit_progress_notification(
        tracker=tracker,
        processed=processed,
        user_id=user_id,
        notification_type=NOTIFICATION_TYPE_COMBINED_MIGRATION_PROGRESS,
        extra_data={
            "processed_files": processed,
            "total_files": total,
            "failed_files": failed_files,
            "running": processed < total,
        },
        message=f"Processed {processed} of {total} files",
        failed_item=failed_item,
    )
