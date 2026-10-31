"""Inconsistent-file repair and file-processing retry under ``/tasks``.

``retry_file_processing`` is the one non-admin route (the caller must be able to edit the file,
checked by ``get_file_by_uuid_with_permission``; non-admins also hit the retry ceiling).
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.core.env import env_bool
from app.core.exceptions import ASRConfigurationError
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import Task as TaskModel
from app.models.user import User
from app.services import system_settings_service
from app.services.request_context import RequestContext
from app.services.task_detection_service import task_detection_service
from app.services.task_recovery_service import task_recovery_service
from app.services.uuid_helpers import get_file_by_uuid_with_permission
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import TASK_STATUS_PENDING

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/system/fix-file/{file_uuid}", response_model=dict[str, Any])
def fix_inconsistent_file(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),  # Only admins can fix files
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Attempt to fix a media file with inconsistent state
    """
    try:
        media_file = get_file_by_uuid_with_permission(
            db,
            file_uuid,
            current_user.id,
            bypass=ctx.bypass,
            organization_id=ctx.org_id,
            min_permission="editor",
        )

        # Attempt to fix the file
        success = task_recovery_service.fix_inconsistent_media_file(db, media_file)

        return {
            "success": success,
            "file_id": str(media_file.uuid),  # Use UUID for frontend
            "message": "File fixed successfully" if success else "Failed to fix file",
            # Nullable status: a fix that could not determine a status leaves it as it was.
            "new_status": media_file.status.value if media_file.status else None,
        }
    except (HTTPException, DomainError):
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
    except (HTTPException, DomainError):
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),  # Any user can retry their own files
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Retry processing for a file that failed or got stuck

    Dispatches inline rather than via ``background_tasks.add_task`` — a response built
    before the background task runs can never reflect its outcome (issue #906). Mirrors
    ``files/management.py``'s ``/{file_uuid}/retry``, including its ``SKIP_CELERY``
    test-mode branch.
    """
    try:
        media_file = get_file_by_uuid_with_permission(
            db,
            file_uuid,
            current_user.id,
            bypass=ctx.bypass,
            organization_id=ctx.org_id,
            min_permission="editor",
        )

        file_id = media_file.id

        # Check if the file is in a state where retry makes sense. A NULL status fails this
        # membership test and lands here, where `.status.value` would raise AttributeError
        # and downgrade a deliberate 400 into an opaque 500.
        if media_file.status not in [FileStatus.ERROR, FileStatus.PROCESSING]:
            current_status = media_file.status.value if media_file.status else "unknown"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot retry file in {current_status} status",
            )

        # Enforce the same admin-tunable retry ceiling every other retry entry point
        # does (files/management.py's `/{file_uuid}/retry`, user_files.py's
        # `/my-files/{uuid}/retry`). This route used to dispatch unconditionally with
        # no retry_count tracking at all -- an admin lowering the ceiling to stop a
        # runaway retry loop had no effect on a user retrying through this endpoint.
        if not current_user.is_admin:
            ceiling_message = system_settings_service.retry_ceiling_message(
                db, int(media_file.retry_count or 0)
            )
            if ceiling_message:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ceiling_message,
                )

        # Reset the file status to PENDING
        from app.utils.task_utils import update_media_file_status

        update_media_file_status(db, int(file_id), FileStatus.PENDING)
        media_file.retry_count = int(media_file.retry_count or 0) + 1

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

        # Start a new transcription synchronously — see the docstring for why.

        if not env_bool("SKIP_CELERY"):
            from app.tasks.transcription import dispatch_transcription_pipeline

            new_task_id = dispatch_transcription_pipeline(
                file_uuid=file_uuid, reuse_requested_options=True
            )
            logger.info(f"Started new transcription for file {file_id}, task ID: {new_task_id}")
            return {
                "success": True,
                "file_id": str(media_file.uuid),
                "task_id": new_task_id,
                "message": "File processing restarted",
            }
        logger.info("Skipping Celery task in test environment")
        return {
            "success": True,
            "file_id": str(media_file.uuid),
            "message": "File processing restarted (test mode)",
        }
    except (HTTPException, DomainError):
        raise
    except ASRConfigurationError:
        # A deliberate refusal (e.g. lite deployment, no cloud ASR — #865). Let it
        # reach main.py's OpenTranscribeError handler: real message + 503, not a
        # generic 500.
        raise
    except Exception as e:
        logger.error("Error in retry_file_processing: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e
