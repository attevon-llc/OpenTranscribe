"""
Celery tasks for bulk speaker attribute detection migration.

Orchestrator dispatches batched tasks to process existing files that
have not yet been analyzed for speaker attributes (gender, age).

Uses the unified migration_pipeline for I/O pipelining and GPU workers,
and speaker_analysis_models.GenderModelAdapter for model abstraction.
"""

import json
import logging

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_PROGRESS
from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
from app.core.constants import CPUPriority
from app.core.constants import GPUPriority
from app.db.session_utils import session_scope
from app.services.migration_progress_service import MigrationProgressService
from app.services.progress_tracker import ProgressTracker
from app.services.progress_tracker import emit_progress_notification
from app.services.speaker_analysis_models import SegmentResult
from app.tasks.migration_pipeline import PreparedFile
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

# Batch size — files per Celery task
_BATCH_SIZE = 25

# Separate progress tracker for speaker attribute migration
attribute_migration_progress = MigrationProgressService(key_prefix="speaker_attr_migration")


# ---------------------------------------------------------------------------
# File query
# ---------------------------------------------------------------------------


def _get_files_needing_attribute_detection(db) -> list:
    """Query completed files with speakers that have never had attributes predicted."""
    from sqlalchemy import exists

    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import Speaker

    return list(
        db.query(MediaFile)
        .filter(
            MediaFile.status == FileStatus.COMPLETED,
            exists().where(
                (Speaker.media_file_id == MediaFile.id)
                & (Speaker.attributes_predicted_at.is_(None))
            ),
        )
        .all()
    )


def _get_all_files_with_speakers(db) -> list:
    """Query all completed files that have speakers (for force reprocess)."""
    from sqlalchemy import exists

    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import Speaker

    return list(
        db.query(MediaFile)
        .filter(
            MediaFile.status == FileStatus.COMPLETED,
            exists().where(Speaker.media_file_id == MediaFile.id),
        )
        .all()
    )


