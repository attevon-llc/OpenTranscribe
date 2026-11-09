"""``/org-admin/ownership``: hand the organization to another of its administrators (v468).

``POST /ownership/transfer`` is for the current owner only (``require_org_owner``). The target
must be an ``org:admin`` of the same organization; an unknown user, a plain member and a member of
another organization all answer the same 409 ``not_an_org_admin``. Recorded as ``org.owner.change``.

Same gate as the rest of ``/org-admin``: the ``organizations`` capability (404 in the community
edition) and refused under a support access grant.
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

from app.api.deps_context import require_org_owner
from app.api.endpoints.org_admin import org_id_of
from app.api.rate_limit import get_auth_rate_limit
from app.api.rate_limit import limiter
from app.api.rate_limit import user_or_ip_key
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.services import organization_owner
from app.services.request_context import RequestContext
from app.services.support_access_service import coded_error

router = APIRouter()


class OwnershipTransfer(BaseModel):
    to_user_uuid: UUID


class OwnershipReceipt(BaseModel):
    owner_user_uuid: UUID


@router.post("/ownership/transfer", response_model=OwnershipReceipt)
@limiter.limit(get_auth_rate_limit(), key_func=user_or_ip_key)
def transfer_ownership(
    request: Request,
    response: Response,  # required by slowapi
    body: OwnershipTransfer,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_owner),
) -> OwnershipReceipt:
    """Hand the organization's ownership to another administrator of it (owner only)."""
    try:
        org_row = organization_owner.transfer(
            db,
            organization_id=org_id_of(ctx),
            actor=ctx.user,
            to_user_uuid=body.to_user_uuid,
            client=get_request_context(request),
        )
    except organization_owner.OwnerError as error:
        http = (
            status.HTTP_403_FORBIDDEN if error.code == "not_the_owner" else status.HTTP_409_CONFLICT
        )
        raise coded_error(http, error.code, error.message) from error
    owner = organization_owner.owner_user_uuid(db, int(org_row.id))
    return OwnershipReceipt(owner_user_uuid=owner)
