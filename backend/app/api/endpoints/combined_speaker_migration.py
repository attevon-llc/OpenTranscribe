"""
API endpoints for combined speaker analysis migration (embedding + gender).

Allows admins to trigger bulk processing of all files for both
embedding extraction and gender detection in a single pass.
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.migration_stop import BATCH_REVOCATION_FAILED
from app.api.endpoints.migration_stop import clear_stop_tracker
from app.api.endpoints.migration_stop import revoke_batch_tasks
from app.api.error_handlers import ErrorHandler
from app.core.exceptions import DomainError
from app.core.redis import get_request_redis
from app.db.base import get_db
from app.models.user import User
from app.tasks.combined_speaker_analysis_task import combined_migration_progress

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/status")
def get_combined_migration_status(
    current_user: User = Depends(get_current_active_superuser),
) -> dict[str, Any]:
    """Get the current combined speaker migration status."""
    try:
        progress = combined_migration_progress.get_status()

        if progress.get("running"):
            from app.services.progress_tracker import ProgressTracker

            tracker_state = ProgressTracker.get_state("combined_speaker_migration", current_user.id)
            if tracker_state and tracker_state.eta_seconds is not None:
                progress["eta_seconds"] = tracker_state.eta_seconds

        return {"progress": progress}

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error getting combined migration status: %s", e, exc_info=True)
        raise ErrorHandler.internal_error("Internal error") from e


@router.post("/start")
def start_combined_migration(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
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

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error starting combined migration: %s", e, exc_info=True)
        raise ErrorHandler.internal_error("Internal error") from e


@router.post("/stop")
def stop_combined_migration(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Stop a running combined speaker migration."""
    try:
        if not combined_migration_progress.is_running():
            return {
                "status": "not_running",
                "message": "No combined migration is currently running",
            }

        success = combined_migration_progress.force_stop()

        # Revoke pending batch tasks
        revoked_count, revoke_failed = revoke_batch_tasks(
            get_request_redis(), "combined_speaker_migration:batch_task_ids"
        )

        # Clear ProgressTracker so queue status clears immediately
        clear_stop_tracker("combined_speaker_migration", current_user.id)

        # Notify frontend via WebSocket
        from app.core.constants import NOTIFICATION_TYPE_COMBINED_MIGRATION_COMPLETE
        from app.utils.websocket_notify import send_ws_event

        # send_ws_event logs its own failures and never raises.
        send_ws_event(
            current_user.id,
            NOTIFICATION_TYPE_COMBINED_MIGRATION_COMPLETE,
            {"status": "stopped", "message": "Migration stopped by user"},
        )

        if success:
            logger.warning(
                "Combined speaker migration stopped by user (revoked %d batch tasks)",
                revoked_count,
            )
            stopped: dict[str, Any] = {
                "status": "stopped",
                "message": "Combined speaker migration stopped.",
            }
            if revoke_failed:
                stopped["warnings"] = [BATCH_REVOCATION_FAILED]
            return stopped
        else:
            return {"status": "error", "message": "Failed to stop migration"}

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error stopping combined migration: %s", e, exc_info=True)
        raise ErrorHandler.internal_error("Internal error") from e


@router.delete("/progress")
def clear_combined_progress(
    current_user: User = Depends(get_current_active_superuser),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
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

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error clearing combined progress: %s", e, exc_info=True)
        raise ErrorHandler.internal_error("Internal error") from e
