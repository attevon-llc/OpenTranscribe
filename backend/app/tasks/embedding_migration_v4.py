"""
Speaker embedding migration task for v3->v4 upgrade.

This module provides the migration infrastructure for upgrading speaker
embeddings from PyAnnote v3 (512-dim) to v4 (256-dim WeSpeaker).

Uses the unified migration_pipeline for I/O pipelining and GPU workers,
and speaker_analysis_models.EmbeddingModelAdapter for model abstraction.

Key features:
- Batched extraction (25 files/task) with warm-cached embedding model
- Prefetch pipeline: I/O threads download next files while GPU processes current
- Skip logic for files already migrated to v4
- Bulk OpenSearch writes instead of individual calls
- Celery priorities ensure migration tasks run before transcription
- Safe finalize with count validation, backup, and finally-block cleanup
"""

import json
import logging

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_MIGRATION_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_MIGRATION_PROGRESS
from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
from app.core.pipeline_constants import FILES_PER_BATCH_TASK
from app.core.queues import CPUPriority
from app.core.queues import GPUPriority
from app.core.queues import UtilityPriority
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.embedding_mode_service import MODE_V4
from app.services.embedding_mode_service import EmbeddingModeService
from app.services.migration_admin_notify import notify_migration_admin
from app.services.migration_progress_service import migration_progress
from app.tasks.embedding_migration_v4_scope import _clear_v4_index
from app.tasks.embedding_migration_v4_scope import _file_skipped_by_policy
from app.tasks.embedding_migration_v4_scope import _get_already_migrated_file_ids
from app.tasks.embedding_migration_v4_scope import get_migration_status
from app.tasks.embedding_migration_v4_writer import _embedding_result_writer
from app.tasks.migration_pipeline import PreparedFile

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="check_migration_status", priority=UtilityPriority.BACKGROUND)
def check_migration_status_task(self):
    """Check the current embedding migration status."""
    return get_migration_status()


