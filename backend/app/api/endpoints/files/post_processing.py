"""Post-processing status of one file: the missed-frame fallback for the notification card."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.schemas.post_processing import PostProcessingStatusResponse
from app.services.post_processing_status import read_status
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{file_uuid}/post-processing", response_model=PostProcessingStatusResponse)
def get_post_processing_status(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> PostProcessingStatusResponse:
    """The states the file's post-processing tasks last reported.

    Read by the notification card when it may have missed ``post_processing_status``
    frames (a reload, a dropped socket). Tenant- and quarantine-gated like every file
    read; an unreadable store answers "no run known" rather than an error, because the
    card must then say it cannot confirm the final steps.
    """
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
    )
    try:
        stored = read_status(int(media_file.id))
    except Exception as exc:  # noqa: BLE001 — "unknown" is the honest answer
        logger.warning("post-processing status unreadable for file %s: %s", media_file.id, exc)
        stored = {"run_at": None, "tasks": {}}
    return PostProcessingStatusResponse(
        file_id=str(media_file.uuid), run_at=stored["run_at"], tasks=stored["tasks"]
    )
