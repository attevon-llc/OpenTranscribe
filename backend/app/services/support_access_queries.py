"""Reading support-access grants: serialization, filtered lists and the use log (#1122)."""

from __future__ import annotations

import uuid as uuid_pkg
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.constants import SUPPORT_ACCESS_PENDING_EXPIRY_HOURS
from app.models.organization import Organization
from app.models.support_access import SupportAccessGrant
from app.models.support_access import SupportAccessUse
from app.models.user import User
from app.schemas.support_access import GrantPage
from app.schemas.support_access import OrgRef
from app.schemas.support_access import SupportGrantOut
from app.schemas.support_access import SupportGrantUseOut
from app.schemas.support_access import UsePage
from app.schemas.support_access import UserRef
from app.services.support_access_service import compute_status
from app.services.support_access_service import pending_expires_at

STATUSES = ("pending", "active", "denied", "expired", "revoked", "lapsed")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)


def _user_ref(user: User | None) -> UserRef | None:
    if user is None:
        return None
    return UserRef(uuid=str(user.uuid), full_name=user.full_name, email=str(user.email))


def _user_by_id(users: dict[int, User], user_id: int | None) -> UserRef | None:
    return _user_ref(users.get(user_id)) if user_id is not None else None


def _org_ref(org: Organization | None) -> OrgRef | None:
    if org is None:
        return None
    return OrgRef(uuid=str(org.uuid), name=str(org.name), slug=org.slug)


def to_outs(db: Session, grants: Iterable[SupportAccessGrant]) -> list[SupportGrantOut]:
    """Serialize grants with their related users and organizations loaded in two queries."""
    rows = list(grants)
    user_ids = {
        uid
        for g in rows
        for uid in (
            g.grantee_user_id,
            g.subject_user_id,
            g.decided_by_user_id,
            g.revoked_by_user_id,
        )
        if uid is not None
    }
    org_ids = {g.organization_id for g in rows if g.organization_id is not None}
    users = (
        {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    )
    orgs = (
        {o.id: o for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()}
        if org_ids
        else {}
    )
    now = datetime.now(UTC)
    out: list[SupportGrantOut] = []
    for g in rows:
        state = compute_status(g, now)
        # model_validate (not keyword construction): the Literal fields are checked at
        # runtime, which is the right place to catch a stray value in a CHECK-constrained column.
        out.append(
            SupportGrantOut.model_validate(
                {
                    "uuid": str(g.uuid),
                    "status": state,
                    "target_kind": g.target_kind,
                    "grant_mode": g.grant_mode,
                    "access_level": g.access_level,
                    "organization": _org_ref(orgs.get(g.organization_id))
                    if g.organization_id
                    else None,
                    "subject_user": _user_by_id(users, g.subject_user_id),
                    "grantee": _user_by_id(users, g.grantee_user_id),
                    "reason": g.reason,
                    "ticket_ref": g.ticket_ref,
                    "requested_duration_minutes": g.requested_duration_minutes,
                    "requested_at": _utc(g.requested_at),
                    "pending_expires_at": _utc(pending_expires_at(g))
                    if state == "pending"
                    else None,
                    "decided_by": _user_by_id(users, g.decided_by_user_id),
                    "decided_at": _utc(g.decided_at),
                    "starts_at": _utc(g.starts_at),
                    "expires_at": _utc(g.expires_at),
                    "revoked_by": _user_by_id(users, g.revoked_by_user_id),
                    "revoked_at": _utc(g.revoked_at),
                }
            )
        )
    return out


def to_out(db: Session, grant: SupportAccessGrant) -> SupportGrantOut:
    return to_outs(db, [grant])[0]


def _status_clause(state: str, now: datetime):
    """SQL twin of ``compute_status``; the list filter runs in the database."""
    g = SupportAccessGrant
    approved_like = or_(g.decision == "approved", g.grant_mode == "break_glass")
    cutoff = now - timedelta(hours=SUPPORT_ACCESS_PENDING_EXPIRY_HOURS)
    live = g.revoked_at.is_(None)
    undecided = and_(live, g.decision.is_(None), g.grant_mode == "approved")
    if state == "revoked":
        return g.revoked_at.is_not(None)
    if state == "denied":
        return and_(live, g.decision == "denied")
    if state == "active":
        return and_(live, approved_like, g.starts_at <= now, g.expires_at > now)
    if state == "expired":
        return and_(live, approved_like, g.expires_at <= now)
    if state == "lapsed":
        return and_(undecided, g.requested_at <= cutoff)
    return and_(undecided, g.requested_at > cutoff)  # pending


def list_grants(
    db: Session,
    *,
    where: list,
    status_filter: str | None,
    limit: int,
    offset: int,
) -> GrantPage:
    """Newest-first page of grants matching ``where``, optionally filtered by status."""
    now = datetime.now(UTC)
    query = db.query(SupportAccessGrant).filter(*where)
    if status_filter:
        query = query.filter(_status_clause(status_filter, now))
    total = query.count()
    rows = (
        query.order_by(SupportAccessGrant.requested_at.desc(), SupportAccessGrant.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return GrantPage(items=to_outs(db, rows), total=total, server_time=now)


def list_uses(db: Session, grant: SupportAccessGrant, *, limit: int, offset: int) -> UsePage:
    """Newest-first page of the grant's use rows."""
    query = db.query(SupportAccessUse).filter(SupportAccessUse.grant_id == grant.id)
    total = query.count()
    rows = (
        query.order_by(SupportAccessUse.occurred_at.desc(), SupportAccessUse.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [
        SupportGrantUseOut.model_validate(
            {
                "occurred_at": _utc(u.occurred_at),
                "method": u.method,
                "route": u.route,
                "resource_type": u.resource_type,
                "resource_uuid": str(u.resource_uuid) if u.resource_uuid else None,
                "need": u.need,
            }
        )
        for u in rows
    ]
    return UsePage(items=items, total=total, server_time=datetime.now(UTC))


def find_grant(db: Session, grant_uuid: str) -> SupportAccessGrant | None:
    """The grant with this uuid, or ``None`` for an unknown or malformed uuid."""
    try:
        parsed = uuid_pkg.UUID(grant_uuid)
    except (ValueError, AttributeError, TypeError):
        return None
    return db.query(SupportAccessGrant).filter(SupportAccessGrant.uuid == parsed).first()
