"""Support-access grants: how platform staff reach a tenant's content (issue #1122).

Three routers share one service:

* ``router``          ``/support-access``            staff request, list, revoke and break glass
* ``org_router``      ``/org-admin/support-access``  a tenant's org admins decide and audit
* ``me_router``       ``/users/me/support-access``   a person decides for their own workspace

Every route answers 404 outside multi-tenant mode, and none of them ever reads the
``X-Support-Access-Grant`` header: they depend on ``get_base_context`` /
``get_current_active_user`` only, so a stale header can never block the call that would
clear it (plan rule 10). Authority is stated per route; a grant the caller may not see is
a 404, never a 403, so uuids cannot be enumerated.
"""

import logging
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_base_context
from app.api.deps_context import require_org_admin
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import SUPPORT_ACCESS_REQUEST_RATE_LIMIT
from app.db.base import get_db
from app.models.organization import Organization
from app.models.support_access import SupportAccessGrant
from app.models.user import User
from app.schemas.support_access import ApproveBody
from app.schemas.support_access import BreakGlassBody
from app.schemas.support_access import CreateGrantBody
from app.schemas.support_access import DecisionNoteBody
from app.schemas.support_access import GrantPage
from app.schemas.support_access import OrgTargetOut
from app.schemas.support_access import SupportGrantOut
from app.schemas.support_access import UsePage
from app.services import support_access_lifecycle as lifecycle
from app.services.platform_access import TenancyMode
from app.services.platform_access import tenancy_mode
from app.services.support_access_queries import find_grant
from app.services.support_access_queries import list_grants
from app.services.support_access_queries import list_uses
from app.services.support_access_queries import to_out

logger = logging.getLogger(__name__)

GrantStatusQuery = Literal["pending", "active", "denied", "expired", "revoked", "lapsed"]


def require_multi_tenant(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_active_user),
) -> None:
    """404 outside multi-tenant mode: in single-tenant mode there is nothing to grant."""
    if tenancy_mode(db) is not TenancyMode.MULTI:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(dependencies=[Depends(require_multi_tenant)])
org_router = APIRouter(dependencies=[Depends(require_multi_tenant)])
me_router = APIRouter(dependencies=[Depends(require_multi_tenant)])


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _require_grant(db: Session, grant_uuid: str) -> SupportAccessGrant:
    grant = find_grant(db, grant_uuid)
    if grant is None:
        raise _not_found()
    return grant


# --- staff: /support-access --------------------------------------------------------------


