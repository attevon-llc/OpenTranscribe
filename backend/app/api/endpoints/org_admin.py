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
    ``GET /org-admin/audit-logs/export`` (``org_admin_audit.py``) downloads the same
    view behind its own ``org.audit_logs`` capability.
  - The scheduled GDPR erasure routes (``/org-admin/gdpr/erase-user/{uuid}``,
    ``/gdpr/erase-organization``, ``/gdpr/erasures``) live in ``org_admin_erasure.py``.

Community-edition invariance: with no orgs, the router's ``organizations``
capability is off (``app.api.router``), so every route answers **404** before
``require_org_admin`` is reached — the surface does not exist, exactly like an
unknown route. ``require_org_admin`` 403s only a caller who reaches the route
with the capability on but is not an admin of an active org. These are
cloud-only surfaces, inert in the self-host/community edition.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import require_org_admin
from app.auth.audit_query import query_audit_logs
from app.db.base import get_db
from app.services.org_audit_view import member_id_for_uuid
from app.services.org_audit_view import org_member_user_ids
from app.services.org_audit_view import with_display_fields
from app.services.request_context import RequestContext

logger = logging.getLogger(__name__)

router = APIRouter()


def org_id_of(ctx: RequestContext) -> int:
    """The caller's organization id; ``require_org_admin`` guarantees it, this makes it a type."""
    if ctx.org_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization required")
    return int(ctx.org_id)


_NOT_A_MEMBER = "Requested user is not a member of your organization"
_TARGET_NOT_A_MEMBER = "Requested target user is not a member of your organization"


def _member_filter(
    db: Session,
    ctx: RequestContext,
    member_ids: list[int],
    *,
    by_id: int | None,
    by_uuid: UUID | None,
    detail: str,
) -> int | None:
    """Resolve a user filter given as an id or a uuid to a member's id, or refuse with 403."""
    if by_uuid is not None:
        resolved = member_id_for_uuid(db, org_id_of(ctx), by_uuid)
        if resolved is None or (by_id is not None and by_id != resolved):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        return resolved
    if by_id is not None and by_id not in member_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return by_id


def org_audit_query(
    db: Session,
    ctx: RequestContext,
    *,
    start_date: datetime | None,
    end_date: datetime | None,
    event_type: str | None,
    user_id: int | None,
    user_uuid: UUID | None,
    target_user_id: int | None,
    target_user_uuid: UUID | None,
    outcome: str | None,
    resource_type: str | None,
    resource_uuid: UUID | None,
) -> dict[str, Any]:
    """The keyword arguments of ``query_audit_logs`` for the caller's organization.

    The ONE place the tenant scope and the member guards are applied, so the list and the
    export cannot disagree about what an organization admin may read. A user filter naming
    someone outside the organization is refused (403) before any query runs.
    """
    org_id = org_id_of(ctx)
    member_ids = org_member_user_ids(db, org_id)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "event_type": event_type,
        "user_id": _member_filter(
            db, ctx, member_ids, by_id=user_id, by_uuid=user_uuid, detail=_NOT_A_MEMBER
        ),
        "target_user_id": _member_filter(
            db,
            ctx,
            member_ids,
            by_id=target_user_id,
            by_uuid=target_user_uuid,
            detail=_TARGET_NOT_A_MEMBER,
        ),
        "outcome": outcome,
        "resource_type": resource_type,
        "resource_uuid": str(resource_uuid) if resource_uuid is not None else None,
        "scope_user_ids": member_ids,
        "scope_org_id": org_id,
    }


@router.get("/audit-logs")
def get_org_audit_logs(
    start_date: datetime | None = Query(None, description="Start date for log query"),
    end_date: datetime | None = Query(None, description="End date for log query"),
    event_type: str | None = Query(None, description="Filter by event type"),
    user_id: int | None = Query(
        None, description="Filter by user ID — the actor (must be an org member)"
    ),
    user_uuid: UUID | None = Query(
        None, description="Filter by the actor's uuid (must be an org member)"
    ),
    target_user_id: int | None = Query(
        None,
        description=("Filter by the affected/erased subject's user ID (must be an org member)"),
    ),
    target_user_uuid: UUID | None = Query(
        None, description="Filter by the subject's uuid (must be an org member)"
    ),
    outcome: str | None = Query(None, description="Filter by outcome"),
    resource_type: str | None = Query(
        None, max_length=64, description="Filter by resource type (media_file, collection, ...)"
    ),
    resource_uuid: UUID | None = Query(
        None, description="Filter by the resource's uuid: everything that happened to one file"
    ),
    limit: int = Query(default=100, le=1000, description="Maximum results"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> dict[str, Any]:
    """Read audit events for the caller's OWN organization (org-admin only).

    Visibility (issue #262a): events STAMPED with this org's id — including
    events with ``user_id`` NULL, e.g. failed logins, which become visible to
    the org once stamped — plus **legacy** un-stamped events attributed via the
    org's member user-ids (the pre-v372 rule, kept so history written before
    org stamping stays readable; see ``build_org_scope_clause`` for the
    legacy-window caveat about shared members). Events stamped with another
    org are never visible. The ``user_id`` filter, if given, is constrained to
    a member of the org (403 otherwise) so it can't be used to probe outside
    the tenant. ``target_user_id`` (the subject — e.g. the erased user in a GDPR
    erasure, issue #443/#828) gets the identical membership guard: without it,
    an org-admin could enumerate erasure/action history for a user outside
    their own org merely by supplying an ID and reading whether any rows came
    back, even though the org-scope clause on the query itself already narrows
    the result set to nothing for a non-member — the guard exists so that
    probe fails loudly (403) rather than silently (empty list indistinguishable
    from "no such events"). ``user_uuid`` / ``target_user_uuid`` are the same
    filters by uuid, which is what the SPA holds.

    Each event gains ``actor_display``, ``target_display`` and ``resource_display``, resolved at
    read time and only inside this organization (see ``services/org_audit_view``); an id that
    cannot be resolved there is ``null``, never another tenant's label.
    """
    result = query_audit_logs(
        **org_audit_query(
            db,
            ctx,
            start_date=start_date,
            end_date=end_date,
            event_type=event_type,
            user_id=user_id,
            user_uuid=user_uuid,
            target_user_id=target_user_id,
            target_user_uuid=target_user_uuid,
            outcome=outcome,
            resource_type=resource_type,
            resource_uuid=resource_uuid,
        ),
        limit=limit,
        offset=offset,
    )
    if result.get("logs"):
        result = {
            **result,
            "logs": with_display_fields(db, org_id_of(ctx), int(ctx.user.id), result["logs"]),
        }
    return result
