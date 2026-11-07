"""
Enhanced file management endpoints for error handling and recovery.
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.files import bulk_actions
from app.api.endpoints.files.crud import delete_media_file
from app.api.endpoints.files.crud import get_media_file_by_uuid
from app.core.env import env_bool
from app.core.exceptions import ASRConfigurationError
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.legal.processing_gate import require_processing_allowed_for_file
from app.models.media import FileStatus
from app.models.user import User
from app.services import system_settings_service
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.request_context import RequestContext
from app.tasks.transcription import dispatch_transcription_pipeline
from app.utils.file_retry import is_file_safe_to_delete
from app.utils.file_retry import reset_file_for_retry
from app.utils.stuck_file_recovery import check_for_stuck_files
from app.utils.stuck_file_recovery import recover_stuck_file
from app.utils.task_cancel import cancel_active_task

router = APIRouter()
logger = logging.getLogger(__name__)


class FileStatusDetail(BaseModel):
    """Detailed file status information."""

    file_uuid: str
    filename: str
    status: str
    can_delete: bool
    can_retry: bool
    can_cancel: bool
    is_stuck: bool
    retry_count: int
    max_retries: int
    active_task_id: str | None = None
    task_started_at: str | None = None
    task_last_update: str | None = None
    last_error_message: str | None = None
    recovery_attempts: int = 0
    force_delete_eligible: bool = False
    actions_available: list[str] = []
    recommendations: list[str] = []


@router.get("/{file_uuid}/status-detail", response_model=FileStatusDetail)
def get_file_status_detail(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get detailed status information for a file."""
    try:
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
        )
        file_id = db_file.id  # Get internal ID for task operations

        # The effective ceiling is the admin-tunable SystemSettings value, never
        # `MediaFile.max_retries` — nothing writes that column (see the retry-endpoint
        # comment below), so reading it here reported a ceiling this file's own retry
        # button did not enforce.
        retry_config = system_settings_service.get_retry_config(db)

        is_safe, delete_reason = is_file_safe_to_delete(db, file_id)

        # Determine if file is stuck
        stuck_files = check_for_stuck_files(db, stuck_threshold_hours=1)
        is_stuck = file_id in stuck_files

        # Determine available actions
        actions = []
        recommendations = []

        if db_file.status == FileStatus.PROCESSING and db_file.active_task_id:
            actions.append("cancel")
            if is_stuck:
                recommendations.append("This file appears stuck. Consider cancelling and retrying.")

        if db_file.status in [
            FileStatus.ERROR,
            FileStatus.CANCELLED,
            FileStatus.ORPHANED,
        ]:
            actions.append("retry")
            if system_settings_service.should_retry_file(db, int(db_file.retry_count or 0)):
                recommendations.append("This file can be retried for processing.")
            else:
                recommendations.append(
                    "This file has reached maximum retry attempts. Consider force deletion or admin intervention."
                )

        if is_safe or db_file.force_delete_eligible:
            actions.append("delete")
        elif is_admin:
            actions.append("force_delete")

        if is_stuck:
            actions.append("recover")
            recommendations.append("Auto-recovery may help resolve stuck processing.")

        return FileStatusDetail(
            file_uuid=str(db_file.uuid),
            filename=str(db_file.filename),
            status=str(db_file.status),
            can_delete=is_safe,
            can_retry=db_file.status
            in [FileStatus.ERROR, FileStatus.CANCELLED, FileStatus.ORPHANED],
            can_cancel=bool(
                db_file.status == FileStatus.PROCESSING and db_file.active_task_id is not None
            ),
            is_stuck=is_stuck,
            retry_count=int(db_file.retry_count or 0),
            max_retries=int(retry_config["max_retries"]),
            active_task_id=str(db_file.active_task_id) if db_file.active_task_id else None,
            task_started_at=db_file.task_started_at.isoformat()
            if db_file.task_started_at
            else None,
            task_last_update=db_file.task_last_update.isoformat()
            if db_file.task_last_update
            else None,
            # Never put the raw exception text on the wire (issue #786) — a fixed,
            # user-facing sentence from ErrorCategorizationService, not the raw message.
            last_error_message=ErrorCategorizationService.user_message_for(
                db_file.last_error_message
            ),
            recovery_attempts=int(db_file.recovery_attempts or 0),
            force_delete_eligible=bool(db_file.force_delete_eligible),
            actions_available=actions,
            recommendations=recommendations,
        )

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error getting file status detail for {file_uuid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving file status details",
        ) from e