@router.post("/grants", response_model=SupportGrantOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(SUPPORT_ACCESS_REQUEST_RATE_LIMIT, key_func=user_or_ip_key)
def create_grant(
    request: Request,
    response: Response,
    body: CreateGrantBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> SupportGrantOut:
    """Request access to a tenant. It stays ``pending`` until the tenant decides."""
    return to_out(db, lifecycle.request_grant(db, current_user, body))


@router.post(
    "/grants/break-glass", response_model=SupportGrantOut, status_code=status.HTTP_201_CREATED
)
@limiter.limit(SUPPORT_ACCESS_REQUEST_RATE_LIMIT, key_func=user_or_ip_key)
def create_break_glass_grant(
    request: Request,
    response: Response,
    body: BreakGlassBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> SupportGrantOut:
    """Open access immediately. super_admin only; the tenant is notified in real time."""
    return to_out(db, lifecycle.break_glass(db, current_user, body))


@router.get("/grants", response_model=GrantPage)
def list_my_grants(
    scope: Literal["mine", "all"] = Query("mine"),
    status_filter: GrantStatusQuery | None = Query(None, alias="status"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> GrantPage:
    """The caller's grants, newest first. ``scope=all`` is super_admin only."""
    where = []
    if scope == "all":
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="super_admin required for scope=all")
    else:
        where.append(SupportAccessGrant.grantee_user_id == current_user.id)
    return list_grants(db, where=where, status_filter=status_filter, limit=limit, offset=offset)


@router.get("/targets/organizations", response_model=list[OrgTargetOut])
def search_target_organizations(
    q: str | None = Query(None, max_length=100),
    limit: int = Query(25, ge=1, le=25),
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
) -> list[OrgTargetOut]:
    """Active organizations an admin may request access to (a platform operation)."""
    query = db.query(Organization).filter(Organization.is_active.is_(True))
    if q:
        like = "%" + q.replace("%", r"\%").replace("_", r"\_") + "%"
        query = query.filter(Organization.name.ilike(like, escape="\\"))
    rows = query.order_by(Organization.name).limit(limit).all()
    return [OrgTargetOut(uuid=str(o.uuid), name=str(o.name), slug=o.slug) for o in rows]


def _visible_to_staff(grant: SupportAccessGrant, user: User) -> bool:
    return grant.grantee_user_id == user.id or bool(user.is_superuser)


@router.get("/grants/{grant_uuid}", response_model=SupportGrantOut)
def get_grant(
    grant_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> SupportGrantOut:
    grant = _require_grant(db, grant_uuid)
    if not _visible_to_staff(grant, current_user):
        raise _not_found()
    return to_out(db, grant)


@router.get("/grants/{grant_uuid}/uses", response_model=UsePage)
def get_grant_uses(
    grant_uuid: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> UsePage:
    grant = _require_grant(db, grant_uuid)
    if not _visible_to_staff(grant, current_user):
        raise _not_found()
    return list_uses(db, grant, limit=limit, offset=offset)


@router.post("/grants/{grant_uuid}/revoke", response_model=SupportGrantOut)
def revoke_grant(
    grant_uuid: str,
    body: DecisionNoteBody | None = None,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_base_context),
) -> SupportGrantOut:
    """End a grant now. The grantee, any super_admin, the tenant's org admin or the subject.

    Idempotent: revoking a grant that is already over returns it unchanged.
    """
    grant = _require_grant(db, grant_uuid)
    user = ctx.user
    is_tenant_admin = (
        grant.organization_id is not None
        and ctx.org_id == grant.organization_id
        and ctx.is_org_admin
    )
    allowed = (
        grant.grantee_user_id == user.id
        or bool(user.is_superuser)
        or is_tenant_admin
        or (grant.subject_user_id is not None and grant.subject_user_id == user.id)
    )
    if not allowed:
        raise _not_found()
    note = body.note if body else None
    return to_out(db, lifecycle.revoke(db, user, grant, note=note))


# --- tenant: /org-admin/support-access ---------------------------------------------------


def _org_grant(db: Session, grant_uuid: str, ctx: RequestContext) -> SupportAccessGrant:
    grant = _require_grant(db, grant_uuid)
    if grant.organization_id is None or grant.organization_id != ctx.org_id:
        raise _not_found()
    return grant


@org_router.get("", response_model=GrantPage)
def list_org_grants(
    status_filter: GrantStatusQuery | None = Query(None, alias="status"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> GrantPage:
    where = [SupportAccessGrant.organization_id == ctx.org_id]
    return list_grants(db, where=where, status_filter=status_filter, limit=limit, offset=offset)


@org_router.get("/{grant_uuid}/uses", response_model=UsePage)
def list_org_grant_uses(
    grant_uuid: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> UsePage:
    return list_uses(db, _org_grant(db, grant_uuid, ctx), limit=limit, offset=offset)


@org_router.post("/{grant_uuid}/approve", response_model=SupportGrantOut)
def approve_org_grant(
    grant_uuid: str,
    body: ApproveBody | None = None,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> SupportGrantOut:
    grant = _org_grant(db, grant_uuid, ctx)
    minutes = body.duration_minutes if body else None
    return to_out(
        db, lifecycle.approve(db, ctx.user, grant, duration_minutes=minutes, org_route=True)
    )


@org_router.post("/{grant_uuid}/deny", response_model=SupportGrantOut)
def deny_org_grant(
    grant_uuid: str,
    body: DecisionNoteBody | None = None,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> SupportGrantOut:
    grant = _org_grant(db, grant_uuid, ctx)
    note = body.note if body else None
    return to_out(db, lifecycle.deny(db, ctx.user, grant, org_route=True, note=note))


# --- subject: /users/me/support-access ---------------------------------------------------


def _my_grant(db: Session, grant_uuid: str, user: User) -> SupportAccessGrant:
    grant = _require_grant(db, grant_uuid)
    if grant.subject_user_id is None or grant.subject_user_id != user.id:
        raise _not_found()
    return grant


@me_router.get("", response_model=GrantPage)
def list_my_workspace_grants(
    status_filter: GrantStatusQuery | None = Query(None, alias="status"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> GrantPage:
    where = [SupportAccessGrant.subject_user_id == current_user.id]
    return list_grants(db, where=where, status_filter=status_filter, limit=limit, offset=offset)


@me_router.get("/{grant_uuid}/uses", response_model=UsePage)
def list_my_workspace_grant_uses(
    grant_uuid: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> UsePage:
    return list_uses(db, _my_grant(db, grant_uuid, current_user), limit=limit, offset=offset)


@me_router.post("/{grant_uuid}/approve", response_model=SupportGrantOut)
def approve_my_workspace_grant(
    grant_uuid: str,
    body: ApproveBody | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SupportGrantOut:
    grant = _my_grant(db, grant_uuid, current_user)
    minutes = body.duration_minutes if body else None
    return to_out(
        db, lifecycle.approve(db, current_user, grant, duration_minutes=minutes, org_route=False)
    )


@me_router.post("/{grant_uuid}/deny", response_model=SupportGrantOut)
def deny_my_workspace_grant(
    grant_uuid: str,
    body: DecisionNoteBody | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SupportGrantOut:
    grant = _my_grant(db, grant_uuid, current_user)
    note = body.note if body else None
    return to_out(db, lifecycle.deny(db, current_user, grant, org_route=False, note=note))
