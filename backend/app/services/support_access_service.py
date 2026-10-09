"""Support-access grants: status, request-time resolution and the use log (issue #1122).

Split by concern: this module owns what a grant IS (computed status), how a request proves
it holds one (``resolve_active_grant``) and the fail-closed use log (``record_use``).
Creating and deciding grants is ``support_access_lifecycle``; listing is
``support_access_queries``.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.core.constants import SUPPORT_ACCESS_PENDING_EXPIRY_HOURS
from app.models.organization import Organization
from app.models.support_access import SupportAccessGrant
from app.models.support_access import SupportAccessUse
from app.models.user import User
from app.services.platform_bypass import SupportGrantView

logger = logging.getLogger(__name__)

AUDIT_UNAVAILABLE = "support_access_audit_unavailable"


def coded_error(http_status: int, code: str, message: str) -> HTTPException:
    """An ``HTTPException`` in the repo's coded-error shape (``detail.code``)."""
    return HTTPException(status_code=http_status, detail={"code": code, "message": message})


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def pending_expires_at(grant: SupportAccessGrant) -> datetime:
    requested = _aware(grant.requested_at)
    assert requested is not None
    return requested + timedelta(hours=SUPPORT_ACCESS_PENDING_EXPIRY_HOURS)


def compute_status(grant: SupportAccessGrant, now: datetime | None = None) -> str:
    """The grant's status. Computed from its timestamps, never stored.

    ``revoked`` wins over everything. A grant is *approved-like* when a tenant approved it or
    it is a break-glass opening (which needs no approval); approved-like grants are
    ``active`` inside ``[starts_at, expires_at)`` and ``expired`` after. An undecided
    request is ``pending`` for 72 hours and ``lapsed`` after.
    """
    now = now or datetime.now(UTC)
    if grant.revoked_at is not None:
        return "revoked"
    if grant.decision == "denied":
        return "denied"
    starts, expires = _aware(grant.starts_at), _aware(grant.expires_at)
    if (grant.decision == "approved" or grant.grant_mode == "break_glass") and expires is not None:
        if now >= expires:
            return "expired"
        if starts is not None and starts <= now:
            return "active"
        return "pending"
    return "lapsed" if now >= pending_expires_at(grant) else "pending"


def _target_is_usable(db: Session, grant: SupportAccessGrant) -> bool:
    """The grant's target still exists and is active (SET NULL leaves it permanently dead)."""
    if grant.target_kind == "organization":
        if grant.organization_id is None:
            return False
        org = db.get(Organization, grant.organization_id)
        return org is not None and bool(org.is_active)
    if grant.subject_user_id is None:
        return False
    subject = db.get(User, grant.subject_user_id)
    return subject is not None and bool(subject.is_active)


def resolve_active_grant(db: Session, grant_uuid: str, user: User) -> SupportGrantView:
    """Validate the grant a request carries, or raise a coded 403.

    Re-read on every request, never cached, so revocation, expiry, demotion and organization
    deactivation take effect on the next request.

    Raises:
        HTTPException: 403 with ``detail.code`` of ``support_grant_invalid`` (unknown uuid,
            wrong grantee, grantee no longer an active admin, dead target),
            ``support_grant_not_active`` (pending, denied, lapsed), ``support_grant_expired``
            or ``support_grant_revoked``.
    """
    invalid = coded_error(403, "support_grant_invalid", "Support access grant is not valid.")
    try:
        parsed = uuid_pkg.UUID(grant_uuid)
    except (ValueError, AttributeError, TypeError):
        raise invalid from None
    grant = db.query(SupportAccessGrant).filter(SupportAccessGrant.uuid == parsed).first()
    if grant is None or grant.grantee_user_id != user.id or not user.is_admin or not user.is_active:
        raise invalid
    state = compute_status(grant)
    if state == "revoked":
        raise coded_error(403, "support_grant_revoked", "Support access grant was revoked.")
    if state == "expired":
        raise coded_error(403, "support_grant_expired", "Support access grant has expired.")
    if state != "active":
        raise coded_error(403, "support_grant_not_active", "Support access grant is not active.")
    if not _target_is_usable(db, grant):
        raise invalid
    expires = _aware(grant.expires_at)
    assert expires is not None
    return SupportGrantView(
        id=int(grant.id),
        uuid=str(grant.uuid),
        organization_id=grant.organization_id,
        subject_user_id=grant.subject_user_id,
        access_level="write" if grant.access_level == "write" else "read",
        grant_mode="break_glass" if grant.grant_mode == "break_glass" else "approved",
        expires_at=expires,
    )


def record_use(
    grant: SupportGrantView,
    *,
    actor_user_id: int | None,
    method: str,
    route: str,
    resource_type: str | None,
    resource_uuid: str | None,
    need: str | None,
    organization_id: int | None,
    owner_user_id: int | None,
) -> None:
    """Write one use row in its OWN short session and mirror it to the audit stream.

    Fail-closed: if the row cannot be written the request gets 503 and is never served
    unrecorded. The audit-stream mirror is best-effort (it is the SIEM feed); this table is
    the record a tenant can rely on. Own session so the row survives a request that later
    rolls back.
    """
    from app.db.session_utils import session_scope

    try:
        with session_scope() as session:
            session.add(
                SupportAccessUse(
                    grant_id=grant.id,
                    method=method[:10],
                    route=route[:255],
                    resource_type=resource_type,
                    resource_uuid=uuid_pkg.UUID(resource_uuid) if resource_uuid else None,
                    need=need,
                    organization_id=organization_id,
                    owner_user_id=owner_user_id,
                )
            )
    except Exception:
        logger.exception("Support-access use row could not be written; refusing the request")
        raise coded_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            AUDIT_UNAVAILABLE,
            "Support access could not be recorded; the request was not served.",
        ) from None

    from app.auth.audit import AuditEventType
    from app.auth.audit import AuditOutcome
    from app.auth.audit import audit_logger

    audit_logger.log(
        AuditEventType.SUPPORT_ACCESS_USED,
        AuditOutcome.SUCCESS,
        user_id=actor_user_id,
        organization_id=organization_id,
        target_user_id=owner_user_id,
        details={
            "grant_uuid": grant.uuid,
            "access_level": grant.access_level,
            "route": route,
            "resource_type": resource_type,
            "resource_uuid": resource_uuid,
            "need": need,
        },
    )