@router.post("/{file_uuid}/cancel")
def cancel_file_processing(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Cancel active processing for a file."""
    try:
        db_file = get_media_file_by_uuid(
            db,
            file_uuid,
            current_user.id,
            bypass=ctx.bypass,
            organization_id=ctx.org_id,
            min_permission="editor",
        )
        file_id = db_file.id  # Get internal ID for task operations

        if db_file.status != FileStatus.PROCESSING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File is not currently processing",
            )

        if not db_file.active_task_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active task found for this file",
            )

        success = cancel_active_task(db, file_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to cancel file processing",
            )

        return {"message": "File processing cancelled successfully", "file_id": str(db_file.uuid)}

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error cancelling file {file_uuid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error cancelling file processing",
        ) from e


@router.post("/{file_uuid}/retry")
def retry_file_processing(
    file_uuid: str,
    reset_retry_count: bool = Query(False, description="Reset retry count to 0"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Retry processing for a failed file."""
    try:
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db,
            file_uuid,
            current_user.id,
            bypass=ctx.bypass,
            organization_id=ctx.org_id,
            min_permission="editor",
        )
        file_id = db_file.id  # Get internal ID for task operations
        require_processing_allowed_for_file(db, db_file)

        if db_file.status not in [
            FileStatus.ERROR,
            FileStatus.CANCELLED,
            FileStatus.ORPHANED,
        ]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File cannot be retried in current status: {db_file.status}",
            )

        from app.api.endpoints.files.reprocess import refuse_reprocess_if_held

        refuse_reprocess_if_held(db, int(file_id), actor=current_user, action="retry")

        # Resetting the retry count bypasses the admin-tunable ceiling entirely,
        # so only an admin may request it (A3: was accepted from any file owner).
        if reset_retry_count and not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an admin may reset the retry count.",
            )

        # Check retry limits against the same admin-tunable ceiling /reprocess honours
        # (A3: this used to read MediaFile.max_retries, a column nothing ever writes,
        # so it always compared against the ORM default of 3 regardless of the admin
        # setting). Message + ceiling determination are shared with
        # `user_files.py`'s `/my-files/{uuid}/retry` via `retry_ceiling_message` — see
        # its docstring for why these two routes must not drift again.
        if not reset_retry_count and not is_admin:
            ceiling_message = system_settings_service.retry_ceiling_message(
                db, int(db_file.retry_count or 0)
            )
            if ceiling_message:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ceiling_message,
                )

        # Reset file for retry
        success = reset_file_for_retry(db, file_id, reset_retry_count)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to reset file for retry",
            )

        # Start new transcription task

        if not env_bool("SKIP_CELERY"):
            # Re-run with what the file was submitted with: model, speaker range, and
            # diarization only if it was skipped (issue #1203).
            task_id = dispatch_transcription_pipeline(
                file_uuid=file_uuid, reuse_requested_options=True
            )
            logger.info(f"Started retry task {task_id} for file {file_id}")
            return {
                "message": "File retry initiated successfully",
                "file_id": str(db_file.uuid),  # Use UUID for frontend
                "task_id": task_id,
                "retry_attempt": db_file.retry_count,
            }
        else:
            logger.info("Skipping Celery task in test environment")
            return {
                "message": "File retry prepared (test mode)",
                "file_id": str(db_file.uuid),  # Use UUID for frontend
                "retry_attempt": db_file.retry_count,
            }

    except (HTTPException, DomainError):
        raise
    except ASRConfigurationError:
        # A deliberate refusal (e.g. a lite deployment with no cloud ASR
        # configured, issue #865) — let it reach the global OpenTranscribeError
        # handler in main.py, which surfaces the real message and a 503
        # instead of the generic 500 below.
        raise
    except Exception as e:
        logger.exception(f"Error retrying file {file_uuid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrying file processing",
        ) from e


