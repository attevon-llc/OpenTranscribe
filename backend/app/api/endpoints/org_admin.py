"""Organization-admin endpoints — tenant-scoped administration (cloud seam).

Every route here is gated by ``require_org_admin`` (``app.api.deps_context``),
the **server-side** authority for org-admin actions. The frontend
``audience == "org_admin"`` capability gate is cosmetic — it decides what UI
renders, never who is authorized. These routes therefore enforce the
membership-mirror role (``ctx.org_role == "org:admin"``), not the token or the
frontend.

Surfaces:
  - ``GET  /org-admin/audit-logs``               — read audit events for the
    caller's OWN org only (scoped to the org's member user-ids). The global,
    all-tenant view stays super-admin-only at ``GET /admin/audit-logs``.
  - ``POST /org-admin/gdpr/erase-user/{uuid}``   — GDPR Art. 17 erasure of a
    member of the caller's org.
  - ``POST /org-admin/gdpr/erase-organization``  — GDPR erasure of the caller's
    entire org (org data + member-owned org data + voiceprints).

Community-edition invariance: with no orgs, ``require_org_admin`` 403s on every
route (``ctx.is_org_admin`` is always False), so this router is inert in the
self-host/community edition — these are cloud-only surfaces.
"""

import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import require_org_admin
from app.auth.audit import query_audit_logs
from app.db.base import get_db
from app.models.organization import OrganizationMembership
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


def _org_member_user_ids(db: Session, org_id: int) -> list[int]:
    """Return the user-ids of all members of ``org_id`` (the audit-scope set)."""
    rows = (
        db.query(OrganizationMembership.user_id)
        .filter(OrganizationMembership.organization_id == org_id)
        .all()
    )
    return [int(r[0]) for r in rows]


@router.get("/audit-logs")
def get_org_audit_logs(
    start_date: datetime | None = Query(None, description="Start date for log query"),
    end_date: datetime | None = Query(None, description="End date for log query"),
    event_type: str | None = Query(None, description="Filter by event type"),
    user_id: int | None = Query(None, description="Filter by user ID (must be an org member)"),
    outcome: str | None = Query(None, description="Filter by outcome"),
    limit: int = Query(default=100, le=1000, description="Maximum results"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """Read audit events for the caller's OWN organization (org-admin only).

    Visibility (issue #262a): events STAMPED with this org's id — including
    events with ``user_id`` NULL, e.g. failed logins, which become visible to
    the org once stamped — plus **legacy** un-stamped events attributed via the
    org's member user-ids (the pre-v372 rule, kept so history written before
    org stamping stays readable; see ``build_org_scope_clause`` for the
    legacy-window caveat about shared members). Events stamped with another
    org are never visible. The ``user_id`` filter, if given, is constrained to
    a member of the org (403 otherwise) so it can't be used to probe outside
    the tenant.
    """
    member_ids = _org_member_user_ids(db, ctx.org_id)  # type: ignore[arg-type]

    if user_id is not None and user_id not in member_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requested user is not a member of your organization",
        )

    return query_audit_logs(
        start_date=start_date,
        end_date=end_date,
        event_type=event_type,
        user_id=user_id,
        outcome=outcome,
        scope_user_ids=member_ids,
        scope_org_id=int(ctx.org_id),  # type: ignore[arg-type]
        limit=limit,
        offset=offset,
    )


@router.post("/gdpr/erase-user/{user_uuid}")
def erase_org_member(
    user_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """GDPR Art. 17 erasure of a member's data WITHIN the caller's organization.

    The target must be a member of the caller's org (403 otherwise), and the
    erasure is strictly org-scoped: only the target's rows stamped with THIS
    org (media, org-scoped profiles/collections, the (user, org) voiceprint
    docs) are destroyed. The target's personal-scope data, other orgs' data,
    and their account survive — an org admin has authority over the tenant's
    data, never over the person's account (full account erasure is the data
    subject's own deletion flow or a platform super-admin action). Idempotent;
    SLA 30 days; audit-logged with the acting admin by the service.
    """
    target = db.query(User).filter(User.uuid == user_uuid).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    member_ids = _org_member_user_ids(db, ctx.org_id)  # type: ignore[arg-type]
    if int(target.id) not in member_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of your organization",
        )

    from app.services.gdpr_erasure_service import erase_org_member_data

    summary = erase_org_member_data(
        db,
        int(target.id),
        int(ctx.org_id),  # type: ignore[arg-type]
        actor_user_id=int(ctx.user.id),
        actor_email=str(ctx.user.email),
    )
    return summary


@router.post("/gdpr/erase-organization")
def erase_own_organization(
    confirm: bool = Query(False, description="Must be true to execute the irreversible erasure"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """GDPR erasure of the caller's ENTIRE organization (irreversible).

    Erases org-stamped media, member-owned org data, the org's voiceprints, and
    the organization row itself. Members keep their personal accounts. Requires
    ``confirm=true``. Idempotent; SLA 30 days.
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pass confirm=true to execute organization erasure (irreversible)",
        )

    from app.services.gdpr_erasure_service import erase_organization

    summary = erase_organization(
        db,
        int(ctx.org_id),  # type: ignore[arg-type]
        actor_user_id=int(ctx.user.id),
        actor_email=str(ctx.user.email),
    )
    return summary
