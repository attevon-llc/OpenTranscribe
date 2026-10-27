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
from app.db.base import get_db
from app.schemas.privacy import OrgPolicy
from app.schemas.privacy import OrgPolicyUpdate
from app.schemas.privacy import PurgeAccepted
from app.schemas.privacy import PurgeRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _not_implemented() -> HTTPException:
    return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@router.get("", response_model=OrgPolicy)
def get_org_policy(
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> OrgPolicy:
    """The caller's organization policy. ``redaction_floor`` is null while ``redaction.policy`` is off."""
    raise _not_implemented()


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
    raise _not_implemented()


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
    raise _not_implemented()
