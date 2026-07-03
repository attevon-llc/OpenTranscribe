"""
User-level file management endpoints.

This module provides endpoints for regular users to manage their files,
check status, and request retries without requiring admin privileges.
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import defer

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.user import User
from app.services.formatting_service import FormattingService
from app.services.task_recovery_service import task_recovery_service
from app.utils.task_utils import get_task_summary_for_media_file
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/status", response_model=dict[str, Any])
def get_user_file_status(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    """
    Get status summary of current user's files including any problems.

    Returns counts of files by status and identifies files that may need attention.
    """
    try:
        now = datetime.now(timezone.utc)
        user_id = current_user.id

        # --- Status counts via SQL GROUP BY (single aggregation query) ---
        status_rows = (
            db.query(MediaFile.status, func.count(MediaFile.id))
            .filter(MediaFile.user_id == user_id)
            .group_by(MediaFile.status)
            .all()
        )

        status_counts: dict[str, int] = {
            "total": 0,
            "pending": 0,
            "processing": 0,
            "completed": 0,
            "error": 0,
        }
        for file_status, count in status_rows:
            key = file_status.value if hasattr(file_status, "value") else str(file_status)
            if key in status_counts:
                status_counts[key] = count
            status_counts["total"] += count

        # --- Problem files via SQL (filtered query, no full table scan) ---
        one_hour_ago = now - timedelta(hours=1)
        two_hours_ago = now - timedelta(hours=2)

        # Defer heavy JSON columns not needed for status display
        problem_query = (
            db.query(MediaFile)
            .options(
                defer(MediaFile.metadata_raw),  # type: ignore[arg-type]
                defer(MediaFile.waveform_data),  # type: ignore[arg-type]
                defer(MediaFile.metadata_important),  # type: ignore[arg-type]
                defer(MediaFile.summary_data),  # type: ignore[arg-type]
            )
            .filter(
                MediaFile.user_id == user_id,
                (
                    (MediaFile.status == FileStatus.PROCESSING)
                    & (MediaFile.upload_time < one_hour_ago)
                )
                | (
                    (MediaFile.status == FileStatus.PENDING)
                    & (MediaFile.upload_time < two_hours_ago)
                )
                | (MediaFile.status == FileStatus.ERROR),
            )
            .order_by(MediaFile.upload_time.desc())
        )

        problem_files = []
        for file in problem_query.all():
            assert file.upload_time is not None  # filtered on upload_time above
            file_age = now - file.upload_time
            problem_files.append(
                {
                    "uuid": str(file.uuid),
                    "filename": file.filename,
                    "status": file.status.value,
                    "upload_time": file.upload_time,
                    "age_hours": file_age.total_seconds() / 3600,
                    "can_retry": file.status in [FileStatus.ERROR, FileStatus.PROCESSING],
                    "formatted_duration": FormattingService.format_duration(
                        float(file.duration) if file.duration is not None else None
                    ),
                    "formatted_file_age": FormattingService.format_file_age(file.upload_time),
                    "formatted_file_size": FormattingService.format_bytes_detailed(
                        int(file.file_size) if file.file_size is not None else None
                    ),
                    "display_status": FormattingService.format_status(FileStatus(file.status)),
                    "status_badge_class": FormattingService.get_status_badge_class(
                        file.status.value
                    ),
                }
            )

        # --- Recent files via SQL (last 24 hours, limited, pre-sorted) ---
        twenty_four_hours_ago = now - timedelta(hours=24)

        # Defer heavy JSON columns not needed for recent file list
        recent_query = (
            db.query(MediaFile)
            .options(
                defer(MediaFile.metadata_raw),  # type: ignore[arg-type]
                defer(MediaFile.waveform_data),  # type: ignore[arg-type]
                defer(MediaFile.metadata_important),  # type: ignore[arg-type]
                defer(MediaFile.summary_data),  # type: ignore[arg-type]
            )
            .filter(
                MediaFile.user_id == user_id,
                MediaFile.upload_time >= twenty_four_hours_ago,
            )
            .order_by(MediaFile.upload_time.desc())
            .limit(10)
        )

        recent_files = []
        for file in recent_query.all():
            assert file.upload_time is not None  # filtered on upload_time above
            file_age = now - file.upload_time
            recent_files.append(
                {
                    "uuid": str(file.uuid),
                    "filename": file.filename,
                    "status": file.status.value,
                    "upload_time": file.upload_time,
                    "duration": file.duration,
                    "age_hours": file_age.total_seconds() / 3600,
                    "formatted_duration": FormattingService.format_duration(
                        float(file.duration) if file.duration is not None else None
                    ),
                    "formatted_file_age": FormattingService.format_file_age(file.upload_time),
                    "formatted_file_size": FormattingService.format_bytes_detailed(
                        int(file.file_size) if file.file_size is not None else None
                    ),
                    "display_status": FormattingService.format_status(FileStatus(file.status)),
                    "status_badge_class": FormattingService.get_status_badge_class(
                        file.status.value
                    ),
                }
            )

        return {
            "status_counts": status_counts,
            "problem_files": {"count": len(problem_files), "files": problem_files},
            "recent_files": {
                "count": len(recent_files),
                "files": recent_files,
            },
            "has_problems": len(problem_files) > 0,
            "timestamp": now,
        }

    except Exception as e:
        logger.error("Error getting user file status: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.get("/{file_uuid}/status", response_model=dict[str, Any])
def get_file_detailed_status(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Get detailed status for a specific file including task information.
    """
    try:
        # Get the file (ensure user owns it, tenant-gated via ctx.org_id)
        media_file = get_file_by_uuid_with_permission(
            db,
            file_uuid,
            current_user.id,
            is_admin=current_user.is_admin,
            organization_id=ctx.org_id,
        )
        file_id = media_file.id

        # Get task summary
        task_summary = get_task_summary_for_media_file(db, file_id)

        # Get all tasks for this file
        tasks = db.query(TaskModel).filter(TaskModel.media_file_id == file_id).all()

        task_details = []
        for task in tasks:
            task_detail: dict[str, Any] = {
                "id": task.id,
                "task_type": task.task_type,
                "status": task.status,
                "progress": task.progress,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
                "completed_at": task.completed_at,
                "error_message": task.error_message,
                # Add formatted processing time
                "formatted_processing_time": FormattingService.format_processing_time(
                    task.created_at,
                    task.completed_at,
                ),
                "status_display": task.status.title(),
            }
            # Include model tracking info from the media file for transcription tasks
            if task.task_type == "transcription":
                task_detail["whisper_model"] = media_file.whisper_model
                task_detail["diarization_model"] = media_file.diarization_model
            task_details.append(task_detail)

        # Calculate file age and determine if retry is available
        assert media_file.upload_time is not None  # server_default=now() on persisted row
        file_age = datetime.now(timezone.utc) - media_file.upload_time
        can_retry = media_file.status in [FileStatus.ERROR, FileStatus.PROCESSING]

        # Check if file might be stuck
        is_stuck = False
        if media_file.status == FileStatus.PROCESSING:
            active_tasks = [t for t in tasks if t.status in ["pending", "in_progress"]]
            if not active_tasks and file_age > timedelta(hours=1):
                is_stuck = True

        return {
            "file": {
                "uuid": str(media_file.uuid),
                "filename": media_file.filename,
                "status": media_file.status.value,
                "upload_time": media_file.upload_time,
                "completed_at": media_file.completed_at,
                "file_size": media_file.file_size,
                "duration": media_file.duration,
                "language": media_file.language,
                # Add formatted fields for frontend display
                "formatted_file_size": FormattingService.format_bytes_detailed(
                    int(media_file.file_size) if media_file.file_size is not None else None
                ),
                "formatted_duration": FormattingService.format_duration(
                    float(media_file.duration) if media_file.duration is not None else None
                ),
                "formatted_file_age": FormattingService.format_file_age(media_file.upload_time),
                "display_status": FormattingService.format_status(FileStatus(media_file.status)),
                "status_badge_class": FormattingService.get_status_badge_class(
                    media_file.status.value
                ),
                "whisper_model": media_file.whisper_model,
                "requested_whisper_model": media_file.requested_whisper_model,
                "model_fallback_occurred": (
                    media_file.requested_whisper_model is not None
                    and media_file.whisper_model is not None
                    and media_file.requested_whisper_model != media_file.whisper_model
                ),
                "diarization_model": media_file.diarization_model,
                "embedding_mode": media_file.embedding_mode,
            },
            "task_summary": task_summary,
            "task_details": task_details,
            "file_age_hours": file_age.total_seconds() / 3600,
            "can_retry": can_retry,
            "is_stuck": is_stuck,
            "suggestions": _get_file_suggestions(
                media_file, task_summary, timedelta(seconds=file_age.total_seconds())
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting file detailed status: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/{file_uuid}/retry", response_model=dict[str, Any])
async def retry_file_processing(
    file_uuid: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Retry processing for a file that failed or is stuck.
    Available to all users for their own files.
    """
    try:
        # Get the file (ensure user owns it, tenant-gated via ctx.org_id)
        media_file = get_file_by_uuid_with_permission(
            db,
            file_uuid,
            current_user.id,
            is_admin=current_user.is_admin,
            organization_id=ctx.org_id,
        )
        file_id = media_file.id

        # Check if retry is appropriate
        if media_file.status not in [FileStatus.ERROR, FileStatus.PROCESSING]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot retry file in {media_file.status.value} status. Only error or stuck processing files can be retried.",
            )

        # Check rate limiting (prevent spam retries)
        recent_tasks = (
            db.query(TaskModel)
            .filter(
                TaskModel.media_file_id == file_id,
                TaskModel.created_at > datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            .count()
        )

        if recent_tasks > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait at least 5 minutes between retry attempts",
            )

        # Use recovery service to handle the retry
        success = task_recovery_service.schedule_file_retry(file_id)

        if success:
            # Update file status to pending
            from app.utils.task_utils import update_media_file_status

            update_media_file_status(db, file_id, FileStatus.PENDING)

            # Mark old tasks as failed
            old_tasks = (
                db.query(TaskModel)
                .filter(
                    TaskModel.media_file_id == file_id,
                    TaskModel.status.in_(["pending", "in_progress"]),
                )
                .all()
            )

            for task in old_tasks:
                task.status = "failed"  # type: ignore[assignment]
                task.error_message = "Task marked as failed for user retry"  # type: ignore[assignment]
                task.completed_at = datetime.now(timezone.utc)  # type: ignore[assignment]

            db.commit()

            logger.info(f"User {current_user.id} successfully retried file {file_id}")

            return {
                "success": True,
                "message": "File processing restarted successfully",
                "file_id": str(media_file.uuid),  # Use UUID for frontend
                "new_status": "pending",
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to schedule file retry",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error retrying file processing: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/request-recovery", response_model=dict[str, Any])
async def request_user_recovery(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Request recovery for all of the current user's problem files.
    This triggers the same recovery process that admins can run.
    """
    try:
        # Check rate limiting (prevent spam requests)
        # This could be implemented with Redis or database tracking

        # Schedule user-specific recovery
        async def run_user_recovery():
            try:
                from app.tasks.recovery import recover_user_files_task

                result = recover_user_files_task.delay(current_user.id)
                logger.info(f"User {current_user.id} requested file recovery, task ID: {result.id}")
            except Exception as e:
                logger.error(f"Error in user recovery request: {e}")

        background_tasks.add_task(run_user_recovery)

        return {
            "success": True,
            "message": "Recovery process started for your files",
            "user_id": str(current_user.uuid),
        }

    except Exception as e:
        logger.error("Error requesting user recovery: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


def _get_file_suggestions(
    media_file: MediaFile, task_summary: dict[str, Any], file_age: timedelta
) -> list[str]:
    """Generate helpful suggestions for the user based on file status."""
    suggestions = []

    if media_file.status == FileStatus.ERROR:
        suggestions.append(
            "This file failed to process. You can retry processing by clicking the retry button."
        )
        if task_summary.get("failed", 0) > 0:
            suggestions.append(
                "Check if the file format is supported or if the audio quality is sufficient."
            )

    elif media_file.status == FileStatus.PROCESSING:
        if file_age > timedelta(hours=2):
            suggestions.append(
                "This file has been processing for a long time. It may be stuck and could benefit from a retry."
            )
        elif file_age > timedelta(minutes=30):
            suggestions.append(
                "Large files or files with poor audio quality may take longer to process."
            )
        else:
            suggestions.append("File is currently being processed. Please wait for completion.")

    elif media_file.status == FileStatus.PENDING:
        if file_age > timedelta(hours=1):
            suggestions.append(
                "This file has been waiting to be processed for a while. You can retry to move it to the front of the queue."
            )
        else:
            suggestions.append("File is waiting to be processed. Processing will begin shortly.")

    elif media_file.status == FileStatus.COMPLETED:
        suggestions.append("File has been successfully processed and is ready for use.")

    return suggestions
