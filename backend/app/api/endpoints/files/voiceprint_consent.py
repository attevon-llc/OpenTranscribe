"""``PATCH /files/{uuid}/voiceprint-consent``: the owner's per-file voice identification choice.

Owner only (organization administrators restrict through organization policy, not per file). A
file the caller does not own, or cannot see, is a 404. Refused under a support grant. Moving to
``declined`` commits the state FIRST (that is what stops new voiceprints), then queues the
two-pass purge of the file's existing voiceprints.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.rate_limit import get_directory_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.file_consent import FileVoiceprintConsent
from app.schemas.file_consent import FileVoiceprintConsentUpdate
from app.services.privacy import file_consent
from app.services.privacy import policy_admin
from app.services.privacy.policy import clear_write_backstop_cache

logger = logging.getLogger(__name__)

router = APIRouter()


@router.patch(
    "/{file_uuid}/voiceprint-consent",
    response_model=FileVoiceprintConsent,
    dependencies=[Depends(refuse_under_support_grant)],
    responses={
        404: {"description": "No such file, or not the caller's."},
        503: {"description": "The state was saved but the purge could not be queued."},
    },
)
@limiter.limit(get_directory_rate_limit(), key_func=user_or_ip_key)
def set_file_voiceprint_consent(
    file_uuid: UUID,
    body: FileVoiceprintConsentUpdate,
    request: Request,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> FileVoiceprintConsent:
    """Record the owner's choice for one file; ``declined`` also deletes its voiceprints."""
    current_user: User = ctx.user
    row = (
        db.query(MediaFile)
        .filter(MediaFile.uuid == file_uuid, MediaFile.user_id == current_user.id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    change = file_consent.change_state(row, body.state, int(current_user.id))
    file_id = int(row.id)
    db.commit()
    clear_write_backstop_cache()

    source_ip, user_agent = _get_client_info(request)
    if change.changed:
        audit_logger.log(
            event_type=AuditEventType.PRIVACY_FILE_CONSENT_CHANGE,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=source_ip,
            user_agent=user_agent,
            details={
                "file_id": file_id,
                "from": change.previous,
                "to": change.current,
                "via": "owner",
            },
        )

    purge_task_id: str | None = None
    # Also when unchanged: a repeated 'declined' is how a client retries a purge that was lost.
    if body.state == "declined":
        try:
            purge_task_id = file_consent.start_opt_out_purge(
                db,
                file_id,
                actor_user_id=int(current_user.id),
                actor_username=str(current_user.email),
                source_ip=source_ip,
                user_agent=user_agent,
            )
        except policy_admin.PurgeNotQueuedError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Voice identification is off for this file, but deleting its existing "
                    "voiceprints could not be queued. Repeat the request to retry."
                ),
            ) from exc

    return FileVoiceprintConsent(
        state=body.state,
        previous_state=change.previous,  # type: ignore[arg-type]
        changed=change.changed,
        purge_task_id=purge_task_id,
        reprocess_to_create_voiceprints=(body.state == "granted" and change.previous == "declined"),
    )
