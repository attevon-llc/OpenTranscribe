"""
Enhanced file management endpoints for error handling and recovery.
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_user
from app.api.endpoints.files.crud import delete_media_file
from app.api.endpoints.files.crud import get_media_file_by_uuid
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import Tag
from app.models.user import User
from app.services.tag_bulk import CHANGED_OUTCOMES
from app.services.tag_bulk import TAG_ACTIONS
from app.services.tag_bulk import BulkTagOutcome
from app.services.tag_bulk import apply_tag_to_file
from app.services.tag_bulk import refresh_tagged_files
from app.services.tag_bulk import resolve_bulk_tag
from app.services.tag_service import InvalidTagNameError
from app.tasks.summarization import summarize_transcript_task
from app.tasks.transcription import dispatch_transcription_pipeline
from app.utils.task_utils import cancel_active_task
from app.utils.task_utils import check_for_stuck_files
from app.utils.task_utils import is_file_safe_to_delete
from app.utils.task_utils import recover_stuck_file
from app.utils.task_utils import reset_file_for_retry

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


class BulkActionRequest(BaseModel):
    """Request for bulk file operations."""

    file_uuids: list[str]
    # delete | retry | cancel | recover | reprocess | summarize | redact
    # | identify_speakers | add_tag | remove_tag
    action: str
    force: bool = False
    reset_retry_count: bool = False
    # Tag actions (required when action='add_tag' or 'remove_tag')
    tag_name: str | None = None
    # Selective reprocessing (optional, used when action='reprocess')
    stages: list[str] = Field(default_factory=list)
    min_speakers: int | None = None
    max_speakers: int | None = None
    num_speakers: int | None = None
    disable_diarization: bool | None = None


class BulkActionResult(BaseModel):
    """Result of bulk file operations."""

    file_uuid: str
    success: bool
    message: str
    error: str | None = None
    outcome: BulkTagOutcome | None = None


@router.get("/{file_uuid}/status-detail", response_model=FileStatusDetail)
def get_file_status_detail(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get detailed status information for a file."""
    try:
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, is_admin=is_admin, organization_id=ctx.org_id
        )
        file_id = db_file.id  # Get internal ID for task operations

        # Check if file is safe to delete
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
            if (db_file.retry_count or 0) < (db_file.max_retries or 3):
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
            max_retries=int(db_file.max_retries or 3),
            active_task_id=str(db_file.active_task_id) if db_file.active_task_id else None,
            task_started_at=db_file.task_started_at.isoformat()
            if db_file.task_started_at
            else None,
            task_last_update=db_file.task_last_update.isoformat()
            if db_file.task_last_update
            else None,
            last_error_message=str(db_file.last_error_message)
            if db_file.last_error_message
            else None,
            recovery_attempts=int(db_file.recovery_attempts or 0),
            force_delete_eligible=bool(db_file.force_delete_eligible),
            actions_available=actions,
            recommendations=recommendations,
        )

    except HTTPException:
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
    current_user: User = Depends(get_current_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Cancel active processing for a file."""
    try:
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, is_admin=is_admin, organization_id=ctx.org_id
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

    except HTTPException:
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
    current_user: User = Depends(get_current_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Retry processing for a failed file."""
    try:
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, is_admin=is_admin, organization_id=ctx.org_id
        )
        file_id = db_file.id  # Get internal ID for task operations

        # Check if file can be retried
        if db_file.status not in [
            FileStatus.ERROR,
            FileStatus.CANCELLED,
            FileStatus.ORPHANED,
        ]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File cannot be retried in current status: {db_file.status}",
            )

        # Check retry limits
        if (
            (db_file.retry_count or 0) >= (db_file.max_retries or 3)
            and not reset_retry_count
            and not is_admin
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File has reached maximum retry attempts ({db_file.max_retries}). Contact admin for help.",
            )

        # Reset file for retry
        success = reset_file_for_retry(db, file_id, reset_retry_count)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to reset file for retry",
            )

        # Start new transcription task
        import os

        if os.environ.get("SKIP_CELERY", "False").lower() != "true":
            # Preserve diarization setting from original processing run
            disable_diarization = bool(getattr(db_file, "diarization_disabled", False))
            task_id = dispatch_transcription_pipeline(
                file_uuid=file_uuid,
                disable_diarization=disable_diarization,
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

    except HTTPException:
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
    current_user: User = Depends(get_current_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Attempt to recover a stuck file."""
    try:
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, is_admin=is_admin, organization_id=ctx.org_id
        )
        file_id = db_file.id  # Get internal ID for task operations

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

    except HTTPException:
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Force delete a file (admin only)."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can force delete files",
        )

    try:
        delete_media_file(db, file_uuid, current_user, force=True, organization_id=ctx.org_id)
        return {"message": "File force deleted successfully", "file_uuid": file_uuid}

    except HTTPException:
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
    current_user: User = Depends(get_current_user),
):
    """Get list of files that appear to be stuck in processing."""
    try:
        stuck_file_ids = check_for_stuck_files(db, threshold_hours)

        # Get file details for user's files only (unless admin)
        is_admin = current_user.is_admin
        stuck_files = []

        for file_id in stuck_file_ids:
            try:
                # Use internal ID lookup for stuck files (file_id is int from check_for_stuck_files)
                from app.api.endpoints.files.crud import get_media_file_by_id

                db_file = get_media_file_by_id(db, file_id, current_user.id, is_admin=is_admin)
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
            except HTTPException:
                # User doesn't have access to this file, skip it
                continue

        return {
            "stuck_files": stuck_files,
            "total_count": len(stuck_files),
            "threshold_hours": threshold_hours,
        }

    except Exception as e:
        logger.exception(f"Error getting stuck files: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving stuck files",
        ) from e


def _handle_delete_action(
    db: Session,
    file_uuid: str,
    current_user: User,
    force: bool,
    organization_id: OrgScope = UNSCOPED,
) -> BulkActionResult:
    """Handle delete action for bulk operations."""
    delete_media_file(db, file_uuid, current_user, force=force, organization_id=organization_id)
    return BulkActionResult(
        file_uuid=file_uuid,
        success=True,
        message="File deleted successfully",
    )


def _handle_retry_action(
    db: Session, file_uuid: str, file_id: int, reset_retry_count: bool
) -> BulkActionResult:
    """Handle retry action for bulk operations."""
    import os

    success = reset_file_for_retry(db, file_id, reset_retry_count)
    if not success:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="Failed to reset file for retry",
            error="RESET_FAILED",
        )

    if os.environ.get("SKIP_CELERY", "False").lower() != "true":
        task_id = dispatch_transcription_pipeline(file_uuid=file_uuid)
        message = f"Retry started (task: {task_id})"
    else:
        message = "Retry prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)


def _handle_cancel_action(db: Session, file_uuid: str, file_id: int) -> BulkActionResult:
    """Handle cancel action for bulk operations."""
    success = cancel_active_task(db, file_id)
    return BulkActionResult(
        file_uuid=file_uuid,
        success=success,
        message="Task cancelled successfully" if success else "Failed to cancel task",
        error=None if success else "CANCEL_FAILED",
    )


def _handle_recover_action(db: Session, file_uuid: str, file_id: int) -> BulkActionResult:
    """Handle recover action for bulk operations."""
    success = recover_stuck_file(db, file_id)
    return BulkActionResult(
        file_uuid=file_uuid,
        success=success,
        message="File recovered successfully" if success else "Failed to recover file",
        error=None if success else "RECOVERY_FAILED",
    )


def _handle_reprocess_action(
    db: Session,
    file_uuid: str,
    file_id: int,
    stages: list[str] | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
) -> BulkActionResult:
    """Handle reprocess action for bulk operations."""
    import os

    from app.models.media import MediaFile

    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File not found",
            error="NOT_FOUND",
        )

    # Only reprocess completed or error files
    if db_file.status not in [FileStatus.COMPLETED, FileStatus.ERROR, FileStatus.CANCELLED]:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message=f"Cannot reprocess file in {db_file.status} status",
            error="INVALID_STATUS",
        )

    if stages:
        # Selective reprocessing
        from app.api.endpoints.files.reprocess import clear_selective_data
        from app.api.endpoints.files.reprocess import dispatch_selective_tasks

        if "transcription" in stages:
            success = reset_file_for_retry(db, file_id, True)
            if not success:
                return BulkActionResult(
                    file_uuid=file_uuid,
                    success=False,
                    message="Failed to reset file for reprocessing",
                    error="RESET_FAILED",
                )

        clear_selective_data(db, db_file, stages)
        dispatch_selective_tasks(
            file_uuid,
            stages,
            min_speakers,
            max_speakers,
            num_speakers,
            file_id=file_id,
            user_id=int(db_file.user_id),
        )
        message = f"Selective reprocessing started (stages: {', '.join(stages)})"
        return BulkActionResult(file_uuid=file_uuid, success=True, message=message)

    # Full reprocess (backward compatible)
    success = reset_file_for_retry(db, file_id, True)
    if not success:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="Failed to reset file for reprocessing",
            error="RESET_FAILED",
        )

    if os.environ.get("SKIP_CELERY", "False").lower() != "true":
        task_id = dispatch_transcription_pipeline(file_uuid=file_uuid)
        message = f"Reprocessing started (task: {task_id})"
    else:
        message = "Reprocessing prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)


def _handle_summarize_action(
    db: Session, file_uuid: str, file_id: int, user_id: int | None = None
) -> BulkActionResult:
    """Handle summarize action for bulk operations."""
    import os

    from app.models.media import MediaFile
    from app.services.llm_service import LLMService

    # Check if LLM is configured
    try:
        llm_service = LLMService.create_from_settings(user_id=user_id)
        if llm_service is None:
            return BulkActionResult(
                file_uuid=file_uuid,
                success=False,
                message="LLM provider is not configured",
                error="LLM_NOT_AVAILABLE",
            )
        llm_service.close()
    except Exception:
        # Bulk actions report per-file outcomes instead of aborting the batch,
        # so an unconstructable LLM client becomes this file's failure result.
        logger.exception(f"Could not construct an LLM client for file {file_uuid}")
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="LLM provider is not configured",
            error="LLM_NOT_AVAILABLE",
        )

    # Check file status - must be completed
    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file or db_file.status != FileStatus.COMPLETED:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File must be completed before summarizing",
            error="INVALID_STATUS",
        )

    if os.environ.get("SKIP_CELERY", "False").lower() != "true":
        task = summarize_transcript_task.delay(
            file_uuid=file_uuid,
            force_regenerate=False,
        )
        message = f"Summarization started (task: {task.id})"
    else:
        message = "Summarization prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)


def _handle_identify_speakers_action(
    db: Session, file_uuid: str, file_id: int, user_id: int | None = None
) -> BulkActionResult:
    """Handle LLM speaker identification for bulk operations.

    Mirrors ``POST /api/files/{uuid}/identify-speakers``. Suggestions are stored as
    confidence-scored predictions for manual review and are never auto-applied.

    Availability is probed by constructing the client, as ``_handle_summarize_action``
    does — the async ``is_llm_available`` helper the single-file endpoint uses cannot be
    awaited from this synchronous dispatch.
    """
    import os

    from app.models.media import MediaFile
    from app.services.llm_service import LLMService

    # Via the `speaker_tasks` re-export shim, as every other caller does — it keeps the
    # legacy task-name routing alive.
    from app.tasks.speaker_tasks import identify_speakers_llm_task

    try:
        llm_service = LLMService.create_from_settings(user_id=user_id)
        if llm_service is None:
            return BulkActionResult(
                file_uuid=file_uuid,
                success=False,
                message="LLM provider is not configured",
                error="LLM_NOT_AVAILABLE",
            )
        llm_service.close()
    except Exception:
        # Bulk actions report per-file outcomes instead of aborting the batch.
        logger.exception(f"Could not construct an LLM client for file {file_uuid}")
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="LLM provider is not configured",
            error="LLM_NOT_AVAILABLE",
        )

    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file or db_file.status != FileStatus.COMPLETED:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File must be completed before identifying speakers",
            error="INVALID_STATUS",
        )

    if not db_file.speakers:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="No speakers found in this file to identify",
            error="NO_SPEAKERS",
        )

    if os.environ.get("SKIP_CELERY", "False").lower() != "true":
        task = identify_speakers_llm_task.delay(file_uuid=file_uuid)
        message = f"Speaker identification started (task: {task.id})"
    else:
        message = "Speaker identification prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)


def _handle_redact_action(db: Session, file_uuid: str, file_id: int) -> BulkActionResult:
    """Handle content-redaction (re)scan for bulk/selective operations.

    Re-runs redaction detection for a completed file (e.g., old files that predate the
    feature, or to apply a settings change). Idempotent — replaces cached spans.
    """
    import os

    from app.core import constants as C  # noqa: N812
    from app.models.media import MediaFile

    db_file = db.query(MediaFile).filter_by(id=file_id).first()
    if not db_file or db_file.status != FileStatus.COMPLETED:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="File must be completed before redaction",
            error="INVALID_STATUS",
        )
    if not db_file.transcript_segments:
        return BulkActionResult(
            file_uuid=file_uuid,
            success=False,
            message="No transcript to redact",
            error="NO_TRANSCRIPT",
        )

    # Mark pending so the UI shows the "Redacting…" state immediately.
    db_file.redaction_status = C.REDACTION_STATUS_PENDING  # type: ignore[assignment]
    db.commit()

    if os.environ.get("SKIP_CELERY", "False").lower() != "true":
        from app.tasks.redaction_task import redaction_detect_task

        task = redaction_detect_task.delay(file_id=file_id, user_id=int(db_file.user_id))
        message = f"Redaction started (task: {task.id})"
    else:
        message = "Redaction prepared (test mode)"

    return BulkActionResult(file_uuid=file_uuid, success=True, message=message)


def _handle_tag_action(
    db: Session,
    file_uuid: str,
    file_id: int,
    tag: Tag | None,
    *,
    add: bool,
    user_id: int,
    is_admin: bool,
) -> BulkActionResult:
    """Attach or detach the batch's tag on one file, as a per-file outcome.

    The mutation itself lives in ``app.services.tag_bulk`` — this only maps its
    outcome onto the bulk envelope.
    """
    applied = apply_tag_to_file(
        db, file_id=file_id, tag=tag, add=add, user_id=user_id, is_admin=is_admin
    )
    return BulkActionResult(
        file_uuid=file_uuid,
        success=applied.success,
        message=applied.message,
        error=applied.error,
        outcome=applied.outcome,
    )


def _process_single_file_action(
    db: Session,
    file_uuid: str,
    action: str,
    current_user: User,
    is_admin: bool,
    force: bool,
    reset_retry_count: bool,
    stages: list[str] | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    num_speakers: int | None = None,
    organization_id: OrgScope = UNSCOPED,
    tag: Tag | None = None,
) -> BulkActionResult:
    """Process a single file action, returning the result."""
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=is_admin, organization_id=organization_id
    )
    file_id = db_file.id

    action_handlers = {
        "delete": lambda: _handle_delete_action(
            db, file_uuid, current_user, force, organization_id
        ),
        "retry": lambda: _handle_retry_action(db, file_uuid, file_id, reset_retry_count),
        "cancel": lambda: _handle_cancel_action(db, file_uuid, file_id),
        "recover": lambda: _handle_recover_action(db, file_uuid, file_id),
        "reprocess": lambda: _handle_reprocess_action(
            db, file_uuid, file_id, stages, min_speakers, max_speakers, num_speakers
        ),
        "summarize": lambda: _handle_summarize_action(db, file_uuid, file_id, current_user.id),
        "redact": lambda: _handle_redact_action(db, file_uuid, file_id),
        "identify_speakers": lambda: _handle_identify_speakers_action(
            db, file_uuid, file_id, current_user.id
        ),
        "add_tag": lambda: _handle_tag_action(
            db, file_uuid, file_id, tag, add=True, user_id=current_user.id, is_admin=is_admin
        ),
        "remove_tag": lambda: _handle_tag_action(
            db, file_uuid, file_id, tag, add=False, user_id=current_user.id, is_admin=is_admin
        ),
    }

    handler = action_handlers.get(action)
    if handler:
        return handler()

    return BulkActionResult(
        file_uuid=file_uuid,
        success=False,
        message=f"Unknown action: {action}",
        error="UNKNOWN_ACTION",
    )


@router.post("/management/bulk-action", response_model=list[BulkActionResult])
def bulk_file_action(
    request: BulkActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Perform bulk actions on multiple files."""
    is_tag_action = request.action in TAG_ACTIONS
    tag = None
    if is_tag_action:
        try:
            tag = resolve_bulk_tag(db, request.action, request.tag_name, user_id=current_user.id)
        except InvalidTagNameError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Tag name is required",
            ) from exc

    try:
        results = []
        is_admin = current_user.is_admin

        for file_uuid in request.file_uuids:
            try:
                result = _process_single_file_action(
                    db=db,
                    file_uuid=file_uuid,
                    action=request.action,
                    current_user=current_user,
                    is_admin=is_admin,
                    force=request.force,
                    reset_retry_count=request.reset_retry_count,
                    stages=request.stages if request.stages else None,
                    min_speakers=request.min_speakers,
                    max_speakers=request.max_speakers,
                    num_speakers=request.num_speakers,
                    organization_id=ctx.org_id,
                    tag=tag,
                )
                results.append(result)
            except HTTPException as e:
                results.append(
                    BulkActionResult(
                        file_uuid=file_uuid,
                        success=False,
                        message=str(e.detail),
                        error="HTTP_ERROR",
                        outcome=BulkTagOutcome.FAILED if is_tag_action else None,
                    )
                )
            except Exception as e:
                logger.exception(f"Error processing bulk action for file {file_uuid}: {e}")
                results.append(
                    BulkActionResult(
                        file_uuid=file_uuid,
                        success=False,
                        message=f"Unexpected error: {str(e)}",
                        error="UNEXPECTED_ERROR",
                        outcome=BulkTagOutcome.FAILED if is_tag_action else None,
                    )
                )

        if is_tag_action:
            # One refresh for the whole batch, and best-effort: every change in
            # `results` is already committed, so a failing reindex must not turn
            # a completed batch into a 500 (the task is idempotent, so a re-run
            # converges).
            try:
                refresh_tagged_files(
                    db,
                    [r.file_uuid for r in results if r.outcome in CHANGED_OUTCOMES],
                    user_id=current_user.id,
                )
            except Exception as e:
                logger.error(f"Could not refresh tags for bulk {request.action}: {e}")

        return results

    except Exception as e:
        logger.exception(f"Error in bulk file action: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error performing bulk file action",
        ) from e


@router.post("/management/cleanup-orphaned")
def cleanup_orphaned_files(
    dry_run: bool = Query(False, description="Preview changes without applying them"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clean up orphaned files (admin only)."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can cleanup orphaned files",
        )

    try:
        # Find stuck files
        stuck_file_ids = check_for_stuck_files(db, stuck_threshold_hours=6)

        cleanup_results: dict[str, int | list[str] | bool] = {
            "stuck_files_found": len(stuck_file_ids),
            "recovered": 0,
            "marked_orphaned": 0,
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
                    logger.exception(f"Orphan cleanup failed for file {file_id}")
                    errors_list = cleanup_results["errors"]
                    if isinstance(errors_list, list):
                        errors_list.append(f"Error processing file {file_id}: {str(e)}")

        return cleanup_results

    except Exception as e:
        logger.exception(f"Error in cleanup orphaned files: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error cleaning up orphaned files",
        ) from e
