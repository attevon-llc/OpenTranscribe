"""Group-mapping reads for the admin group-mapping routes. None of these commit."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.orm import Session

from app.models.group import MEMBERSHIP_SOURCES_PROTECTED
from app.models.group import GroupMapping
from app.models.group import UserGroup
from app.models.group import UserGroupMember


def list_directory_member_rows(db: Session, group_ids: Collection[int]) -> list:
    """``(group_id, source)`` rows for memberships a directory pass owns.

    ``manual`` and ``scim`` are excluded because neither is derived from a mapping, see
    ``models.group.MEMBERSHIP_SOURCES_PROTECTED``.
    """
    return (
        db.query(UserGroupMember.group_id, UserGroupMember.source)
        .filter(
            UserGroupMember.group_id.in_(group_ids),
            UserGroupMember.source.notin_(MEMBERSHIP_SOURCES_PROTECTED),
        )
        .all()
    )


def list_mappings_for_source(db: Session, source: str) -> list[GroupMapping]:
    """Every mapping under one source (``ldap`` or ``oidc``)."""
    return db.query(GroupMapping).filter(GroupMapping.source == source).all()


def list_mappings(db: Session, source: str | None) -> list[GroupMapping]:
    """Every mapping, optionally one source's, ordered by source then id."""
    query = db.query(GroupMapping)
    if source:
        query = query.filter(GroupMapping.source == source)
    return query.order_by(GroupMapping.source, GroupMapping.id).all()


def get_groups_by_ids(db: Session, group_ids: Collection[int]) -> list[UserGroup]:
    """Every group whose primary key is in ``group_ids``."""
    return db.query(UserGroup).filter(UserGroup.id.in_(group_ids)).all()
