"""Creating and deciding support-access grants (issue #1122).

Decisions are atomic: approve and deny are one conditional ``UPDATE ... WHERE decision IS
NULL AND revoked_at IS NULL AND requested_at > now() - 72h``. Two approvers (or an approver
racing a revoke) cannot both win; the loser re-reads and gets a coded 409.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.constants import SUPPORT_ACCESS_PENDING_EXPIRY_HOURS
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.support_access import SupportAccessGrant
from app.models.user import User
from app.schemas.support_access import BreakGlassBody
from app.schemas.support_access import CreateGrantBody
from app.services.support_access_service import coded_error
from app.services.support_access_service import compute_status
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

WS_REQUESTED = "support_access_requested"
WS_BREAK_GLASS = "support_access_break_glass"
WS_DECIDED = "support_access_decided"
WS_REVOKED = "support_access_revoked"

INVALID_TARGET = "support_grant_invalid_target"
SELF_APPROVAL = "support_grant_self_approval"


def _send(user_ids: list[int], notification_type: str, data: dict) -> None:
    """Push a lifecycle event to specific users. None of these name a ``MediaFile``."""
    for user_id in user_ids:
        send_ws_event(user_id, notification_type, data)


def _invalid_target(message: str = "Exactly one valid target is required.") -> HTTPException:
    return coded_error(422, INVALID_TARGET, message)


def _resolve_target(
    db: Session,
    organization_uuid: str | None,
    subject_user_uuid: str | None,
    grantee: User,
) -> tuple[Organization | None, User | None]:
    """The grant's target: exactly one active organization, or one active other user."""
    if (organization_uuid is None) == (subject_user_uuid is None):
        raise _invalid_target()
    try:
        if organization_uuid is not None:
            org = (
                db.query(Organization)
                .filter(
                    Organization.uuid == uuid_pkg.UUID(organization_uuid),
                    Organization.is_active.is_(True),
                )
                .first()
            )
            if org is None:
                raise _invalid_target("Organization not found or inactive.")
            return org, None
        assert subject_user_uuid is not None
        subject = db.query(User).filter(User.uuid == uuid_pkg.UUID(subject_user_uuid)).first()
    except ValueError:
        raise _invalid_target() from None
    if subject is None or not subject.is_active:
        raise _invalid_target("User not found or inactive.")
    if subject.id == grantee.id:
        raise _invalid_target("Support access to your own workspace is not available.")
    return None, subject


def _recipients(db: Session, grant: SupportAccessGrant, exclude_user_id: int) -> list[int]:
    """The people entitled to know: the target org's admins, or the subject user."""
    if grant.target_kind == "personal":
        ids = [grant.subject_user_id] if grant.subject_user_id is not None else []
    else:
        rows = (
            db.query(OrganizationMembership.user_id)
            .join(User, User.id == OrganizationMembership.user_id)
            .filter(
                OrganizationMembership.organization_id == grant.organization_id,
                OrganizationMembership.role == "org:admin",
                User.is_active.is_(True),
            )
            .all()
        )
        ids = [int(r[0]) for r in rows]
    return [i for i in ids if i != exclude_user_id]


def _name(user: User | None) -> str:
    return str(user.full_name or user.email) if user is not None else ""


def _new_grant(
    org: Organization | None,
    subject: User | None,
    grantee: User,
    *,
    body: CreateGrantBody | BreakGlassBody,
    mode: str,
) -> SupportAccessGrant:
    return SupportAccessGrant(
        uuid=uuid_pkg.uuid4(),
        target_kind="organization" if org is not None else "personal",
        organization_id=org.id if org is not None else None,
        subject_user_id=subject.id if subject is not None else None,
        grantee_user_id=grantee.id,
        access_level=body.access_level,
        grant_mode=mode,
        reason=body.reason,
        requested_duration_minutes=body.duration_minutes,
        requested_at=datetime.now(UTC),
    )


def request_grant(db: Session, grantee: User, body: CreateGrantBody) -> SupportAccessGrant:
    """An admin asks the tenant for access. The grant is ``pending`` until it is decided."""
    org, subject = _resolve_target(db, body.organization_uuid, body.subject_user_uuid, grantee)
    grant = _new_grant(org, subject, grantee, body=body, mode="approved")
    db.add(grant)
    db.commit()
    db.refresh(grant)

    audit_logger.log(
        AuditEventType.SUPPORT_ACCESS_REQUESTED,
        AuditOutcome.SUCCESS,
        user_id=grantee.id,
        organization_id=grant.organization_id,
        target_user_id=grant.subject_user_id,
        details={
            "grant_uuid": str(grant.uuid),
            "access_level": grant.access_level,
            "requested_duration_minutes": grant.requested_duration_minutes,
        },
    )
    _send(
        _recipients(db, grant, grantee.id),
        WS_REQUESTED,
        {"grant_uuid": str(grant.uuid), "grantee_name": _name(grantee)},
    )
    return grant


