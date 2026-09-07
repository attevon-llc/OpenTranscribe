"""
Summary status endpoint for checking AI summary availability and status
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.services.llm_service import is_llm_available
from app.tasks.summary_retry import retry_summary_if_available

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{file_uuid}/summary-status")
async def get_summary_status(
    file_uuid: str,  # Changed from int to str for UUID
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Get the summary status for a file and LLM availability

    Returns:
        - summary_status: 'pending', 'processing', 'completed', 'failed'
        - llm_available: whether LLM service is available
        - can_retry: whether a failed summary can be retried
        - summary_exists: whether a summary already exists
    """
    # Get file by UUID (tenant-gated via ctx.org_id)
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id

    try:
        # Check LLM availability for current user
        llm_available = await is_llm_available(user_id=current_user.id)
    except Exception as e:
        logger.warning(f"Failed to check LLM availability for file {file_id}: {e}")
        llm_available = False

    # Check summary enable settings
    from app.utils.summary_settings import is_summary_enabled_for_user
    from app.utils.summary_settings import is_summary_enabled_system

    system_enabled = is_summary_enabled_system(db)
    user_enabled = is_summary_enabled_for_user(db, current_user.id)

    # Determine if retry is possible (not for disabled status)
    summary_status_str = media_file.summary_status or "pending"
    can_retry = (
        summary_status_str == "failed"
        and llm_available
        and media_file.status == "completed"  # Transcription must be complete
    )

    # Whether the user can generate a summary (manual trigger)
    is_admin = getattr(current_user, "role", None) == "admin"
    can_generate = (
        llm_available and media_file.status == "completed" and (is_admin or system_enabled)
    )

    return {
        "file_id": str(media_file.uuid),  # Use UUID for frontend
        "summary_status": summary_status_str,
        "summary_exists": bool(media_file.summary_data or media_file.summary_opensearch_id),
        "llm_available": llm_available,
        "can_retry": can_retry,
        "transcription_status": media_file.status,
        "filename": media_file.filename,
        "summary_enabled_system": system_enabled,
        "summary_enabled_user": user_enabled,
        "can_generate": can_generate,
    }


@router.post("/{file_uuid}/retry-summary")
async def retry_summary(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Retry failed summary generation for a file
    """
    # Get file by UUID with permission check (tenant-gated via ctx.org_id)
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        is_admin=current_user.is_admin,
        organization_id=ctx.org_id,
        min_permission="editor",
    )
    file_id = media_file.id

    # Check if retry is needed and possible
    if str(media_file.summary_status) == "disabled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This file has summary generation disabled. "
                "Use 'Generate Summary' to manually create one, "
                "or update your settings to re-enable auto-generation."
            ),
        )
    if media_file.summary_status not in ["failed", "pending"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot retry summary with status: {media_file.summary_status}",
        )

    if media_file.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot generate summary until transcription is completed",
        )

    # Check LLM availability for current user
    try:
        llm_available = await is_llm_available(user_id=current_user.id)
    except Exception as e:
        logger.exception(f"Failed to check LLM availability for retry of file {file_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify LLM service availability. Please try again later.",
        ) from e

    if not llm_available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM service is not available. Please try again later.",
        )

    # Attempt to retry. retry_summary_if_available takes the file UUID, not the
    # internal integer PK (`file_id` above) — get_by_uuid_optional inside it
    # would never match on a stringified PK, so this always returned False and
    # the endpoint always 500'd "Failed to queue summary retry" (issue #474).
    success = await retry_summary_if_available(db, str(media_file.uuid))
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue summary retry",
        )

    logger.info(f"Summary retry queued for file {file_id} by user {current_user.email}")

    return {
        "status": "success",
        "message": "Summary generation has been queued",
        "file_id": str(media_file.uuid),  # Use UUID for frontend
    }
