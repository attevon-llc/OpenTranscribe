"""Admin stuck-task and file recovery triggers under ``/tasks``.

Every route here is admin-gated (``get_current_admin_user``). Handlers are ``def`` for the
same reason as in ``tasks.py``: pure blocking work, threadpooled by Starlette.
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.core.exceptions import ASRConfigurationError
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.media import Task as TaskModel
from app.models.user import User
from app.services.task_detection_service import task_detection_service
from app.services.task_recovery_service import task_recovery_service
from app.services.uuid_helpers import get_user_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/recover-stuck-tasks", response_model=dict[str, Any])
def recover_all_stuck_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),  # Only admins can recover tasks
):
    """
    Attempt to recover all stuck tasks

    This endpoint will identify and recover all stuck tasks in the system.

    Dispatches inline rather than via ``background_tasks.add_task`` — FastAPI sends the
    response BEFORE a background task runs, so a response built from ``background_tasks``
    can never reflect the actual re-dispatch outcome (issue #906). This handler stays a
    plain ``def`` (see ``tests/api/test_handler_blocking_io.py::HARDENED_HANDLERS``), so it
    already runs in Starlette's threadpool and dispatching inline is safe.
    """
    try:
        # Identify stuck tasks
        stuck_tasks = task_detection_service.identify_stuck_tasks(db)
        if not stuck_tasks:
            return {"success": True, "count": 0, "message": "No stuck tasks found"}

        recovered_count = 0
        retried_count = 0
        retry_failures: list[dict[str, str]] = []
        for task in stuck_tasks:
            # requeue=False: this endpoint dispatches the replacement itself, inline, so it
            # can report a failed dispatch (issue #906).
            if not task_recovery_service.recover_stuck_task(db, task, requeue=False):
                continue
            recovered_count += 1
            if task.task_type != "transcription" or not task.media_file_id:
                continue
            file_uuid = str(task.media_file.uuid) if task.media_file else None
            if not file_uuid:
                continue
            try:
                from app.tasks.transcription import dispatch_transcription_pipeline

                new_task_id = dispatch_transcription_pipeline(
                    file_uuid=file_uuid, reuse_requested_options=True
                )
                retried_count += 1
                logger.info(
                    f"Retrying transcription for file {file_uuid}, new task ID: {new_task_id}"
                )
            except Exception as e:
                # PER-FILE catch deliberately: one bad config must not abort the sweep
                # for everyone else. What changes is the outcome now reaches the
                # caller, not just the log. File is marked ERROR by dispatch itself.
                #
                # retry_failures is returned verbatim in this endpoint's response body
                # (see below) — never interpolate the raw exception text here, only its
                # class name (#914). Same sanitization as recover_task's dispatch_error
                # a few hundred lines down; the original #914 scanner missed this site
                # because the taint flows into a list .append() rather than an
                # assignment or a return.
                logger.exception(f"Failed to re-dispatch transcription for file {file_uuid}")
                retry_failures.append(
                    {"file_uuid": file_uuid, "error": f"Re-dispatch failed ({type(e).__name__})"}
                )

        return {
            "success": not retry_failures,
            "count": recovered_count,
            "total": len(stuck_tasks),
            "retried": retried_count,
            "retry_failures": retry_failures,
            "message": (
                f"Successfully recovered {recovered_count} of {len(stuck_tasks)} tasks"
                if not retry_failures
                else f"Recovered {recovered_count} of {len(stuck_tasks)} tasks, but "
                f"{len(retry_failures)} could not be re-queued"
            ),
        }
    except (HTTPException, DomainError):
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
    except (HTTPException, DomainError):
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
    except (HTTPException, DomainError):
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
    except (HTTPException, DomainError):
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),  # Only admins can recover tasks
):
    """
    Attempt to recover a stuck task

    This endpoint will mark a stuck task as failed and retry it if appropriate.

    Dispatches inline rather than via ``background_tasks.add_task`` — a response built
    before the background task runs can never reflect its outcome (issue #906).
    """
    try:
        # Find the task
        task = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if not task:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

        # Attempt recovery
        # requeue=False: the replacement is dispatched inline below, so the response can
        # report a failed dispatch (issue #906).
        success = task_recovery_service.recover_stuck_task(db, task, requeue=False)

        retry_scheduled = False
        dispatch_error: str | None = None
        if success and task.task_type == "transcription" and task.media_file_id:
            file_uuid = str(task.media_file.uuid) if task.media_file else None
            if file_uuid:
                try:
                    from app.tasks.transcription import dispatch_transcription_pipeline

                    new_task_id = dispatch_transcription_pipeline(
                        file_uuid=file_uuid, reuse_requested_options=True
                    )
                    retry_scheduled = True
                    logger.info(
                        f"Retrying transcription for file {file_uuid}, new task ID: {new_task_id}"
                    )
                except ASRConfigurationError:
                    raise  # let it reach main.py's 503 handler
                except Exception as e:
                    # dispatch_error is returned verbatim in this endpoint's response
                    # body (see below) — never interpolate the raw exception text
                    # here, only its class name (#914).
                    logger.exception(f"Failed to re-dispatch transcription for file {file_uuid}")
                    dispatch_error = f"Re-dispatch failed ({type(e).__name__})"

        return {
            "success": success and dispatch_error is None,
            "task_id": task_id,
            "message": "Task recovery successful" if success else "Task recovery failed",
            "retry_scheduled": retry_scheduled,
            "dispatch_error": dispatch_error,
        }
    except (HTTPException, DomainError):
        raise
    except ASRConfigurationError:
        # A deliberate refusal (e.g. lite deployment, no cloud ASR — #865). Let it
        # reach main.py's OpenTranscribeError handler: real message + 503, not a
        # generic 500.
        raise
    except Exception as e:
        logger.error("Error in recover_task: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e
