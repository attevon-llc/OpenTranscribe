"""
API endpoints for bulk speaker attribute detection migration.

Allows admins to trigger bulk processing of existing files for
speaker gender/age detection.
"""

import json
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.db.base import get_db
from app.models.user import User
from app.tasks.speaker_attribute_migration_task import attribute_migration_progress

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/status")
def get_attribute_migration_status(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """Get the current speaker attribute migration status.

    Returns file counts (total, processed, pending) and progress info.
    """
    try:
        from sqlalchemy import case
        from sqlalchemy import exists
        from sqlalchemy import func

        from app.models.media import FileStatus
        from app.models.media import MediaFile
        from app.models.media import Speaker as SpeakerModel

        # Single query: count total files with speakers AND pending files
        # (replaces full ORM hydration that loaded megabytes of JSONB data)
        has_speakers = exists().where(SpeakerModel.media_file_id == MediaFile.id)
        has_unpredicted = exists().where(
            (SpeakerModel.media_file_id == MediaFile.id)
            & (SpeakerModel.attributes_predicted_at.is_(None))
        )

        row = (
            db.query(
                func.count(MediaFile.id).label("total"),
                func.sum(case((has_unpredicted, 1), else_=0)).label("pending"),
            )
            .filter(MediaFile.status == FileStatus.COMPLETED, has_speakers)
            .one()
        )
        total_with_speakers = row.total or 0
        pending_count = int(row.pending or 0)

        # Get progress from Redis
        progress = attribute_migration_progress.get_status()

        # Include ETA from the unified progress tracker (if running)
        if progress.get("running"):
            from app.services.progress_tracker import ProgressTracker

            tracker_state = ProgressTracker.get_state("attribute_migration", current_user.id)
            if tracker_state and tracker_state.eta_seconds is not None:
                progress["eta_seconds"] = tracker_state.eta_seconds

        return {
            "total_files": total_with_speakers,
            "pending_files": pending_count,
            "progress": progress,
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error getting attribute migration status: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/start")
def start_attribute_migration(
    force: bool = False,
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """Start bulk speaker attribute detection.

    Args:
        force: If True, reset all speaker attributes and reprocess all files.
    """
    from app.tasks.speaker_attribute_migration_task import migrate_speaker_attributes_task

    # Guard: THIS migration type already running
    if attribute_migration_progress.is_running():
        return {
            "status": "already_running",
            "message": "Speaker attribute migration is already in progress",
            "progress": attribute_migration_progress.get_status(),
        }

    try:
        # Dispatch — lock is ref-counted, multiple migrations can coexist
        task = migrate_speaker_attributes_task.delay(user_id=current_user.id, force=force)

        logger.info("Started speaker attribute migration task: %s", task.id)
        return {
            "status": "started",
            "task_id": task.id,
            "message": "Speaker attribute migration dispatched.",
        }

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error starting attribute migration: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/stop")
def stop_attribute_migration(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """Stop a running speaker attribute migration.

    Marks migration as stopped and revokes pending batch tasks so they
    don't continue processing on the GPU queue.
    """
    try:
        if not attribute_migration_progress.is_running():
            return {
                "status": "not_running",
                "message": "No attribute migration is currently running",
            }

        success = attribute_migration_progress.force_stop()

        # Revoke pending batch tasks from the GPU queue
        revoked = 0
        try:
            r = attribute_migration_progress.redis_client
            if r:
                batch_ids_json = r.get(f"{attribute_migration_progress.key_prefix}:batch_task_ids")
                if batch_ids_json:
                    batch_ids = json.loads(batch_ids_json)
                    from app.core.celery import celery_app

                    for tid in batch_ids:
                        celery_app.control.revoke(tid, terminate=True)
                        revoked += 1
                    r.delete(f"{attribute_migration_progress.key_prefix}:batch_task_ids")
                    logger.info("Revoked %d pending batch tasks", revoked)
        except Exception as e:
            logger.warning("Failed to revoke batch tasks: %s", e)

        # Clear ProgressTracker so queue status clears immediately
        try:
            from app.services.progress_tracker import ProgressTracker

            tracker = ProgressTracker(
                task_type="attribute_migration",
                user_id=current_user.id,
                total=0,
            )
            tracker.complete(message="Stopped by user")
        except Exception as e:
            logger.warning("Failed to clear progress tracker: %s", e)

        # Notify frontend via WebSocket
        try:
            from app.core.constants import NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_COMPLETE
            from app.utils.websocket_notify import send_ws_event

            send_ws_event(
                current_user.id,
                NOTIFICATION_TYPE_ATTRIBUTE_MIGRATION_COMPLETE,
                {"status": "stopped", "message": "Migration stopped by user"},
            )
        except Exception as e:
            logger.warning("Failed to send stop WS event: %s", e)

        if success:
            logger.warning("Speaker attribute migration stopped by user")
            return {
                "status": "stopped",
                "message": f"Speaker attribute migration stopped. {revoked} pending tasks revoked.",
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
        logger.error("Error stopping attribute migration: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.delete("/progress")
def clear_attribute_progress(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """Clear stale attribute migration progress data from Redis."""
    try:
        if attribute_migration_progress.is_running():
            return {
                "status": "error",
                "message": "Cannot clear progress while migration is running.",
            }

        success = attribute_migration_progress.clear_status()

        if success:
            return {"status": "cleared", "message": "Progress tracking cleared."}
        else:
            return {"status": "error", "message": "Failed to clear progress."}

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error clearing attribute progress: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e
