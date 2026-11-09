"""Reads behind the SCIM ``/Users`` and ``/Groups`` routes. None of these commit."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.user import User


def get_group_by_uuid(db: Session, group_uuid: str) -> UserGroup | None:
    """The group with this public UUID, or ``None``."""
    return db.query(UserGroup).filter(UserGroup.uuid == group_uuid).first()


def get_group_members(db: Session, group_id: int) -> list[User]:
    """Every account in the group, whatever wrote the membership row."""
    return (
        db.query(User)
        .join(UserGroupMember, UserGroupMember.user_id == User.id)
        .filter(UserGroupMember.group_id == group_id)
        .all()
    )


def get_groups_of_user(db: Session, user_id: int) -> list[UserGroup]:
    """Every group the account belongs to."""
    return (
        db.query(UserGroup)
        .join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
        .filter(UserGroupMember.user_id == user_id)
        .all()
    )


def list_groups_page(
    db: Session, display_name: str | None, offset: int, limit: int
) -> tuple[list[UserGroup], int]:
    """``(page, total)`` of groups, optionally restricted to an exact ``display_name``."""
    query = db.query(UserGroup)
    if display_name is not None:
        query = query.filter(UserGroup.name == display_name)
    total = query.order_by(None).count()
    rows = query.order_by(UserGroup.id).offset(offset).limit(limit).all()
    return rows, total


def list_users_page(
    db: Session, *, email: str | None, external_id: str | None, offset: int, limit: int
) -> tuple[list[User], int]:
    """``(page, total)`` of accounts, optionally filtered by email or external id."""
    query = db.query(User)
    if email is not None:
        query = query.filter(User.email == email)
    if external_id is not None:
        query = query.filter(User.external_id == external_id)
    total = query.order_by(None).count()
    rows = query.order_by(User.id).offset(offset).limit(limit).all()
    return rows, total
