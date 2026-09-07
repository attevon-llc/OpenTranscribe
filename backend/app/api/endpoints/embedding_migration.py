"""
API endpoints for speaker embedding migration (v3 to v4).

Includes safeguards against concurrent migrations and progress tracking.
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.db.base import get_db
from app.models.user import User
from app.services.embedding_mode_service import EmbeddingModeService
from app.services.migration_progress_service import migration_progress

router = APIRouter()
logger = logging.getLogger(__name__)


def _compute_ground_truth_status(db: Session) -> dict | None:
    """Compute true migration state from OpenSearch + DB when Redis data is stale.

    Only counts files that have embeddable speakers (>= 0.5s segments).
    Files with no speakers or only sub-0.5s segments are excluded — they
    legitimately cannot produce embeddings and are not "stalled".

    Returns dict with stalled info or None if not stalled.
    """
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.tasks.embedding_migration_v4 import _count_embeddable_speakers_per_file
    from app.tasks.embedding_migration_v4 import _get_already_migrated_file_ids

    try:
        completed_ids = [
            int(row[0])
            for row in db.query(MediaFile.id).filter(MediaFile.status == FileStatus.COMPLETED).all()
        ]

        if not completed_ids:
            return None

        # Only count files that actually have embeddable speakers
        embeddable_counts = _count_embeddable_speakers_per_file(completed_ids)
        migratable_ids = {fid for fid, count in embeddable_counts.items() if count > 0}

        if not migratable_ids:
            return None

        already_migrated_ids = _get_already_migrated_file_ids()
        migrated_count = len(migratable_ids & already_migrated_ids)

        remaining = len(migratable_ids) - migrated_count
        if remaining <= 0:
            return None

        return {
            "stalled": True,
            "total_completed_files": len(migratable_ids),
            "migrated_files": migrated_count,
            "remaining_files": remaining,
        }
    except Exception as e:
        logger.warning("Could not compute ground truth status: %s", e)
        return None


@router.get("/status")
def get_migration_status(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """
    Get the current speaker embedding migration status.

    Returns information about:
    - Current embedding mode (v3 or v4)
    - Whether migration is needed
    - Document counts in each index
    - Progress tracking (if migration is running)
    - Stalled migration detection (when Redis data expired)

    Degrades gracefully when OpenSearch is unavailable: still 200, with
    ``{"status": "error", "message": "OpenSearch not available"}`` in place of
    the index fields; ``mode_info``/``progress``/``stalled`` are always present
    (mode defaults to v4, progress to the not-running envelope).
    """
    from app.tasks.embedding_migration_v4 import get_migration_status

    try:
        status = get_migration_status()

        # Add mode info
        status["mode_info"] = EmbeddingModeService.get_mode_info()

        # Add progress tracking info from Redis
        progress = migration_progress.get_status()
        status["progress"] = progress

        # Issue #657, defect 4: this was hardcoded False even after the lock
        # was actually wired up - now reports the real lock state.
        from app.services.migration_lock_service import migration_lock

        status["transcription_paused"] = migration_lock.is_active()

        # Issue #657, defect 6: the UI ETA used to multiply v3_document_count
        # (speaker DOCUMENTS) by a per-file constant, as if they were files -
        # overcounting by however many speakers the average file has.
        # completed_file_count is the actual unit the ETA formula needs.
        from app.models.media import FileStatus
        from app.models.media import MediaFile

        status["completed_file_count"] = (
            db.query(MediaFile).filter(MediaFile.status == FileStatus.COMPLETED).count()
        )

        # Detect stalled migration: v4 index has docs but mode is still v3
        # and Redis progress is empty/expired (no running migration)
        status["stalled"] = False
        if (
            not progress.get("running")
            and status.get("current_mode") == "v3"
            and status.get("v4_document_count", 0) > 0
        ):
            ground_truth = _compute_ground_truth_status(db)
            if ground_truth:
                status["stalled"] = True
                status["stalled_info"] = ground_truth

        # Include consistency status (Redis reads only — near zero cost)
        try:
            from app.tasks.speaker_embedding_consistency import get_embedding_consistency_status

            status["consistency_status"] = get_embedding_consistency_status()
        except Exception:
            # Optional Redis-backed enrichment of an otherwise complete status
            # payload — a missing sub-field must not fail the whole endpoint.
            logger.exception("Could not read embedding consistency status")
            status["consistency_status"] = None

        return status

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error getting migration status: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.get("/progress")
def get_migration_progress(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """
    Get detailed migration progress information.

    Returns:
    - running: Whether a migration is currently in progress
    - total_files: Total number of files to process
    - processed_files: Number of files processed so far
    - failed_files: List of file UUIDs that failed
    - started_at: When the migration started
    - completed_at: When the migration completed (if finished)
    - last_updated: Last progress update timestamp
    """
    try:
        return migration_progress.get_status()
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error getting migration progress: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/start")
def start_migration(
    user_id: int | None = None,
    force: bool = False,
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """Start the speaker embedding migration from v3 to v4.

    This is an async operation - it dispatches Celery tasks and returns immediately.
    Use the /status or /progress endpoints to monitor progress.

    Args:
        user_id: Optional user ID to migrate (None for all users)
        force: If True, clear existing v4 embeddings and re-extract all from
            scratch. Use after code changes that improve extraction quality.

    Returns:
        - status: "started", "skipped", or "error"
        - task_id: Celery task ID (if started)
        - message: Human-readable status message
        - progress: Current progress info (if migration already running)
    """
    from app.tasks.embedding_migration_v4 import migrate_speaker_embeddings_v4_task

    # Check if THIS migration type is already running
    if migration_progress.is_running():
        progress = migration_progress.get_status()
        return {
            "status": "already_running",
            "message": "Embedding migration is already in progress",
            "progress": progress,
        }

    # Check if already in v4 mode (skip unless force re-extract)
    current_mode = EmbeddingModeService.detect_mode()
    if current_mode == "v4" and not force:
        return {
            "status": "skipped",
            "message": "Already using v4 embeddings, no migration needed",
        }

    try:
        # Dispatch the migration task — lock is ref-counted, multiple migrations can coexist
        task = migrate_speaker_embeddings_v4_task.delay(
            user_id=user_id,
            force=force,
        )

        action = "Force re-migration" if force else "Migration"
        logger.info(f"{action} task started: {task.id}")

        return {
            "status": "started",
            "task_id": task.id,
            "message": f"{action} task dispatched. Check /status or /progress for updates.",
            "force": force,
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error starting migration: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/stop")
def stop_migration(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """
    Force stop a running migration.

    Note: This does not actually stop running Celery tasks - they will continue
    to completion. This endpoint marks the migration as stopped and prevents
    new tasks from being dispatched.

    For a full stop, you would need to revoke the Celery tasks manually.
    """
    try:
        if not migration_progress.is_running():
            return {
                "status": "not_running",
                "message": "No migration is currently running",
            }

        success = migration_progress.force_stop()

        # Release the migration lock (issue #657, defect 4) - a stopped
        # migration must not leave transcription backing off forever.
        from app.services.migration_lock_service import migration_lock

        migration_lock.deactivate()

        # Revoke any in-flight batch tasks
        try:
            from app.core.celery import celery_app
            from app.core.redis import get_redis

            r = get_redis()
            raw = r.get("embedding_migration:batch_task_ids")
            if raw:
                import json

                batch_ids = json.loads(raw)
                for tid in batch_ids:
                    celery_app.control.revoke(tid, terminate=True)
                r.delete("embedding_migration:batch_task_ids")
                logger.info("Revoked %d embedding migration batch tasks", len(batch_ids))
        except Exception as e:
            logger.warning("Failed to revoke batch tasks: %s", e)

        # Clear ProgressTracker so queue status clears immediately
        try:
            from app.services.progress_tracker import ProgressTracker

            tracker = ProgressTracker(
                task_type="migration",
                user_id=current_user.id,
                total=0,
            )
            tracker.complete(message="Stopped by user")
        except Exception as e:
            logger.warning("Failed to clear progress tracker: %s", e)

        # Notify frontend via WebSocket
        try:
            from app.core.constants import NOTIFICATION_TYPE_MIGRATION_COMPLETE
            from app.utils.websocket_notify import send_ws_event

            send_ws_event(
                current_user.id,
                NOTIFICATION_TYPE_MIGRATION_COMPLETE,
                {"status": "stopped", "message": "Migration stopped by user"},
            )
        except Exception as e:
            logger.warning("Failed to send stop WS event: %s", e)

        if success:
            logger.warning("Migration force stopped by user")
            return {
                "status": "stopped",
                "message": "Migration stopped and transcription lock released.",
            }
        else:
            return {
                "status": "error",
                "message": "Failed to stop migration",
            }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error stopping migration: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/finalize")
def finalize_migration(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """
    Finalize the v4 migration by swapping indices.

    This should be called after all extraction tasks complete.
    It will:
    1. Backup the current v3 index
    2. Swap the v4 index to be the primary
    3. Clear the mode cache
    4. Clear migration progress tracking

    Note: Check /progress endpoint to ensure all tasks are complete before calling.
    """
    from app.tasks.embedding_migration_v4 import finalize_v4_migration_task

    # Check if migration is still running
    progress = migration_progress.get_status()
    if progress.get("running"):
        processed = progress.get("processed_files", 0)
        total = progress.get("total_files", 0)
        return {
            "status": "still_running",
            "message": f"Migration still in progress ({processed}/{total} files processed). "
            "Wait for completion before finalizing.",
            "progress": progress,
        }

    try:
        task = finalize_v4_migration_task.delay(user_id=current_user.id)

        logger.info(f"Started finalization task: {task.id}")

        return {
            "status": "started",
            "task_id": task.id,
            "message": "Finalization task dispatched.",
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error starting finalization: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.delete("/progress")
def clear_progress(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """
    Clear the migration progress tracking from Redis.

    Use this to reset stale progress data if a migration was interrupted
    and you want to start fresh.

    Warning: Only use this if you are sure no migration is running.
    """
    try:
        if migration_progress.is_running():
            return {
                "status": "error",
                "message": "Cannot clear progress while migration is running. Use /stop first.",
            }

        success = migration_progress.clear_status()

        if success:
            return {
                "status": "cleared",
                "message": "Migration progress tracking cleared.",
            }
        else:
            return {
                "status": "error",
                "message": "Failed to clear progress. Redis may be unavailable.",
            }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error clearing progress: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/retry-failed")
def retry_failed_files(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """
    Retry migration for files that were not successfully migrated.

    Computes truly-missing files from DB + OpenSearch ground truth
    (does not rely on stale Redis failed_files list).
    """
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.tasks.embedding_migration_v4 import _get_already_migrated_file_ids
    from app.tasks.embedding_migration_v4 import extract_v4_embeddings_batch_task

    batch_size = 25

    # Guard: reject if already running
    if migration_progress.is_running():
        return {
            "status": "error",
            "message": "A migration is already in progress",
        }

    try:
        from app.tasks.embedding_migration_v4 import _count_embeddable_speakers_per_file

        # Compute truly-missing files from ground truth (project only id + uuid)
        completed_rows = (
            db.query(MediaFile.id, MediaFile.uuid)
            .filter(MediaFile.status == FileStatus.COMPLETED)
            .all()
        )
        already_migrated = _get_already_migrated_file_ids()

        # Only retry files that have embeddable speakers (>= 0.5s segments)
        embeddable_counts = _count_embeddable_speakers_per_file([f.id for f in completed_rows])

        missing_rows = [
            f
            for f in completed_rows
            if f.id not in already_migrated and embeddable_counts.get(f.id, 0) > 0
        ]
        if not missing_rows:
            return {
                "status": "skipped",
                "message": "No files need retry — all completed files have been migrated",
            }

        total_retry = len(missing_rows)
        file_uuids = [str(f.uuid) for f in missing_rows]

        migration_progress.start_migration(total_files=total_retry)

        # Dispatch batch tasks
        batches = [file_uuids[i : i + batch_size] for i in range(0, len(file_uuids), batch_size)]
        for batch_idx, batch in enumerate(batches):
            extract_v4_embeddings_batch_task.delay(
                file_uuids=batch,
                batch_index=batch_idx,
                total_batches=len(batches),
                total_files=total_retry,
            )

        logger.info(f"Retry: dispatched {len(batches)} batches for {total_retry} files")

        return {
            "status": "started",
            "message": f"Retrying {total_retry} files in {len(batches)} batches",
            "total_files": total_retry,
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error retrying failed files: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/force-complete")
def force_complete_migration(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """
    Force-mark the migration as complete, skipping any unprocessed files.

    Use this for genuinely unrecoverable files (e.g., missing from MinIO).
    """
    from app.core.constants import NOTIFICATION_TYPE_MIGRATION_COMPLETE
    from app.utils.websocket_notify import send_ws_event

    try:
        # Compute how many files are being skipped
        ground_truth = _compute_ground_truth_status(db)
        skipped_count = ground_truth["remaining_files"] if ground_truth else 0

        # Mark migration as complete
        migration_progress.complete_migration(success=True)

        # Release the migration lock (issue #657, defect 4). force-complete
        # only stops waiting on remaining files; without this the pause
        # point in transcription's embedding extraction would keep backing
        # off against a migration that is no longer actually running.
        from app.services.migration_lock_service import migration_lock

        migration_lock.deactivate()

        # Send completion notification
        send_ws_event(
            current_user.id,
            NOTIFICATION_TYPE_MIGRATION_COMPLETE,
            {
                "status": "force_completed",
                "skipped_files": skipped_count,
                "message": f"Migration force-completed, {skipped_count} files skipped",
            },
        )

        logger.warning(
            "Migration force-completed by %s, skipping %d files",
            current_user.email,
            skipped_count,
        )

        return {
            "status": "completed",
            "message": f"Migration marked as complete. {skipped_count} files skipped.",
            "skipped_files": skipped_count,
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error force-completing migration: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.get("/mode")
def get_embedding_mode(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """
    Get detailed information about the current embedding mode.
    """
    return EmbeddingModeService.get_mode_info()
