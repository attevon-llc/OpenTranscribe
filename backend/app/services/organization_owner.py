"""Who owns an organization (v468): read it, name it, hand it over.

The owner is the one organization administrator who may schedule the erasure of the whole
organization. ``set_owner`` is the single write path: the transfer route calls it for the owner's
own hand-over, and a deployment operator (the managed edition) calls it to name an owner when
there is none, so the rule "the owner is an active ``org:admin`` of this organization" lives in
one place.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.auth.audit import AuditOutcome
from app.auth.audit_events import TENANT_SCOPE_ORG
from app.auth.audit_events import AuditEventType
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.user import User
from app.services import audit_outbox


class OwnerError(Exception):
    """A refusal with a stable ``code``: ``not_the_owner``, ``not_an_org_admin``, ``already_owner``,
    ``organization_not_found``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def owner_user_id(db: Session, organization_id: int) -> int | None:
    """The organization's owner, or None when it has none."""
    owner: int | None = (
        db.query(Organization.owner_user_id).filter(Organization.id == organization_id).scalar()
    )
    return owner


def owner_user_uuid(db: Session, organization_id: int) -> UUID:
    """The owner's public uuid; the caller has just set it, so it exists."""
    owner: UUID | None = (
        db.query(User.uuid)
        .join(Organization, Organization.owner_user_id == User.id)
        .filter(Organization.id == organization_id)
        .scalar()
    )
    assert owner is not None
    return owner


def _admin_membership(db: Session, organization_id: int, user_id: int) -> bool:
    return (
        db.query(OrganizationMembership.id)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.role == "org:admin",
        )
        .first()
        is not None
    )


def set_owner(
    db: Session,
    *,
    organization_id: int,
    new_owner: User,
    actor: User | None,
    client: dict[str, Any] | None = None,
) -> Organization:
    """Make ``new_owner`` the organization's owner and record ``org.owner.change``, in one commit.

    ``actor`` is the owner handing over, or None for the operator's own call.

    Raises:
        OwnerError: ``organization_not_found``; ``not_an_org_admin`` (the target must be an
            ``org:admin`` of THIS organization: a member of another organization, or a plain
            member, is refused); ``already_owner``. Nothing is written on a refusal.
    """
    org = (
        db.query(Organization).filter(Organization.id == organization_id).with_for_update().first()
    )
    if org is None:
        raise OwnerError("organization_not_found", "Organization not found.")
    if not _admin_membership(db, organization_id, int(new_owner.id)):
        raise OwnerError(
            "not_an_org_admin", "The new owner must be an administrator of this organization."
        )
    previous_id = org.owner_user_id
    if previous_id == new_owner.id:
        raise OwnerError("already_owner", "That person already owns this organization.")
    previous_uuid = (
        db.query(User.uuid).filter(User.id == previous_id).scalar() if previous_id else None
    )
    org.owner_user_id = int(new_owner.id)
    ctx = client or {}
    audit_outbox.stage(
        db,
        event_type=AuditEventType.ORG_OWNER_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=int(actor.id) if actor is not None else None,
        source_ip=ctx.get("source_ip"),
        user_agent=ctx.get("user_agent"),
        organization_id=organization_id,
        target_user_id=int(new_owner.id),
        resource_type="organization",
        resource_uuid=str(org.uuid),
        tenant_scope=TENANT_SCOPE_ORG,
        details={
            "previous_owner_uuid": str(previous_uuid) if previous_uuid else None,
            "new_owner_uuid": str(new_owner.uuid),
            "actor_kind": "owner" if actor is not None else "operator",
        },
    )
    db.commit()
    audit_outbox.deliver_staged(db)
    return org


def transfer(
    db: Session,
    *,
    organization_id: int,
    actor: User,
    to_user_uuid: UUID,
    client: dict[str, Any] | None = None,
) -> Organization:
    """The CURRENT owner hands the organization to another of its administrators.

    Raises:
        OwnerError: ``not_the_owner`` when ``actor`` is not the owner now (re-read under the row
            lock, not trusted from the request context), ``not_an_org_admin`` for an unknown
            user, a plain member or a member of another organization, plus ``set_owner``'s.
    """
    if owner_user_id(db, organization_id) != actor.id:
        raise OwnerError("not_the_owner", "Only the owner of the organization can do this.")
    target = db.query(User).filter(User.uuid == to_user_uuid).first()
    if target is None:
        # Same answer as a non-admin: do not reveal which accounts exist.
        raise OwnerError(
            "not_an_org_admin", "The new owner must be an administrator of this organization."
        )
    return set_owner(
        db, organization_id=organization_id, new_owner=target, actor=actor, client=client
    )
