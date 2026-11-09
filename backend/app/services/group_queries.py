"""Read queries behind the user-group endpoints."""

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.models.group import UserGroup
from app.models.group import UserGroupMember


def get_membership(db: Session, group_id: int, user_id: int) -> UserGroupMember | None:
    """A user's membership row in a group, or None."""
    result: UserGroupMember | None = (
        db.query(UserGroupMember)
        .filter(
            UserGroupMember.group_id == group_id,
            UserGroupMember.user_id == user_id,
        )
        .first()
    )
    return result


def count_members(db: Session, group_id: int) -> int:
    """Number of members in a group."""
    return db.query(UserGroupMember).filter(UserGroupMember.group_id == group_id).count()


def list_groups_for_member(
    db: Session, user_id: int, *, organization_id: int | None
) -> list[UserGroup]:
    """Groups the user belongs to in one tenant, owners eagerly loaded.

    ``organization_id`` None selects personal-scope groups, otherwise the groups of that org.
    """
    tenant_term = (
        UserGroup.organization_id == organization_id
        if organization_id is not None
        else UserGroup.organization_id.is_(None)
    )
    return (
        db.query(UserGroup)
        .join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
        .options(joinedload(UserGroup.owner))
        .filter(UserGroupMember.user_id == user_id, tenant_term)
        .all()
    )


def member_counts_by_group(db: Session, group_ids: list[int]) -> dict[int, int]:
    """``{group_id: member_count}`` for the given groups."""
    rows = (
        db.query(UserGroupMember.group_id, func.count(UserGroupMember.id))
        .filter(UserGroupMember.group_id.in_(group_ids))
        .group_by(UserGroupMember.group_id)
        .all()
    )
    return {gid: cnt for gid, cnt in rows}


def roles_by_group(db: Session, group_ids: list[int], user_id: int) -> dict[int, str]:
    """``{group_id: role}`` for the user's memberships among the given groups."""
    rows = (
        db.query(UserGroupMember.group_id, UserGroupMember.role)
        .filter(
            UserGroupMember.group_id.in_(group_ids),
            UserGroupMember.user_id == user_id,
        )
        .all()
    )
    return {gid: role for gid, role in rows}


def group_name_taken(
    db: Session, owner_id: int, name: str, *, exclude_group_id: int | None = None
) -> bool:
    """Whether the owner already has a group called ``name`` (optionally ignoring one group)."""
    query = db.query(UserGroup).filter(UserGroup.owner_id == owner_id, UserGroup.name == name)
    if exclude_group_id is not None:
        query = query.filter(UserGroup.id != exclude_group_id)
    return query.first() is not None


def list_members_with_users(db: Session, group_id: int) -> list[UserGroupMember]:
    """All members of a group with their users eagerly loaded."""
    return (
        db.query(UserGroupMember)
        .options(joinedload(UserGroupMember.user))
        .filter(UserGroupMember.group_id == group_id)
        .all()
    )
