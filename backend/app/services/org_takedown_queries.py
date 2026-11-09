"""Queries behind the organization administrator's takedown screens."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from app.models.media import MediaFile
from app.models.organization import OrganizationMembership
from app.models.user import User


def find_org_file(db: Session, file_uuid: UUID, org_id: int) -> MediaFile | None:
    """The file with this uuid that belongs to organization ``org_id``, or ``None``."""
    return (
        db.query(MediaFile)
        .filter(MediaFile.uuid == file_uuid, MediaFile.organization_id == org_id)
        .first()
    )


def list_taken_down_files(
    db: Session, org_id: int, *, include_legal_holds: bool, offset: int, limit: int
) -> tuple[list[tuple], int]:
    """A page of the organization's taken-down files, newest first, and the total.

    Each row is ``(file, owner, actor, owner_membership_id, actor_membership_id)``; the two
    membership ids are ``None`` once that person is no longer a member of the organization.
    """
    condition: ColumnElement[bool] = MediaFile.is_quarantined.is_(True)
    if include_legal_holds:
        condition = or_(condition, MediaFile.legal_hold.is_(True))
    scoped = (MediaFile.organization_id == org_id, condition)

    total = db.query(func.count(MediaFile.id)).filter(*scoped).scalar() or 0

    owner = aliased(User)
    actor = aliased(User)
    owner_member = aliased(OrganizationMembership)
    actor_member = aliased(OrganizationMembership)
    rows = (
        db.query(MediaFile, owner, actor, owner_member.id, actor_member.id)
        .filter(*scoped)
        .outerjoin(owner, owner.id == MediaFile.user_id)
        .outerjoin(actor, actor.id == MediaFile.quarantined_by)
        .outerjoin(
            owner_member,
            (owner_member.user_id == MediaFile.user_id) & (owner_member.organization_id == org_id),
        )
        .outerjoin(
            actor_member,
            (actor_member.user_id == MediaFile.quarantined_by)
            & (actor_member.organization_id == org_id),
        )
        .order_by(MediaFile.quarantined_at.desc().nullslast(), MediaFile.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [tuple(r) for r in rows], int(total)