def _reset_all_speaker_attributes(db) -> int:
    """Reset attributes_predicted_at for all speakers to enable full reprocessing.

    Returns the number of speakers reset.
    """
    from app.models.media import Speaker

    count = (
        db.query(Speaker)
        .filter(Speaker.attributes_predicted_at.isnot(None))
        .update(
            {
                Speaker.attributes_predicted_at: None,
                Speaker.predicted_gender: None,
                Speaker.predicted_age_range: None,
                Speaker.attribute_confidence: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return int(count)


# ---------------------------------------------------------------------------
# Result writer for the unified pipeline
# ---------------------------------------------------------------------------


def _gender_result_writer(
    prepared: PreparedFile,
    results_by_model: dict[str, list[SegmentResult]],
) -> int:
    """Write gender detection results to PostgreSQL.

    Marks ALL speakers as attempted (attributes_predicted_at = now), even
    those with no valid segments, so they don't perpetually show as pending.
    """
    from datetime import datetime
    from datetime import timezone

    from app.models.media import Speaker

    gender_results = results_by_model.get("gender", [])

    # Gather per-speaker results
    speaker_probs: dict[int, dict[str, float]] = {}
    speaker_clip_counts: dict[int, int] = {}

    for sr in gender_results:
        gender, confidence = sr.value  # tuple[str, float]
        if sr.speaker_id not in speaker_probs:
            speaker_probs[sr.speaker_id] = {"male": 0.0, "female": 0.0}
            speaker_clip_counts[sr.speaker_id] = 0
        speaker_probs[sr.speaker_id][gender] += confidence
        speaker_clip_counts[sr.speaker_id] += 1

    # Write to DB — mark ALL speakers as attempted
    now = datetime.now(timezone.utc)
    predicted_count = 0

    with session_scope() as db:
        speakers = db.query(Speaker).filter(Speaker.media_file_id == prepared.media_file_id).all()
        speaker_by_id = {int(s.id): s for s in speakers}

        for sid, speaker_obj in speaker_by_id.items():
            probs = speaker_probs.get(sid)
            if probs:
                clips = speaker_clip_counts[sid]
                final_gender = max(probs, key=lambda k: probs[k])
                final_conf = probs[final_gender] / clips

                speaker_obj.predicted_gender = final_gender
                speaker_obj.predicted_age_range = None
                speaker_obj.attribute_confidence = {
                    "gender": round(final_conf, 3),
                }
                predicted_count += 1
            else:
                speaker_obj.predicted_gender = None
                speaker_obj.predicted_age_range = None
                speaker_obj.attribute_confidence = {"gender": 0.0}

            speaker_obj.attributes_predicted_at = now

        db.commit()

    return predicted_count


# ---------------------------------------------------------------------------
# Progress helper
# ---------------------------------------------------------------------------


def _emit_attr_progress(
    tracker: ProgressTracker,
    user_id: int,
    total_files: int,
    failed_item: str | None = None,
) -> None:
    """Emit a progress notification with ETA using the unified tracker."""
    status = attribute_migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0) or total_files
    failed_files = status.get("failed_files", [])

    emit_progress_notification(
        tracker=tracker,
        processed=processed,
        user_id=user_id,
        notification_type=NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_PROGRESS,
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
# Celery tasks
# ---------------------------------------------------------------------------


@celery_app.task(bind=True, name="migrate_speaker_attributes", priority=CPUPriority.ADMIN_BATCH)
def migrate_speaker_attributes_task(self, user_id: int, force: bool = False):
    """Orchestrate bulk speaker attribute detection for existing files."""
    task_id = self.request.id
    logger.info("Starting speaker attribute migration: task_id=%s", task_id)

    if attribute_migration_progress.is_running():
        logger.warning("Speaker attribute migration already in progress")
        return {
            "status": "skipped",
            "message": "Attribute migration already in progress",
        }

    # Atomic lock
    lock_key = f"{attribute_migration_progress.key_prefix}:orchestrator_lock"
    r = attribute_migration_progress.redis_client
    if r and not r.set(lock_key, task_id, nx=True, ex=3600):
        logger.warning("Another orchestrator already holds the lock")
        return {
            "status": "skipped",
            "message": "Attribute migration already starting",
        }

    try:
        # _reset_all_speaker_attributes does its own explicit commit (preserved);
        # session_scope's exit commit is then a harmless no-op. Reads use the same scope.
        with session_scope() as db:
            if force:
                reset_count = _reset_all_speaker_attributes(db)
                logger.info("Force mode: reset %d speaker attributes", reset_count)
                files_to_process = _get_all_files_with_speakers(db)
            else:
                files_to_process = _get_files_needing_attribute_detection(db)
            total_files = len(files_to_process)

            if total_files == 0:
                logger.info("No files need speaker attribute detection")
                if r:
                    r.delete(lock_key)
                return {"status": "skipped", "message": "No files to process"}

            file_uuids = [str(f.uuid) for f in files_to_process]

        attribute_migration_progress.start_migration(total_files=total_files, task_id=task_id)

        send_ws_event(
            user_id or 1,
            NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_PROGRESS,
            {
                "processed_files": 0,
                "total_files": total_files,
                "failed_files": [],
                "progress": 0,
                "running": True,
            },
        )

        batches = [file_uuids[i : i + _BATCH_SIZE] for i in range(0, len(file_uuids), _BATCH_SIZE)]

        batch_task_ids = []
        for batch_idx, batch in enumerate(batches):
            result = detect_speaker_attributes_batch_task.apply_async(
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

        if r:
            r.set(
                f"{attribute_migration_progress.key_prefix}:batch_task_ids",
                json.dumps(batch_task_ids),
                ex=86400,
            )

        # Release lock — orchestrator's job is done once batches are dispatched
        if r:
            r.delete(lock_key)

        logger.info(
            "Dispatched %d attribute detection batches for %d files",
            len(batches),
            total_files,
        )

        # Notify frontend that batches are queued and waiting for a GPU worker.
        send_ws_event(
            user_id or 1,
            NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_PROGRESS,
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
            "status": "started",
            "message": f"Dispatched {len(batches)} batches ({total_files} files)",
            "total_files": total_files,
            "task_id": task_id,
        }

    except Exception as e:
        logger.error("Orchestrator failed: %s", e, exc_info=True)
        if r:
            r.delete(lock_key)
        return {"status": "error", "message": str(e)}


@celery_app.task(
    bind=True,
    name="detect_speaker_attributes_batch",
    priority=GPUPriority.ADMIN_MIGRATION,
    soft_time_limit=1800,
    time_limit=2100,
)
def detect_speaker_attributes_batch_task(
    self,
    file_uuids: list[str],
    batch_index: int = 0,
    total_batches: int = 1,
    total_files: int = 0,
    user_id: int | None = None,
):
    """Process a batch of files for speaker attribute detection.

    Uses the unified migration_pipeline with GenderModelAdapter.
    """
    from app.services.speaker_analysis_models import GenderModelAdapter
    from app.services.speaker_analysis_models import MultiModelRunner
    from app.tasks.migration_pipeline import prepare_file
    from app.tasks.migration_pipeline import process_batch_pipelined

    logger.info(
        "Attribute batch %d/%d: processing %d files",
        batch_index + 1,
        total_batches,
        len(file_uuids),
    )

    if not attribute_migration_progress.is_running():
        logger.warning("Attribute migration stopped — aborting batch")
        return {"status": "stopped", "batch_index": batch_index}

    # Initialize progress tracker
    target_user = user_id or 1
    tracker = ProgressTracker(
        task_type="attribute_migration",
        user_id=target_user,
        total=total_files,
    )
    existing_state = ProgressTracker.get_state("attribute_migration", target_user)
    if existing_state:
        tracker.resume_from_state(existing_state)
    else:
        tracker.start(message="Processing speaker attributes...")

    # Phase 1: Prepare all files
    prepared_files: list[tuple[str, PreparedFile]] = []
    for file_uuid in file_uuids:
        if not attribute_migration_progress.is_running():
            break
        try:
            prepared = prepare_file(file_uuid)
            if prepared is None:
                attribute_migration_progress.increment_processed(success=True)
                _emit_attr_progress(tracker, target_user, total_files)
                logger.debug("%s… no speakers, skipped", file_uuid[:12])
                continue
            prepared_files.append((file_uuid, prepared))
        except Exception as e:
            logger.error("Failed to prepare %s: %s", file_uuid, e)
            attribute_migration_progress.increment_processed(success=False, file_uuid=file_uuid)
            _emit_attr_progress(tracker, target_user, total_files, failed_item=file_uuid)

    if not prepared_files:
        logger.info("Batch %d: no files to process after preparation", batch_index)
        _emit_attr_progress(tracker, target_user, total_files)
        return {"status": "empty", "batch_index": batch_index}

    # Phase 2: Pipelined I/O + GPU inference (model cached per process)
    from app.services.speaker_attribute_service import get_cached_attribute_service

    service = get_cached_attribute_service()
    runner = MultiModelRunner([GenderModelAdapter(service)])

    def on_success(fuuid: str) -> None:
        attribute_migration_progress.increment_processed(success=True)
        _emit_attr_progress(tracker, target_user, total_files)

    def on_failure(fuuid: str, exc: Exception | None) -> None:
        attribute_migration_progress.increment_processed(success=False, file_uuid=fuuid)
        _emit_attr_progress(tracker, target_user, total_files, failed_item=fuuid)

    process_batch_pipelined(
        prepared_files=prepared_files,
        runner=runner,
        result_writer=_gender_result_writer,
        is_running_check=attribute_migration_progress.is_running,
        on_file_success=on_success,
        on_file_failure=on_failure,
        min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
    )

    # Send final batch progress
    _emit_attr_progress(tracker, target_user, total_files)

    # Check completion
    status = attribute_migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0) or total_files
    is_complete = processed >= total and total > 0

    if is_complete and attribute_migration_progress.complete_migration(success=True):
        logger.info("All %d files processed for speaker attributes", total)
        failed_files = status.get("failed_files", [])
        tracker.complete(message=f"Processed {total} files for speaker attributes")
        send_ws_event(
            user_id or 1,
            NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_COMPLETE,
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
