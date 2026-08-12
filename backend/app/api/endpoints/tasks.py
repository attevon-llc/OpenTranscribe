"""Task-system API: task listing, health, recovery, and retry.

Handlers are declared ``def``, not ``async def`` (issue #284 A2.5). Their bodies are
pure blocking work — SQLAlchemy aggregates over ``Task``/``MediaFile``, the detection
and recovery services, Celery ``.delay()`` — with no ``await`` anywhere, so running
them as coroutines pinned the event loop for the duration of each admin sweep. The
same applies to the nested ``BackgroundTasks`` callables: they call the synchronous
``dispatch_transcription_pipeline``, so they too are ``def`` and get threadpooled.
"""

import logging
import math
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.user import User
from app.schemas.media import PaginatedTaskResponse
from app.schemas.media import Task
from app.services.task_detection_service import task_detection_service
from app.services.task_filtering_service import TaskFilteringService
from app.services.task_recovery_service import task_recovery_service
from app.utils.uuid_helpers import get_file_by_uuid_with_permission
from app.utils.uuid_helpers import get_user_by_uuid

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


# Define task status constants to match those in the utils module
TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"


def _get_user_media_files(db: Session, current_user: User) -> list[MediaFile]:
    """Get media files based on user permissions.

    Projects only the columns needed for task list display, avoiding
    loading large JSONB columns (summary_data, metadata_raw, waveform_data).
    """
    columns = [
        MediaFile.id,
        MediaFile.uuid,
        MediaFile.filename,
        MediaFile.file_size,
        MediaFile.content_type,
        MediaFile.duration,
        MediaFile.language,
        MediaFile.status,
        MediaFile.upload_time,
        MediaFile.completed_at,
        MediaFile.media_format,
        MediaFile.codec,
        MediaFile.active_task_id,
        MediaFile.last_error_message,
    ]
    query = db.query(*columns)
    if not current_user.is_admin:
        query = query.filter(MediaFile.user_id == current_user.id)
    return query.all()  # type: ignore[no-any-return]


def _latest_task_by_file(
    db: Session, current_user: User, file_id: int | None = None
) -> dict[int, Any]:
    """Map ``media_file_id`` to the task row that represents the file's current work.

    A file accumulates one task row per pipeline stage (transcription,
    summarization, speaker_embedding, ...), so "the" task for a file is the one
    the pipeline is currently running — ``MediaFile.active_task_id`` — falling
    back to the most recently created row once that clears.

    Joins through ``MediaFile`` rather than passing an ``IN`` list of file ids so
    the query cost does not scale with the caller's library size.
    """
    query = (
        db.query(
            TaskModel.id,
            TaskModel.media_file_id,
            TaskModel.task_type,
            TaskModel.status,
            TaskModel.progress,
            TaskModel.error_message,
            TaskModel.created_at,
            TaskModel.updated_at,
            TaskModel.completed_at,
            MediaFile.active_task_id,
        )
        .join(MediaFile, TaskModel.media_file_id == MediaFile.id)
        .order_by(TaskModel.media_file_id, TaskModel.created_at)
    )
    if not current_user.is_admin:
        query = query.filter(MediaFile.user_id == current_user.id)
    if file_id is not None:
        query = query.filter(TaskModel.media_file_id == file_id)

    best: dict[int, Any] = {}
    for row in query.all():
        incumbent = best.get(row.media_file_id)
        if incumbent is None or row.id == row.active_task_id:
            best[row.media_file_id] = row
        elif incumbent.id != incumbent.active_task_id:
            # Rows arrive in created_at order, so a later row is the newer one.
            best[row.media_file_id] = row
    return best


def _map_file_status_to_task_status(file_status: FileStatus) -> str:
    """Map media file status to task status."""
    status_mapping = {
        FileStatus.PENDING: "pending",
        FileStatus.PROCESSING: "in_progress",
        FileStatus.COMPLETED: "completed",
        FileStatus.ERROR: "failed",
    }
    return status_mapping.get(file_status, "pending")


def _extract_file_format(content_type: str, filename: str) -> str | None:
    """Extract file format from content type or filename."""
    if content_type and "/" in content_type:
        return content_type.split("/")[1]
    elif filename and "." in filename:
        return filename.split(".")[-1]
    return None