# ---------------------------------------------------------------------------
# Orchestrator task
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="migrate_speaker_embeddings_to_v4",
    priority=CPUPriority.ADMIN_BATCH,
)
def migrate_speaker_embeddings_v4_task(
    self,
    user_id: int | None = None,
    force: bool = False,
):
    """Orchestrate migration of all speaker embeddings to v4.

    Acquires the migration lock (pausing transcription), queries completed
    files, filters out already-migrated ones, and dispatches batched GPU
    tasks (25 files per task).
    """
    from app.services.opensearch_service import create_speaker_index_v4

    task_id = self.request.id
    action = "force re-migration" if force else "migration"
    logger.info(f"Starting speaker embedding {action} to v4: task_id={task_id}")

    # Prevent concurrent migrations of the same type
    if migration_progress.is_running():
        logger.warning("Migration already in progress, skipping")
        return {
            "status": "skipped",
            "message": "A migration is already in progress",
            "existing_status": migration_progress.get_status(),
        }

    # Check current mode (allow force even if already v4)
    current_mode = EmbeddingModeService.detect_mode()
    if current_mode == MODE_V4 and not force:
        logger.info("Already in v4 mode, no migration needed")
        return {"status": "skipped", "message": "Already using v4 embeddings"}

    # Force mode: delete all existing v4 documents for clean re-extraction
    if force:
        _clear_v4_index()

    # Create the v4 staging index (no-op if already exists)
    if not create_speaker_index_v4():
        logger.error("Failed to create v4 staging index")
        return {"status": "error", "message": "Failed to create v4 staging index"}

    # Get completed files from DB (project only id + uuid to avoid loading JSONB)
    with session_scope() as db:
        query = db.query(MediaFile.id, MediaFile.uuid).filter(
            MediaFile.status == FileStatus.COMPLETED
        )
        if user_id:
            query = query.filter(MediaFile.user_id == user_id)
        media_rows = query.all()

        # Filter out files already in v4 index (force mode: nothing to skip)
        if force:
            files_to_migrate = list(media_rows)
            skipped = 0
        else:
            already_migrated = _get_already_migrated_file_ids()
            files_to_migrate = [f for f in media_rows if f.id not in already_migrated]
            skipped = len(media_rows) - len(files_to_migrate)

        total_files = len(files_to_migrate)
        logger.info(
            f"Found {len(media_rows)} completed files, "
            f"{skipped} already migrated, {total_files} to process"
        )

        if total_files == 0:
            return {
                "status": "success",
                "message": f"No files to migrate ({skipped} already done)",
                "migrated": 0,
                "skipped": skipped,
            }

        # Start progress tracking
        migration_progress.start_migration(total_files=total_files, task_id=task_id)

        # Acquire the migration lock (issue #657, defect 4). Previously this
        # docstring claimed the lock was acquired here and it never was -
        # migration_lock_service.py had zero production callers. Live
        # transcription and this migration's GPU batches both resolve the
        # embedding model through the same (mode, model_name)-keyed cache
        # (speaker_embedding_service.get_cached_embedding_service); with no
        # lock, the two race it and pay a 40-60s reload on every alternation.
        # The transcription voiceprint stage (speaker_embedding_task) checks
        # this lock and waits briefly rather than racing it.
        from app.services.migration_lock_service import migration_lock

        migration_lock.activate()

        # Initialize unified progress tracker for ETA
        from app.services.progress_tracker import ProgressTracker

        tracker = ProgressTracker(
            task_type="migration",
            user_id=user_id or 1,
            total=total_files,
        )
        tracker.start(message="Starting embedding migration...")

        notify_migration_admin(
            user_id,
            NOTIFICATION_TYPE_MIGRATION_PROGRESS,
            {
                "processed_files": 0,
                "total_files": total_files,
                "failed_files": [],
                "progress": 0,
                "running": True,
            },
        )

        # Build batches of file UUIDs
        file_uuids = [str(f.uuid) for f in files_to_migrate]

    step = FILES_PER_BATCH_TASK
    batches = [file_uuids[i : i + step] for i in range(0, len(file_uuids), step)]

    batch_task_ids = []
    for batch_idx, batch in enumerate(batches):
        result = extract_v4_embeddings_batch_task.apply_async(
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

    # Store batch task IDs for revocation on stop
    try:
        from app.core.redis import get_redis

        r = get_redis()
        r.set("embedding_migration:batch_task_ids", json.dumps(batch_task_ids), ex=86400)
    except Exception as e:
        logger.warning("Failed to store batch task IDs: %s", e)

    logger.info(f"Dispatched {len(batches)} batch tasks for {total_files} files")

    # Notify frontend that batches are queued and waiting for a GPU worker.
    # The first WS event (above) had no message; this one tells the user why
    # progress is stuck at 0% — GPU may be busy with another task.
    notify_migration_admin(
        user_id,
        NOTIFICATION_TYPE_MIGRATION_PROGRESS,
        {
            "processed_files": 0,
            "total_files": total_files,
            "failed_files": [],
            "progress": 0,
            "running": True,
            "message": f"Queued — {len(batches)} batches waiting for GPU worker",
        },
    )

    return {
        "status": "in_progress",
        "message": f"Dispatched {len(batches)} batch tasks ({total_files} files, {skipped} skipped)",
        "total_files": total_files,
        "skipped": skipped,
        "task_id": task_id,
    }


# ---------------------------------------------------------------------------
# Batched extraction task with unified pipeline
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True, name="extract_v4_embeddings_batch", priority=GPUPriority.ADMIN_MIGRATION
)
def extract_v4_embeddings_batch_task(
    self,
    file_uuids: list[str],
    batch_index: int = 0,
    total_batches: int = 1,
    total_files: int = 0,
    user_id: int | None = None,
):
    """Extract v4 embeddings for a batch of media files.

    Uses the unified migration_pipeline with EmbeddingModelAdapter for
    multi-model GPU pipelining. The primary embedding model is loaded ONCE
    and kept warm across batches.
    """
    from app.services.speaker_analysis_models import EmbeddingModelAdapter
    from app.services.speaker_analysis_models import MultiModelRunner
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.migration_pipeline import prepare_file
    from app.tasks.migration_pipeline import process_batch_pipelined

    logger.info(f"Batch {batch_index + 1}/{total_batches}: processing {len(file_uuids)} files")

    # Get warm-cached embedding service (loaded once, reused across batches)
    embedding_service = get_cached_embedding_service(mode=MODE_V4)
    runner = MultiModelRunner([EmbeddingModelAdapter(embedding_service)])

    # Initialize unified progress tracker for ETA
    from app.services.progress_tracker import ProgressTracker

    target_user = user_id or 1
    tracker = ProgressTracker(
        task_type="migration",
        user_id=target_user,
        total=total_files,
    )
    existing_tracker = ProgressTracker.get_state("migration", target_user)
    if existing_tracker:
        tracker.resume_from_state(existing_tracker)

    # Phase 1: Prepare all files (presigned URLs + DB queries — fast)
    files_with_speakers: list[tuple[str, PreparedFile]] = []
    for fuuid in file_uuids:
        if not migration_progress.is_running():
            logger.warning("Migration stopped — aborting batch")
            break
        try:
            prepared = prepare_file(fuuid, include_profile=True)
            if prepared is None:
                # No speakers — still counts as processed
                migration_progress.increment_processed(success=True)
                _emit_progress(tracker, target_user, total_files)
            elif _file_skipped_by_policy(prepared.media_file_id):
                # Privacy policy (#1280): no GPU time is spent on a scope that is off.
                migration_progress.increment_processed(success=True)
                _emit_progress(tracker, target_user, total_files)
            else:
                files_with_speakers.append((fuuid, prepared))
        except Exception as e:
            logger.error(f"Failed to prepare file {fuuid}: {e}")
            migration_progress.increment_processed(success=False, file_uuid=fuuid)
            _emit_progress(tracker, target_user, total_files, failed_item=fuuid)

    # Phase 2: Multi-model pipelined GPU extraction
    if files_with_speakers:

        def on_success(fuuid: str) -> None:
            migration_progress.increment_processed(success=True)
            _emit_progress(tracker, target_user, total_files)

        def on_failure(fuuid: str, exc: Exception | None) -> None:
            migration_progress.increment_processed(success=False, file_uuid=fuuid)
            _emit_progress(tracker, target_user, total_files, failed_item=fuuid)

        process_batch_pipelined(
            prepared_files=files_with_speakers,
            runner=runner,
            result_writer=_embedding_result_writer,
            is_running_check=migration_progress.is_running,
            on_file_success=on_success,
            on_file_failure=on_failure,
            min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
        )

    # Check completion
    status = migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0) or total_files
    failed_files = status.get("failed_files", [])
    is_complete = processed >= total and total > 0

    if is_complete and migration_progress.complete_migration(success=True):
        logger.info(f"All {total} migration files processed")
        tracker.complete(message="Migration complete")
        notify_migration_admin(
            user_id,
            NOTIFICATION_TYPE_MIGRATION_COMPLETE,
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

    return {
        "status": "success",
        "batch_index": batch_index,
    }


def _emit_progress(
    tracker,
    user_id: int,
    total_files: int,
    failed_item: str | None = None,
) -> None:
    """Emit migration progress notification with ETA."""
    from app.services.progress_tracker import emit_progress_notification

    status = migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0) or total_files
    failed_files = status.get("failed_files", [])

    emit_progress_notification(
        tracker=tracker,
        processed=processed,
        user_id=user_id,
        notification_type=NOTIFICATION_TYPE_MIGRATION_PROGRESS,
        extra_data={
            "processed_files": processed,
            "total_files": total,
            "failed_files": failed_files,
            "running": processed < total,
        },
        message=f"Processed {processed} of {total} files",
        failed_item=failed_item,
    )


