"""
Summary status endpoint for checking AI summary availability and status
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.core.exceptions import ServiceUnavailableError
from app.db.base import get_db
from app.legal.processing_gate import require_processing_allowed_for_file
from app.models.user import User
from app.services.llm_service import is_llm_available
from app.services.llm_settings_outage import llm_settings_outage_as_503
from app.services.request_context import RequestContext
from app.tasks.summary_retry import retry_summary_if_available

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{file_uuid}/summary-status")
async def get_summary_status(
    file_uuid: str,  # Changed from int to str for UUID
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """
    Get the summary status for a file and LLM availability

    Returns:
        - summary_status: 'pending', 'processing', 'completed', 'failed'
        - llm_available: whether LLM service is available
        - llm_settings_unavailable: the settings could not be read (outage), which is not
          the same as "no provider configured"
        - can_retry: whether a failed summary can be retried
        - summary_exists: whether a summary already exists
    """
    user_id = current_user.id
    # Every database read happens first, in the threadpool, and the request session is
    # released before the provider health check (an outbound call with a 3 s timeout):
    # the transaction the auth dependency opened must not stay open across it.
    state = await run_in_threadpool(_read_summary_state, db, file_uuid, user_id, ctx)
    file_id = state["file_id"]

    settings_unavailable = False
    try:
        # Check LLM availability for current user
        llm_available = await is_llm_available(user_id=user_id)
    except SQLAlchemyError:
        logger.error(f"LLM settings unreadable for file {file_id}", exc_info=True)
        llm_available = False
        settings_unavailable = True
    except Exception as e:
        logger.warning(f"Failed to check LLM availability for file {file_id}: {e}")
        llm_available = False

    # Determine if retry is possible (not for disabled status)
    summary_status_str = state["summary_status"]
    can_retry = (
        summary_status_str == "failed"
        and llm_available
        and state["status"] == "completed"  # Transcription must be complete
    )

    # Whether the user can generate a summary (manual trigger)
    can_generate = (
        llm_available
        and state["status"] == "completed"
        and (ctx.bypass.user_is_admin or state["system_enabled"])
    )

    return {
        "file_id": state["file_uuid"],  # Use UUID for frontend
        "summary_status": summary_status_str,
        "summary_exists": state["summary_exists"],
        "llm_available": llm_available,
        "llm_settings_unavailable": settings_unavailable,
        "can_retry": can_retry,
        "transcription_status": state["status"],
        "filename": state["filename"],
        "summary_enabled_system": state["system_enabled"],
        "summary_enabled_user": state["user_enabled"],
        "can_generate": can_generate,
    }


def _read_summary_state(
    db: Session, file_uuid: str, user_id: int, ctx: RequestContext
) -> dict[str, Any]:
    """Read what the summary-status response needs as plain data, then release ``db``."""
    from app.services.uuid_helpers import get_file_by_uuid_with_permission
    from app.utils.summary_settings import is_summary_enabled_for_user
    from app.utils.summary_settings import is_summary_enabled_system

    # Get file by UUID (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, user_id, bypass=ctx.bypass, organization_id=ctx.org_id
    )
    state = {
        "file_id": media_file.id,
        "file_uuid": str(media_file.uuid),
        "summary_status": media_file.summary_status or "pending",
        "summary_exists": bool(media_file.summary_data or media_file.summary_opensearch_id),
        "status": media_file.status,
        "filename": media_file.filename,
        "system_enabled": is_summary_enabled_system(db),
        "user_enabled": is_summary_enabled_for_user(db, user_id),
    }
    db.close()
    return state


def _retry_preconditions(
    db: Session, file_uuid: str, current_user: User, ctx: RequestContext
) -> tuple[int, str]:
    """Refuse a retry the file cannot take; return ``(file_id, file_uuid)`` and release ``db``.

    Raises:
        HTTPException: 400 when summaries are disabled for the file, its summary is not
            failed or pending, or its transcription is not complete; and whatever the
            permission, processing and hold gates raise.
    """
    from app.api.hold_refusal import refuse_if_frozen
    from app.services.uuid_helpers import get_file_by_uuid_with_permission

    # Get file by UUID with permission check (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
        min_permission="editor",
    )
    file_id = int(media_file.id)
    require_processing_allowed_for_file(db, media_file)
    refuse_if_frozen(db, file_id, actor=current_user, action="summary_generate")

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
    file_ref = str(media_file.uuid)
    db.close()
    return file_id, file_ref


@router.post("/{file_uuid}/retry-summary", dependencies=[Depends(refuse_under_support_grant)])
async def retry_summary(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """
    Retry failed summary generation for a file

    The checks and the reset run through ``run_in_threadpool``; the request transaction
    is released before the provider health check, which is the only work awaited on the
    event loop.
    """
    user_id = int(current_user.id)
    file_id, file_ref = await run_in_threadpool(
        _retry_preconditions, db, file_uuid, current_user, ctx
    )

    # Check LLM availability for current user
    try:
        with llm_settings_outage_as_503():
            llm_available = await is_llm_available(user_id=user_id)
    except ServiceUnavailableError:
        raise
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
    success = await retry_summary_if_available(db, file_ref)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue summary retry",
        )

    logger.info(f"Summary retry queued for file {file_id} by user {user_id}")

    return {
        "status": "success",
        "message": "Summary generation has been queued",
        "file_id": file_ref,  # Use UUID for frontend
    }