def _create_task_dict_from_media_file(
    file: Any, current_user: User, task: Any | None = None
) -> dict:
    """Convert a media file row (ORM or named tuple) to a task dictionary.

    When the file's ``task`` row is supplied, every task-shaped field is read
    from it: the pipeline records real Celery ids, real fractional progress and
    real error text there via ``app.utils.task_utils``. Without one, the file's
    own columns are used and progress stays unknown rather than invented — a
    fabricated mid-point renders as a permanently half-full progress bar in the
    UI, which is what this endpoint used to emit for every processing file.
    """
    file_status = file.status
    completed_at = file.completed_at if hasattr(file, "completed_at") else None
    file_format = _extract_file_format(str(file.content_type), str(file.filename))

    if task is not None:
        task_id = task.id
        task_type = task.task_type
        task_status = task.status
        progress = float(task.progress) if task.progress is not None else 0.0
        error_message = task.error_message
        created_at = task.created_at or file.upload_time
        updated_at = task.updated_at or task.created_at or file.upload_time
        completed_at = task.completed_at or completed_at
    else:
        task_id = f"task_{file.id}"
        task_type = "transcription"
        task_status = _map_file_status_to_task_status(file_status)  # type: ignore[arg-type]
        progress = 1.0 if file_status == FileStatus.COMPLETED else 0.0
        error_message = (
            getattr(file, "last_error_message", None) if file_status == FileStatus.ERROR else None
        )
        created_at = file.upload_time
        updated_at = file.upload_time

    return {
        "id": task_id,
        "user_id": str(current_user.uuid),
        "task_type": task_type,
        "status": task_status,
        "media_file_id": str(file.uuid),
        "progress": progress,
        "created_at": created_at,
        "updated_at": updated_at,
        "completed_at": completed_at,
        "error_message": error_message,
        "media_file": {
            "uuid": str(file.uuid),
            "filename": file.filename,
            "file_size": file.file_size,
            "content_type": file.content_type,
            "duration": file.duration,
            "language": file.language,
            "format": file_format,
            "media_format": file.media_format if hasattr(file, "media_format") else None,
            "codec": file.codec if hasattr(file, "codec") else None,
            "upload_time": file.upload_time,
        },
    }


# =============================================================================
# PROGRESS RECOVERY - For frontend to recover in-flight task progress
# =============================================================================


@router.get("/progress/active", response_model=list[dict[str, Any]])
def get_active_progress(
    current_user: User = Depends(get_current_active_user),
):
    """Get all active (running) task progress for the current user.

    Returns ProgressState dicts from Redis so the frontend can recover
    progress bars after page refresh or modal close/reopen.
    """
    from app.services.progress_tracker import ProgressTracker

    return ProgressTracker.get_active_tasks(current_user.id)


# =============================================================================
# STATIC AND NESTED ROUTES - Must come before single-param routes
# =============================================================================


