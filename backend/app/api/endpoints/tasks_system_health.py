"""Admin task-system health report (``GET /tasks/system/health``)."""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.user import User
from app.services.task_detection_service import task_detection_service
from app.utils.task_utils import TASK_STATUS_COMPLETED
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import TASK_STATUS_PENDING

logger = logging.getLogger(__name__)

router = APIRouter()


# Helper function to calculate age in seconds
def calculate_age_seconds(timestamp):
    """Calculate seconds between now and a timestamp, handling timezone differences safely"""
    if not timestamp:
        return None

    # Get current time with timezone
    now = datetime.now(UTC)

    # Make timestamp timezone-aware if it's not
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    # Calculate the difference
    return (now - timestamp).total_seconds()


@router.get("/system/health", response_model=dict[str, Any])
def task_system_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),  # Only admins can access this endpoint
):
    """
    Get health information about the task system

    Returns information about stuck tasks and inconsistent media files
    """
    try:
        # Identify stuck tasks
        stuck_tasks = task_detection_service.identify_stuck_tasks(db)

        # Identify inconsistent media files
        inconsistent_files = task_detection_service.identify_inconsistent_media_files(db)

        # Count tasks by status in a single query (replaces 5 separate queries)
        from sqlalchemy import case
        from sqlalchemy import func

        task_row = db.query(
            func.count(TaskModel.id).label("total"),
            func.count(case((TaskModel.status == TASK_STATUS_PENDING, 1))).label("pending"),
            func.count(case((TaskModel.status == TASK_STATUS_IN_PROGRESS, 1))).label("in_progress"),
            func.count(case((TaskModel.status == TASK_STATUS_COMPLETED, 1))).label("completed"),
            func.count(case((TaskModel.status == TASK_STATUS_FAILED, 1))).label("failed"),
        ).one()
        task_counts = {
            TASK_STATUS_PENDING: task_row.pending,
            TASK_STATUS_IN_PROGRESS: task_row.in_progress,
            TASK_STATUS_COMPLETED: task_row.completed,
            TASK_STATUS_FAILED: task_row.failed,
            "total": task_row.total,
        }

        # Count files by status in a single query (replaces 5 separate queries)
        file_row = db.query(
            func.count(MediaFile.id).label("total"),
            func.count(case((MediaFile.status == FileStatus.PENDING, 1))).label("pending"),
            func.count(case((MediaFile.status == FileStatus.PROCESSING, 1))).label("processing"),
            func.count(case((MediaFile.status == FileStatus.COMPLETED, 1))).label("completed"),
            func.count(case((MediaFile.status == FileStatus.ERROR, 1))).label("error"),
        ).one()
        file_counts = {
            "pending": file_row.pending,
            "processing": file_row.processing,
            "completed": file_row.completed,
            "error": file_row.error,
            "total": file_row.total,
        }

        # Format stuck tasks for response
        stuck_task_data = []
        for task in stuck_tasks:
            stuck_task_data.append(
                {
                    "id": task.id,
                    "task_type": task.task_type,
                    "status": task.status,
                    "media_file_id": str(task.media_file.uuid) if task.media_file else None,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                    "age_seconds": calculate_age_seconds(task.created_at),
                }
            )

        # Format inconsistent files for response
        inconsistent_file_data = []
        for file in inconsistent_files:
            inconsistent_file_data.append(
                {
                    "uuid": str(file.uuid),
                    "filename": file.filename,
                    # `MediaFile.status` is nullable with a Python-side default, so a row
                    # written by raw SQL or an explicit UPDATE holds NULL; guarded like the
                    # sibling read in endpoints/speakers.py rather than raising
                    # AttributeError and 500-ing the whole health report.
                    "status": file.status.value if file.status else None,
                    "user_id": str(file.user.uuid) if file.user else None,
                    "upload_time": file.upload_time,
                    "age_seconds": calculate_age_seconds(file.upload_time),
                }
            )

        return {
            "task_counts": task_counts,
            "file_counts": file_counts,
            "stuck_tasks": {"count": len(stuck_tasks), "items": stuck_task_data},
            "inconsistent_files": {
                "count": len(inconsistent_files),
                "items": inconsistent_file_data,
            },
            "timestamp": datetime.now(UTC),
        }
    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error in task_system_health: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e