def break_glass(db: Session, grantee: User, body: BreakGlassBody) -> SupportAccessGrant:
    """A super_admin opens access immediately; the tenant is told in real time."""
    org, subject = _resolve_target(db, body.organization_uuid, body.subject_user_uuid, grantee)
    grant = _new_grant(org, subject, grantee, body=body, mode="break_glass")
    now = datetime.now(UTC)
    grant.ticket_ref = body.ticket_ref
    grant.starts_at = now
    grant.expires_at = now + timedelta(minutes=body.duration_minutes)
    db.add(grant)
    db.commit()
    db.refresh(grant)

    audit_logger.log(
        AuditEventType.SUPPORT_ACCESS_BREAK_GLASS,
        AuditOutcome.SUCCESS,
        user_id=grantee.id,
        organization_id=grant.organization_id,
        target_user_id=grant.subject_user_id,
        details={
            "grant_uuid": str(grant.uuid),
            "access_level": grant.access_level,
            "ticket_ref": grant.ticket_ref,
            "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        },
    )
    target = org.name if org is not None else _name(subject)
    _send(
        _recipients(db, grant, grantee.id),
        WS_BREAK_GLASS,
        {
            "grant_uuid": str(grant.uuid),
            "grantee_name": _name(grantee),
            "target_name": str(target),
            "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        },
    )
    return grant


def _refuse_if_not_independent(approver: User, grant: SupportAccessGrant, org_route: bool) -> None:
    """Self-approval is refused; so is a platform admin approving another's org request (D2)."""
    if approver.id == grant.grantee_user_id or (org_route and approver.is_admin):
        raise coded_error(
            403, SELF_APPROVAL, "Support access must be decided by the tenant, not by staff."
        )


def _decide(
    db: Session,
    grant: SupportAccessGrant,
    values: dict,
) -> bool:
    """Apply a decision iff the grant is still undecided, unrevoked and unlapsed."""
    cutoff = datetime.now(UTC) - timedelta(hours=SUPPORT_ACCESS_PENDING_EXPIRY_HOURS)
    result = db.execute(
        update(SupportAccessGrant)
        .where(
            SupportAccessGrant.id == grant.id,
            SupportAccessGrant.decision.is_(None),
            SupportAccessGrant.grant_mode == "approved",
            SupportAccessGrant.revoked_at.is_(None),
            SupportAccessGrant.requested_at > cutoff,
        )
        .values(**values)
        .returning(SupportAccessGrant.id)
    )
    won = result.first() is not None
    db.commit()
    db.expire(grant)
    return won


def _raise_lost_decision(grant: SupportAccessGrant) -> None:
    if compute_status(grant) == "lapsed":
        raise coded_error(
            409, "support_grant_lapsed", "This request lapsed and can no longer be decided."
        )
    raise coded_error(
        409, "support_grant_already_decided", "This request has already been decided."
    )


def approve(
    db: Session,
    approver: User,
    grant: SupportAccessGrant,
    *,
    duration_minutes: int | None,
    org_route: bool,
    note: str | None = None,
) -> SupportAccessGrant:
    """Approve a pending request. The approver may shorten the duration, never extend it."""
    _refuse_if_not_independent(approver, grant, org_route)
    requested = int(grant.requested_duration_minutes)
    if duration_minutes is not None and duration_minutes > requested:
        raise coded_error(
            422,
            "support_grant_duration_exceeds_request",
            "An approver may shorten the requested duration, never extend it.",
        )
    minutes = duration_minutes or requested
    now = datetime.now(UTC)
    won = _decide(
        db,
        grant,
        {
            "decision": "approved",
            "decided_at": now,
            "decided_by_user_id": approver.id,
            "starts_at": now,
            "expires_at": now + timedelta(minutes=minutes),
        },
    )
    if not won:
        _raise_lost_decision(grant)
    audit_logger.log(
        AuditEventType.SUPPORT_ACCESS_APPROVED,
        AuditOutcome.SUCCESS,
        user_id=approver.id,
        organization_id=grant.organization_id,
        target_user_id=grant.grantee_user_id,
        details={
            "grant_uuid": str(grant.uuid),
            "access_level": grant.access_level,
            "duration_minutes": minutes,
            **({"note": note} if note else {}),
        },
    )
    _notify_grantee(grant, WS_DECIDED)
    return grant


def deny(
    db: Session,
    approver: User,
    grant: SupportAccessGrant,
    *,
    org_route: bool,
    note: str | None = None,
) -> SupportAccessGrant:
    """Deny a pending request."""
    _refuse_if_not_independent(approver, grant, org_route)
    won = _decide(
        db,
        grant,
        {
            "decision": "denied",
            "decided_at": datetime.now(UTC),
            "decided_by_user_id": approver.id,
        },
    )
    if not won:
        _raise_lost_decision(grant)
    audit_logger.log(
        AuditEventType.SUPPORT_ACCESS_DENIED,
        AuditOutcome.SUCCESS,
        user_id=approver.id,
        organization_id=grant.organization_id,
        target_user_id=grant.grantee_user_id,
        details={"grant_uuid": str(grant.uuid), **({"note": note} if note else {})},
    )
    _notify_grantee(grant, WS_DECIDED)
    return grant


def revoke(
    db: Session, actor: User, grant: SupportAccessGrant, *, note: str | None = None
) -> SupportAccessGrant:
    """End a pending or active grant now. Idempotent on a grant that is already over."""
    if compute_status(grant) not in ("pending", "active"):
        return grant
    result = db.execute(
        update(SupportAccessGrant)
        .where(SupportAccessGrant.id == grant.id, SupportAccessGrant.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_by_user_id=actor.id)
        .returning(SupportAccessGrant.id)
    )
    won = result.first() is not None
    db.commit()
    db.expire(grant)
    if not won:
        return grant
    audit_logger.log(
        AuditEventType.SUPPORT_ACCESS_REVOKED,
        AuditOutcome.SUCCESS,
        user_id=actor.id,
        organization_id=grant.organization_id,
        target_user_id=grant.grantee_user_id,
        details={"grant_uuid": str(grant.uuid), **({"note": note} if note else {})},
    )
    _notify_grantee(grant, WS_REVOKED)
    return grant


def _notify_grantee(grant: SupportAccessGrant, notification_type: str) -> None:
    if grant.grantee_user_id is None:
        return
    _send(
        [int(grant.grantee_user_id)],
        notification_type,
        {"grant_uuid": str(grant.uuid), "status": compute_status(grant)},
    )
