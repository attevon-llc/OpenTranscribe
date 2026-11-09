"""``/org-admin/gdpr``: scheduled, re-authenticated, cancellable organization erasure (#1105).

* ``POST /gdpr/erase-user/{user_uuid}``: schedule the erasure of one member's data in the
  caller's organization. **202** with the request receipt; nothing is destroyed now.
* ``POST /gdpr/erase-organization?confirm=true``: schedule the erasure of the whole organization
  (the organization's OWNER only, v468).
* ``GET  /gdpr/erasures``: the organization's open scheduled erasures.
* ``POST /gdpr/erasures/{ledger_uuid}/cancel``: withdraw one before it runs. Any organization
  admin may cancel: more people able to stop a destructive act is the safe direction.

Both requests need the caller to re-authenticate in the body (password or current TOTP code, the
same rule as the account holder's own erasure request) and wait
``gdpr.org_erasure_delay_hours`` / ``gdpr.org_member_erasure_delay_hours`` (default 72 h) before the
executor runs them. There is no immediate option here; a platform super administrator's routes
are unchanged.

Gated like the rest of ``/org-admin``: the ``organizations`` capability (404 in the community
edition), ``require_org_admin`` on every route (``require_org_owner`` on the organization erasure), and refused under a support access grant.
"""

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import require_org_admin
from app.api.deps_context import require_org_owner
from app.api.endpoints.org_admin import org_id_of
from app.api.rate_limit import get_auth_rate_limit
from app.api.rate_limit import limiter
from app.api.rate_limit import user_or_ip_key
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.models.erasure import ErasureLedgerEntry
from app.models.user import User
from app.schemas.scheduled_erasure import OrgErasureConfirm
from app.schemas.scheduled_erasure import ScheduledErasureItem
from app.schemas.scheduled_erasure import ScheduledErasureReceipt
from app.services import scheduled_erasure_service as scheduled
from app.services.reauth import can_reauthenticate
from app.services.reauth import verify_reauth
from app.services.request_context import RequestContext
from app.services.support_access_service import coded_error

router = APIRouter()

_NOT_FOUND = "Scheduled erasure not found"


def _receipt(entry: ErasureLedgerEntry) -> ScheduledErasureReceipt:
    # model_validate checks subject_type against the schema's Literal at run time.
    return ScheduledErasureReceipt.model_validate(
        {
            "ledger_uuid": entry.uuid,
            "subject_type": entry.subject_type,
            "subject_user_uuid": entry.subject_user_uuid,
            "requested_at": entry.requested_at,
            "scheduled_for": entry.scheduled_for,
            "sla_due_at": entry.sla_due_at,
            "request_digest": entry.request_digest,
            "status": "cancelled" if entry.status == "cancelled" else "scheduled",
            "cancelled_at": entry.cancelled_at,
        }
    )


def _raise(error: scheduled.ScheduledErasureError) -> NoReturn:
    http = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if error.code == "ledger_unavailable"
        else status.HTTP_409_CONFLICT
    )
    raise coded_error(http, error.code, error.message)


def _require_reauth(db: Session, user: User, body: OrgErasureConfirm | None) -> None:
    """409 ``reauth_unavailable`` / 403 ``reauth_required``, as the self-service request."""
    if not can_reauthenticate(db, user):
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "reauth_unavailable",
            "This account cannot confirm a request here. Ask the operator of this deployment.",
        )
    password = body.password.get_secret_value() if body and body.password else None
    mfa_code = body.mfa_code if body else None
    if not verify_reauth(db, user, password=password, mfa_code=mfa_code):
        raise coded_error(
            status.HTTP_403_FORBIDDEN, "reauth_required", "The password or code was not accepted."
        )