@router.get("", response_model=PaginatedTaskResponse)
def list_tasks(
    status: str | None = None,  # Filter by task status
    task_type: str | None = None,  # Filter by task type
    age_filter: str | None = None,  # Filter by age: "today", "week", "month", "older"
    date_from: str | None = None,  # Filter from date (YYYY-MM-DD)
    date_to: str | None = None,  # Filter to date (YYYY-MM-DD)
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    List tasks for the current user with server-side filtering and pagination.
    """
    try:
        # Get media files based on user permissions
        media_files = _get_user_media_files(db, current_user)

        # Convert media files to task dictionaries, pairing each with its real
        # task row so status/progress/task_type filters act on recorded values.
        tasks_by_file = _latest_task_by_file(db, current_user)
        tasks = [
            _create_task_dict_from_media_file(file, current_user, tasks_by_file.get(file.id))
            for file in media_files
        ]

        # Apply server-side filtering
        filtered_tasks = TaskFilteringService.filter_tasks_by_criteria(
            tasks=tasks,
            status=status,
            task_type=task_type,
            age_filter=age_filter,
            date_from=date_from,
            date_to=date_to,
        )

        # Pagination
        total = len(filtered_tasks)
        total_pages = max(1, math.ceil(total / page_size))
        offset = (page - 1) * page_size
        page_items = filtered_tasks[offset : offset + page_size]

        # Convert to Task schema objects
        items = [Task(**task_dict) for task_dict in page_items]

        return PaginatedTaskResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            has_more=page < total_pages,
        )

    except HTTPException:
        # A deliberate status raised below (or by a helper) must reach the client as
        # itself, not be relabelled 500 by the broad handler.
        raise
    except Exception as e:
        # Never answer 200 with an empty page here: a failed query is
        # indistinguishable from "this user has no tasks", so the SPA renders an
        # empty task list and the operator sees nothing wrong (issue #431).
        logger.exception(f"Error in list_tasks: {e}")
        # Literal 500: this handler's `status` query param shadows `fastapi.status`,
        # so `status.HTTP_500_INTERNAL_SERVER_ERROR` here raises AttributeError.
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


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
                    "status": file.status.value,
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
    except HTTPException:
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


@router.post("/recover-stuck-tasks", response_model=dict[str, Any])
def recover_all_stuck_tasks(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),  # Only admins can recover tasks
):
    """
    Attempt to recover all stuck tasks

    This endpoint will identify and recover all stuck tasks in the system.
    """
    try:
        # Identify stuck tasks
        stuck_tasks = task_detection_service.identify_stuck_tasks(db)
        if not stuck_tasks:
            return {"success": True, "count": 0, "message": "No stuck tasks found"}

        # Try to recover each task
        recovered_count = 0
        for task in stuck_tasks:
            success = task_recovery_service.recover_stuck_task(db, task)
            if success:
                recovered_count += 1

                # If it's a transcription task, retry it
                if task.task_type == "transcription" and task.media_file_id:
                    # Schedule a retry in the background for each recovered task
                    def retry_transcription(file_uuid):
                        try:
                            from app.tasks.transcription import dispatch_transcription_pipeline

                            task_id = dispatch_transcription_pipeline(file_uuid=file_uuid)
                            logger.info(
                                f"Retrying transcription for file {file_uuid}, "
                                f"new task ID: {task_id}"
                            )
                        except Exception as e:
                            logger.exception(f"Error retrying transcription: {e}")

                    # Get UUID from the relationship
                    file_uuid = str(task.media_file.uuid) if task.media_file else None
                    if file_uuid:
                        background_tasks.add_task(retry_transcription, file_uuid)

        return {
            "success": True,
            "count": recovered_count,
            "total": len(stuck_tasks),
            "message": f"Successfully recovered {recovered_count} of {len(stuck_tasks)} tasks",
        }
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error in recover_all_stuck_tasks: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/system/startup-recovery", response_model=dict[str, Any])
def trigger_startup_recovery(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Manually trigger startup recovery for files interrupted by system crashes.

    This endpoint allows admins to manually run the startup recovery process
    that would normally run automatically when the system starts.
    """
    try:
        # Schedule startup recovery in background
        def run_recovery():
            try:
                from app.tasks.recovery import startup_recovery_task

                result = startup_recovery_task.delay()
                logger.info(f"Manual startup recovery triggered: {result.id}")
            except Exception as e:
                logger.exception(f"Error in manual startup recovery: {e}")

        background_tasks.add_task(run_recovery)

        return {
            "success": True,
            "message": "Startup recovery task scheduled successfully",
        }
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error triggering startup recovery: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/system/recover-all-user-files", response_model=dict[str, Any])
def trigger_all_user_file_recovery(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Manually trigger file recovery for all users.

    This is useful for system-wide recovery after major issues.
    """
    try:
        # Schedule recovery for all users in background
        def run_all_user_recovery():
            try:
                from app.tasks.recovery import recover_user_files_task

                result = recover_user_files_task.delay()  # No user_id means all users
                logger.info(f"All user file recovery triggered: {result.id}")
            except Exception as e:
                logger.exception(f"Error in all user file recovery: {e}")

        background_tasks.add_task(run_all_user_recovery)

        return {"success": True, "message": "File recovery scheduled for all users"}
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error triggering all user file recovery: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/system/recover-user-files/{user_uuid}", response_model=dict[str, Any])
def trigger_user_file_recovery(
    user_uuid: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Manually trigger file recovery for a specific user.

    This is useful when a user reports stuck or missing files.
    """
    try:
        # Verify the user exists using UUID helper
        target_user = get_user_by_uuid(db, user_uuid)

        # Schedule user file recovery in background using internal integer ID
        user_id = target_user.id

        def run_user_recovery():
            try:
                from app.tasks.recovery import recover_user_files_task

                result = recover_user_files_task.delay(user_id)
                logger.info(f"User file recovery triggered for user {user_id}: {result.id}")
            except Exception as e:
                logger.exception(f"Error in user file recovery: {e}")

        background_tasks.add_task(run_user_recovery)

        return {
            "success": True,
            "message": f"File recovery scheduled for user {target_user.email}",
            "user_uuid": user_uuid,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error triggering user file recovery: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/system/recover-task/{task_id}", response_model=dict[str, Any])
def recover_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),  # Only admins can recover tasks
):
    """
    Attempt to recover a stuck task

    This endpoint will mark a stuck task as failed and retry it if appropriate.
    """
    try:
        # Find the task
        task = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if not task:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

        # Attempt recovery
        success = task_recovery_service.recover_stuck_task(db, task)

        # If successful and appropriate, retry the task
        if success and task.task_type == "transcription" and task.media_file_id:
            # Schedule a retry in the background
            # This avoids blocking the API call
            file_uuid = str(task.media_file.uuid) if task.media_file else None
            if file_uuid:

                def retry_transcription():
                    try:
                        from app.tasks.transcription import dispatch_transcription_pipeline

                        task_id = dispatch_transcription_pipeline(file_uuid=file_uuid)
                        logger.info(
                            f"Retrying transcription for file {file_uuid}, new task ID: {task_id}"
                        )
                    except Exception as e:
                        logger.exception(f"Error retrying transcription: {e}")

                background_tasks.add_task(retry_transcription)

        return {
            "success": success,
            "task_id": task_id,
            "message": "Task recovery successful" if success else "Task recovery failed",
            "retry_scheduled": success and task.task_type == "transcription",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in recover_task: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/system/fix-file/{file_uuid}", response_model=dict[str, Any])
def fix_inconsistent_file(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),  # Only admins can fix files
):
    """
    Attempt to fix a media file with inconsistent state
    """
    try:
        # Find the media file - admins can access any file
        from app.utils.uuid_helpers import get_file_by_uuid

        media_file = get_file_by_uuid(db, file_uuid)

        # Attempt to fix the file
        success = task_recovery_service.fix_inconsistent_media_file(db, media_file)

        return {
            "success": success,
            "file_id": str(media_file.uuid),  # Use UUID for frontend
            "message": "File fixed successfully" if success else "Failed to fix file",
            "new_status": media_file.status.value,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in fix_inconsistent_file: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/fix-inconsistent-files", response_model=dict[str, Any])
def fix_all_inconsistent_files(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Attempt to fix all media files with inconsistent state
    """
    try:
        inconsistent_files = task_detection_service.identify_inconsistent_media_files(db)
        if not inconsistent_files:
            return {
                "success": True,
                "count": 0,
                "total": 0,
                "message": "No inconsistent files found",
            }

        fixed_count = 0
        for file in inconsistent_files:
            success = task_recovery_service.fix_inconsistent_media_file(db, file)
            if success:
                fixed_count += 1

        return {
            "success": True,
            "count": fixed_count,
            "total": len(inconsistent_files),
            "message": f"Successfully fixed {fixed_count} of {len(inconsistent_files)} files",
        }
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error in fix_all_inconsistent_files: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/retry/{file_uuid}", response_model=dict[str, Any])
def retry_file_processing(
    file_uuid: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),  # Any user can retry their own files
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Retry processing for a file that failed or got stuck
    """
    try:
        # Find the media file (tenant-gated via ctx.org_id for non-admins)
        if current_user.is_admin:
            from app.utils.uuid_helpers import get_file_by_uuid

            media_file = get_file_by_uuid(db, file_uuid)
        else:
            media_file = get_file_by_uuid_with_permission(
                db,
                file_uuid,
                current_user.id,
                is_admin=current_user.is_admin,
                organization_id=ctx.org_id,
            )

        file_id = media_file.id

        # Check if the file is in a state where retry makes sense
        if media_file.status not in [FileStatus.ERROR, FileStatus.PROCESSING]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot retry file in {media_file.status.value} status",
            )

        # Reset the file status to PENDING
        from app.utils.task_utils import update_media_file_status

        update_media_file_status(db, int(file_id), FileStatus.PENDING)

        # Clear old tasks or mark them as failed
        old_tasks = (
            db.query(TaskModel)
            .filter(
                TaskModel.media_file_id == file_id,
                TaskModel.status.in_([TASK_STATUS_PENDING, TASK_STATUS_IN_PROGRESS]),
            )
            .all()
        )

        for task in old_tasks:
            task.status = TASK_STATUS_FAILED  # type: ignore[assignment]
            task.error_message = "Task marked as failed for retry"  # type: ignore[assignment]
            task.completed_at = datetime.now(UTC)  # type: ignore[assignment]

        db.commit()

        # Schedule a new transcription in the background
        def start_new_transcription():
            try:
                from app.tasks.transcription import dispatch_transcription_pipeline

                task_id = dispatch_transcription_pipeline(file_uuid=file_uuid)
                logger.info(f"Started new transcription for file {file_id}, task ID: {task_id}")
            except Exception as e:
                logger.exception(f"Error starting new transcription: {e}")

        background_tasks.add_task(start_new_transcription)

        return {
            "success": True,
            "file_id": str(media_file.uuid),  # Use UUID for frontend
            "message": "File processing restarted",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in retry_file_processing: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


# =============================================================================
# SINGLE-PARAM ROUTES - Must come last
# =============================================================================


def _get_task_row_by_id(db: Session, task_id: str, current_user: User) -> Any | None:
    """Look up a real task row by its Celery id, or ``None`` if there is no such row.

    Returns ``None`` for a row with no ``media_file_id`` so the caller falls
    through to the legacy id path rather than trying to build a file-shaped
    response with no file.
    """
    query = db.query(TaskModel).filter(TaskModel.id == task_id)
    if not current_user.is_admin:
        query = query.filter(TaskModel.user_id == current_user.id)
    task = query.first()
    return task if task is not None and task.media_file_id is not None else None


def _parse_task_id(task_id: str) -> int:
    """Parse task ID to extract media file ID."""
    if not task_id.startswith("task_"):
        raise ValueError("Invalid task ID format")
    try:
        return int(task_id.split("_")[1])
    except (ValueError, IndexError) as e:
        raise ValueError("Invalid task ID format") from e


def _get_media_file_by_id(db: Session, file_id: int, current_user: User) -> MediaFile:
    """Get media file by ID with proper permission checking."""
    if current_user.is_admin:
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
    else:
        media_file = (
            db.query(MediaFile)
            .filter(MediaFile.id == file_id, MediaFile.user_id == current_user.id)
            .first()
        )

    if not media_file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    return media_file  # type: ignore[no-any-return]


@router.get("/{task_id}", response_model=Task)
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get a specific task by its ID.

    Accepts either form of id this API issues: a real Celery task id (what the
    pipeline records, and what ``POST /tasks/system/recover-task/{task_id}``
    expects) or the legacy ``task_<media_file_id>`` form, which is still returned
    for files that have no task row yet.
    """
    try:
        task_row = _get_task_row_by_id(db, task_id, current_user)
        if task_row is not None:
            media_file = _get_media_file_by_id(db, task_row.media_file_id, current_user)
            return Task(**_create_task_dict_from_media_file(media_file, current_user, task_row))

        try:
            file_id = _parse_task_id(task_id)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task ID format"
            ) from e

        # Get media file with permission checking
        media_file = _get_media_file_by_id(db, file_id, current_user)

        # Prefer the file's real task row; the legacy id form only identifies the file.
        task_dict = _create_task_dict_from_media_file(
            media_file,
            current_user,
            _latest_task_by_file(db, current_user, media_file.id).get(media_file.id),
        )
        task_dict["id"] = task_id  # Honour the id form the caller asked with

        return Task(**task_dict)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in get_task: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e
