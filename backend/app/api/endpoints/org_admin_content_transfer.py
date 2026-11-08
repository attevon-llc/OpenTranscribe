"""``POST /org-admin/members/{user_uuid}/transfer-content``: hand a member's organization content
to an administrator (issue #1104).

Org admin only (the same gate as the rest of ``/org-admin``: the ``organizations`` capability,
404 in the community edition, refused under a support grant). Moves only the caller's own
organization's rows; held files stay with their owner and are counted in ``skipped_held``.
Recorded as ``org.content.transfer``. Safe to repeat: a second call moves nothing and re-runs the
search restamp.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps_context import require_org_admin
from app.api.endpoints.org_admin import org_id_of
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.services import org_content_transfer
from app.services.org_audit_view import member_id_for_uuid
from app.services.request_context import RequestContext
from app.services.support_access_service import coded_error

router = APIRouter()


class ContentTransfer(BaseModel):
    """``to_user_uuid`` omitted = the organization's owner, else its longest-standing admin."""

    to_user_uuid: UUID | None = None


class ContentTransferReceipt(BaseModel):
    to_user_uuid: UUID
    counts: dict[str, int]
    skipped_held: int
    skipped_conflict: dict[str, int]
    index_complete: bool


@router.post("/members/{user_uuid}/transfer-content", response_model=ContentTransferReceipt)
@limiter.limit(get_auth_rate_limit(), key_func=user_or_ip_key)
def transfer_member_content(
    request: Request,
    response: Response,  # required by slowapi
    user_uuid: UUID,
    body: ContentTransfer,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> ContentTransferReceipt:
    """Re-own a member's organization content (files, speakers, collections, tags, ...)."""
    org_id = org_id_of(ctx)
    from_id = member_id_for_uuid(db, org_id, user_uuid)
    if from_id is None:
        raise coded_error(status.HTTP_404_NOT_FOUND, "member_not_found", "Member not found.")
    to_id: int | None = None
    if body.to_user_uuid is not None:
        to_id = org_content_transfer.user_id_for_uuid(db, body.to_user_uuid)
        if to_id is None:
            raise coded_error(
                status.HTTP_409_CONFLICT,
                "not_an_org_admin",
                "The target must be an administrator of this organization.",
            )
    try:
        result = org_content_transfer.transfer_org_content(
            db,
            organization_id=org_id,
            from_user_id=from_id,
            to_user_id=to_id,
            actor=ctx.user,
            client=get_request_context(request),
            reason="admin_transfer",
        )
    except org_content_transfer.TransferError as error:
        http = (
            status.HTTP_404_NOT_FOUND
            if error.code == "organization_not_found"
            else status.HTTP_409_CONFLICT
        )
        raise coded_error(http, error.code, error.message) from error
    target_uuid = org_content_transfer.uuid_for_user_id(db, result.to_user_id)
    return ContentTransferReceipt(
        to_user_uuid=target_uuid,
        counts=result.counts,
        skipped_held=result.skipped_held,
        skipped_conflict=result.skipped_conflict,
        index_complete=result.index_complete,
    )
