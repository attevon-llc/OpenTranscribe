"""
API endpoints for combined speaker analysis migration (embedding + gender).

Allows admins to trigger bulk processing of all files for both
embedding extraction and gender detection in a single pass.
"""

import json
import logging

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.core.redis import get_redis
from app.db.base import get_db
from app.models.user import User
from app.tasks.combined_speaker_analysis_task import combined_migration_progress
from app.utils.error_handlers import ErrorHandler

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/status")
def get_combined_migration_status(
    current_user: User = Depends(get_current_active_superuser),
):
    """Get the current combined speaker migration status."""
    try:
        progress = combined_migration_progress.get_status()

        if progress.get("running"):
            from app.services.progress_tracker import ProgressTracker

            tracker_state = ProgressTracker.get_state("combined_speaker_migration", current_user.id)
            if tracker_state and tracker_state.eta_seconds is not None:
                progress["eta_seconds"] = tracker_state.eta_seconds

        return {"progress": progress}

    except Exception as e:
        logger.error("Error getting combined migration status: %s", e, exc_info=True)
        raise ErrorHandler.internal_error("Internal error") from e


@router.post("/start")
def start_combined_migration(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """Start combined speaker analysis for all completed files."""
    from app.tasks.combined_speaker_analysis_task import migrate_speakers_combined_task

    if combined_migration_progress.is_running():
        return {
            "status": "already_running",
            "message": "Combined speaker migration is already in progress",
            "progress": combined_migration_progress.get_status(),
        }

    try:
        task = migrate_speakers_combined_task.delay(user_id=current_user.id)

        logger.info("Started combined speaker migration task: %s", task.id)

        return {
            "status": "started",
            "task_id": task.id,
            "message": "Combined speaker migration dispatched.",
        }

    except Exception as e:
        logger.error("Error starting combined migration: %s", e, exc_info=True)
        raise ErrorHandler.internal_error("Internal error") from e


@router.post("/stop")
def stop_combined_migration(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """Stop a running combined speaker migration."""
    try:
        if not combined_migration_progress.is_running():
            return {
                "status": "not_running",
                "message": "No combined migration is currently running",
            }

        success = combined_migration_progress.force_stop()

        # Revoke pending batch tasks
        revoked_count = 0
        try:
            r = get_redis()
            batch_ids_raw = r.get("combined_speaker_migration:batch_task_ids")
            if batch_ids_raw:
                batch_ids = json.loads(batch_ids_raw)
                from app.core.celery import celery_app

                for tid in batch_ids:
                    celery_app.control.revoke(tid, terminate=True)
                    revoked_count += 1
                r.delete("combined_speaker_migration:batch_task_ids")
        except Exception as e:
            logger.warning("Failed to revoke batch tasks: %s", e)

        # Clear ProgressTracker so queue status clears immediately
        try:
            from app.services.progress_tracker import ProgressTracker

            tracker = ProgressTracker(
                task_type="combined_speaker_migration",
                user_id=current_user.id,
                total=0,
            )
            tracker.complete(message="Stopped by user")
        except Exception as e:
            logger.warning("Failed to clear progress tracker: %s", e)

        # Notify frontend via WebSocket
        try:
            from app.core.constants import NOTIFICATION_TYPE_COMBINED_MIGRATION_COMPLETE
            from app.utils.websocket_notify import send_ws_event

            send_ws_event(
                current_user.id,
                NOTIFICATION_TYPE_COMBINED_MIGRATION_COMPLETE,
                {"status": "stopped", "message": "Migration stopped by user"},
            )
        except Exception as e:
            logger.warning("Failed to send stop WS event: %s", e)

        if success:
            logger.warning(
                "Combined speaker migration stopped by user (revoked %d batch tasks)",
                revoked_count,
            )
            return {
                "status": "stopped",
                "message": "Combined speaker migration stopped.",
            }
        else:
            return {"status": "error", "message": "Failed to stop migration"}

    except Exception as e:
        logger.error("Error stopping combined migration: %s", e, exc_info=True)
        raise ErrorHandler.internal_error("Internal error") from e


@router.delete("/progress")
def clear_combined_progress(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
):
    """Clear stale combined migration progress data."""
    try:
        if combined_migration_progress.is_running():
            return {
                "status": "error",
                "message": "Cannot clear progress while migration is running.",
            }

        success = combined_migration_progress.clear_status()

        if success:
            return {"status": "cleared", "message": "Progress tracking cleared."}
        else:
            return {"status": "error", "message": "Failed to clear progress."}

    except Exception as e:
        logger.error("Error clearing combined progress: %s", e, exc_info=True)
        raise ErrorHandler.internal_error("Internal error") from e