@router.post("/{file_uuid}/recover")
def recover_file(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Attempt to recover a stuck file."""
    try:
        db_file = get_media_file_by_uuid(
            db,
            file_uuid,
            current_user.id,
            bypass=ctx.bypass,
            organization_id=ctx.org_id,
            min_permission="editor",
        )
        file_id = db_file.id  # Get internal ID for task operations
        require_processing_allowed_for_file(db, db_file)

        success = recover_stuck_file(db, file_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to recover file",
            )

        # Refresh file to get updated status
        db.refresh(db_file)

        return {
            "message": "File recovery completed",
            "file_id": str(db_file.uuid),  # Use UUID for frontend
            "new_status": db_file.status,
            "recovery_attempts": db_file.recovery_attempts,
        }

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error recovering file {file_uuid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error recovering file",
        ) from e


@router.delete("/{file_uuid}/force")
def force_delete_file(
    file_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Move a file to the trash even while a task is running on it (admin only).

    Since issue #1101 every delete is reversible, so this is the same move to the trash as
    ``DELETE /files/{uuid}`` (whose in-flight refusal is gone too); it no longer destroys
    anything. ``DELETE /files/{uuid}/purge`` is the explicit destroy, from the trash only.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can force delete files",
        )

    try:
        delete_media_file(
            db,
            file_uuid,
            current_user,
            organization_id=ctx.org_id,
            bypass=ctx.bypass,
            request=request,
        )
        return {"message": "File moved to the trash", "file_uuid": file_uuid}

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error force deleting file {file_uuid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error force deleting file",
        ) from e


@router.get("/management/stuck")
def get_stuck_files(
    threshold_hours: float = Query(2.0, description="Hours threshold for stuck detection"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get list of files that appear to be stuck in processing.

    Non-admins see only their own files in the ACTIVE tenant.
    """
    current_user = ctx.user
    try:
        stuck_file_ids = check_for_stuck_files(db, threshold_hours)

        stuck_files = []

        for file_id in stuck_file_ids:
            try:
                # Use internal ID lookup for stuck files (file_id is int from check_for_stuck_files)
                from app.api.endpoints.files.crud import get_media_file_by_id

                db_file = get_media_file_by_id(
                    db, file_id, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
                )
                stuck_files.append(
                    {
                        "uuid": str(db_file.uuid),
                        "filename": db_file.filename,
                        "status": db_file.status,
                        "active_task_id": db_file.active_task_id,
                        "task_started_at": db_file.task_started_at.isoformat()
                        if db_file.task_started_at
                        else None,
                        "task_last_update": db_file.task_last_update.isoformat()
                        if db_file.task_last_update
                        else None,
                        "recovery_attempts": db_file.recovery_attempts,
                    }
                )
            except (HTTPException, DomainError):
                # User doesn't have access to this file, skip it
                continue

        return {
            "stuck_files": stuck_files,
            "total_count": len(stuck_files),
            "threshold_hours": threshold_hours,
        }

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error getting stuck files: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving stuck files",
        ) from e


# Included here, not at the top: route-matching order is registration order and the bulk
# action has always sat between the stuck listing and the orphan sweep.
router.include_router(bulk_actions.router)


@router.post("/management/cleanup-orphaned", summary="Recover stuck files in bulk (admin only)")
def recover_stuck_files_bulk(
    dry_run: bool = Query(False, description="Preview changes without applying them"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Recover every file stuck mid-processing, deployment-wide (admin only).

    **The path is a misnomer and the behaviour is the contract.** This runs
    ``check_for_stuck_files`` (6 h threshold) and then ``recover_stuck_file`` over
    the result — it is the bulk sibling of ``POST /tasks/system/fix-file/{uuid}``
    and it has never touched an orphan. Real orphan cleanup (rows whose storage
    object is gone, and the reverse) is ``POST /admin/data-integrity``. The path
    stays as it is because operator runbooks and cron jobs already call it;
    renaming it would break them to fix a spelling.

    The response used to carry a ``marked_orphaned`` counter that was initialized
    and never incremented, so it reported ``0`` on every call in every deployment
    — an operator reading it concluded there was nothing to clean up. It is gone
    rather than wired up: nothing in this handler marks anything orphaned.

    Args:
        dry_run: Count the stuck files and change nothing.

    Returns:
        ``stuck_files_found``, how many were ``recovered``, per-file ``errors``,
        and the ``dry_run`` flag that was applied. A dry run always reports
        ``recovered: 0``.

    Raises:
        HTTPException: 403 for a non-admin caller, 500 if the sweep itself fails.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can recover stuck files",
        )

    try:
        # Find stuck files
        stuck_file_ids = check_for_stuck_files(db, stuck_threshold_hours=6)

        cleanup_results: dict[str, int | list[str] | bool] = {
            "stuck_files_found": len(stuck_file_ids),
            "recovered": 0,
            "errors": [],
            "dry_run": dry_run,
        }

        if not dry_run:
            for file_id in stuck_file_ids:
                try:
                    success = recover_stuck_file(db, file_id)
                    if success:
                        recovered_count = cleanup_results["recovered"]
                        assert isinstance(recovered_count, int)
                        cleanup_results["recovered"] = recovered_count + 1
                    else:
                        errors_list = cleanup_results["errors"]
                        if isinstance(errors_list, list):
                            errors_list.append(f"Failed to recover file {file_id}")
                except Exception as e:
                    # Per-file failure is recorded and the sweep continues; the
                    # caller reads cleanup_results["errors"].
                    logger.exception(f"Stuck-file recovery failed for file {file_id}")
                    errors_list = cleanup_results["errors"]
                    if isinstance(errors_list, list):
                        errors_list.append(f"Error processing file {file_id}: {str(e)}")

        return cleanup_results

    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception(f"Error in bulk stuck-file recovery: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error recovering stuck files",
        ) from e