@router.post(
    "/gdpr/erase-user/{user_uuid}",
    response_model=ScheduledErasureReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.shared_limit(get_auth_rate_limit(), scope="org-erase-member", key_func=user_or_ip_key)
def erase_org_member(
    request: Request,
    response: Response,  # required by slowapi
    user_uuid: str,
    body: OrgErasureConfirm | None = None,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> ScheduledErasureReceipt:
    """Schedule the GDPR Art. 17 erasure of a member's data WITHIN the caller's organization.

    The target must be a member of the caller's org (403 otherwise). When it runs, only the
    target's rows stamped with THIS org are destroyed; their account, personal-scope data and
    other organizations' data survive (an org admin has authority over the tenant's data, never
    over the person's account). Answers 202 with the request receipt; the erasure runs once
    ``scheduled_for`` has passed unless an organization admin cancels it first.
    """
    org_id = org_id_of(ctx)
    try:
        target = scheduled.member_of_organization(db, org_id, user_uuid)
    except scheduled.ScheduledErasureError as error:
        if error.code == "user_not_found":
            raise HTTPException(status_code=404, detail="User not found") from error
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of your organization",
        ) from error
    _require_reauth(db, ctx.user, body)
    try:
        entry = scheduled.schedule_for_organization(
            db,
            organization_id=org_id,
            subject_type="org_member",
            actor=ctx.user,
            subject_user=target,
            client=get_request_context(request),
        )
    except scheduled.ScheduledErasureError as error:
        _raise(error)
    return _receipt(entry)


@router.post(
    "/gdpr/erase-organization",
    response_model=ScheduledErasureReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(get_auth_rate_limit(), key_func=user_or_ip_key)
def erase_own_organization(
    request: Request,
    response: Response,  # required by slowapi
    confirm: bool = Query(False, description="Must be true to schedule the irreversible erasure"),
    body: OrgErasureConfirm | None = None,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_owner),
) -> ScheduledErasureReceipt:
    """Schedule the GDPR erasure of the caller's ENTIRE organization (irreversible once it runs).

    When it runs: org-stamped media, member-owned org data, the org's voiceprints and the
    organization row itself. Members keep their personal accounts. Owner only: 403
    ``org_owner_only`` for another admin, 409 ``org_owner_required`` while the organization has no
    owner. Requires ``confirm=true`` and re-authentication; answers 202 with the request receipt.
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pass confirm=true to schedule organization erasure (irreversible)",
        )
    org_id = org_id_of(ctx)
    _require_reauth(db, ctx.user, body)
    try:
        entry = scheduled.schedule_for_organization(
            db,
            organization_id=org_id,
            subject_type="organization",
            actor=ctx.user,
            client=get_request_context(request),
        )
    except scheduled.ScheduledErasureError as error:
        _raise(error)
    return _receipt(entry)


@router.get("/gdpr/erasures", response_model=list[ScheduledErasureItem])
def list_scheduled_erasures(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> list[ScheduledErasureItem]:
    """The caller's organization's open scheduled erasures, soonest first.

    ``requested_by`` is resolved now, and only while the requester is still a member of this
    organization: the ledger keeps no names.
    """
    org_id = org_id_of(ctx)
    entries = scheduled.open_entries_for_organization(db, org_id)
    names = scheduled.requester_names(db, org_id, entries)
    return [
        ScheduledErasureItem(
            **_receipt(e).model_dump(),
            requested_by=names.get(int(e.actor_user_id)) if e.actor_user_id else None,
        )
        for e in entries
    ]


@router.post("/gdpr/erasures/{ledger_uuid}/cancel", response_model=ScheduledErasureReceipt)
def cancel_scheduled_erasure(
    request: Request,
    ledger_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> ScheduledErasureReceipt:
    """Withdraw a scheduled erasure of this organization before it runs (409 once it has).

    Another organization's entry, a self-service request and an unknown uuid are the same 404.
    """
    entry = scheduled.find_organization_entry(db, org_id_of(ctx), ledger_uuid)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    try:
        entry = scheduled.cancel(db, entry, actor=ctx.user, client=get_request_context(request))
    except scheduled.ScheduledErasureError as error:
        _raise(error)
    return _receipt(entry)
