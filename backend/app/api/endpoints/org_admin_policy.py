"""``/org-admin/policy``: an organization's privacy policy and redaction floor (issues #1280, #1279).

The organization comes from the resolved request context and nowhere else: there is no
organization parameter, so an administrator can read and change only their own organization.

Gating: ``require_org_admin`` plus the server-enforced ``org.defaults`` capability, mounted in
``api/router.py`` with no platform-admin bypass (a superuser gets the same 404 while the key is
off) and refused under a support-access grant. The community edition has no organizations, so
every route answers 403 there.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import require_org_admin
from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.org_admin import org_id_of
from app.api.endpoints.privacy import queue_purge
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.capabilities import capability_enabled
from app.db.base import get_db
from app.schemas.privacy import OrgPolicy
from app.schemas.privacy import OrgPolicyUpdate
from app.schemas.privacy import PurgeAccepted
from app.schemas.privacy import PurgeRequest
from app.services.privacy import policy_admin
from app.services.privacy.purge import PurgeScope

logger = logging.getLogger(__name__)

router = APIRouter()

#: The capability that decides whether the redaction floor is part of this screen.
FLOOR_CAPABILITY = "redaction.policy"


@router.get("", response_model=OrgPolicy)
def get_org_policy(
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> OrgPolicy:
    """The caller's organization policy. ``redaction_floor`` is null while ``redaction.policy`` is off."""
    return policy_admin.read_org_policy(
        db, org_id_of(ctx), include_floor=capability_enabled(FLOOR_CAPABILITY, request)
    )


@router.patch(
    "",
    response_model=OrgPolicy,
    responses={404: {"description": "redaction_floor sent while redaction.policy is off."}},
)
def patch_org_policy(
    body: OrgPolicyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> OrgPolicy:
    """Partially update the caller's organization policy. Audited as org.policy.change (field names only)."""
    org_id = org_id_of(ctx)
    floor_enabled = capability_enabled(FLOOR_CAPABILITY, request)
    if body.redaction_floor is not None and not floor_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    changed = policy_admin.apply_org_policy_update(db, org_id, int(ctx.user.id), body)

    source_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ORG_POLICY_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=ctx.user.id,
        username=str(ctx.user.email),
        source_ip=source_ip,
        user_agent=user_agent,
        details={"fields": changed},
        organization_id=org_id,
    )
    return policy_admin.read_org_policy(db, org_id, include_floor=floor_enabled)


@router.post(
    "/purge",
    response_model=PurgeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "policy_still_enabled: a target is still switched on."}},
)
def purge_org_data(
    body: PurgeRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> PurgeAccepted:
    """Irreversibly delete the organization's voiceprints and/or inferred gender, all members' included."""
    scope = PurgeScope("organization", organization_id=org_id_of(ctx))
    return queue_purge(db, request, scope, list(body.targets), ctx.user)