# ---------------------------------------------------------------------------
# Legacy single-file task (kept for backward compat with any in-flight tasks)
# ---------------------------------------------------------------------------


@celery_app.task(bind=True, name="extract_v4_embeddings", priority=GPUPriority.ADMIN_MIGRATION)
def extract_v4_embeddings_task(
    self,
    file_uuid: str,
    task_index: int = 0,
    total_tasks: int = 1,
    user_id: int | None = None,
):
    """Legacy single-file extraction (kept for in-flight task compatibility).

    New migrations use extract_v4_embeddings_batch_task instead.
    """
    from app.services.speaker_analysis_models import EmbeddingModelAdapter
    from app.services.speaker_analysis_models import MultiModelRunner
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.migration_pipeline import prepare_file
    from app.tasks.migration_pipeline import process_batch_pipelined

    logger.info(f"[legacy] Extracting v4 embeddings for file {file_uuid}")

    embedding_service = get_cached_embedding_service(mode=MODE_V4)
    runner = MultiModelRunner([EmbeddingModelAdapter(embedding_service)])

    try:
        prepared = prepare_file(file_uuid, include_profile=True)
        if prepared:
            process_batch_pipelined(
                prepared_files=[(file_uuid, prepared)],
                runner=runner,
                result_writer=_embedding_result_writer,
                is_running_check=migration_progress.is_running,
                on_file_success=lambda _: migration_progress.increment_processed(  # type: ignore[arg-type]
                    success=True
                ),
                on_file_failure=lambda f, _: migration_progress.increment_processed(  # type: ignore[arg-type]
                    success=False, file_uuid=f
                ),
                min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
            )
        else:
            migration_progress.increment_processed(success=True)
    except Exception as e:
        logger.error(f"[legacy] Error migrating {file_uuid}: {e}")
        migration_progress.increment_processed(success=False, file_uuid=file_uuid)

    # Check completion
    status = migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0)
    if processed >= total and total > 0:
        migration_progress.complete_migration(success=True)

    # Free intermediate CUDA tensors for follow-on tasks
    from app.tasks.migration_pipeline import cleanup_gpu_memory

    cleanup_gpu_memory()

    return {"status": "success", "file_uuid": file_uuid}
