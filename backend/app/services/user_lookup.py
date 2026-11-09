"""Account lookups and listings used by the user, admin and auth routers.

Every function takes the request's ``Session`` and plain arguments, and none commits: the
caller keeps the transaction boundary it had when these queries lived in the router.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth.roles import ROLE_SUPER_ADMIN
from app.models.organization import OrganizationMembership
from app.models.user import User
from app.utils.pagination import paginate


def get_user_by_uuid(db: Session, user_uuid: UUID | str) -> User | None:
    """The account with this public UUID, or ``None``."""
    return db.query(User).filter(User.uuid == user_uuid).first()


def get_user_by_id(db: Session, user_id: int) -> User | None:
    """The account with this primary key, or ``None``."""
    return db.query(User).filter(User.id == user_id).first()


def get_user_by_email(db: Session, email: str) -> User | None:
    """The account whose stored email equals ``email`` exactly, or ``None``."""
    return db.query(User).filter(User.email == email).first()


def get_user_by_lower_email(db: Session, lowered_email: str) -> User | None:
    """The account whose email, lower-cased, equals ``lowered_email``."""
    return db.query(User).filter(func.lower(User.email) == lowered_email).first()


def get_user_by_email_or_ldap_uid(db: Session, username: str) -> User | None:
    """The account a sign-in name resolves to: its email or its LDAP uid."""
    return db.query(User).filter(or_(User.email == username, User.ldap_uid == username)).first()


def get_users_by_lower_emails(db: Session, lowered_emails: Collection[str]) -> list[User]:
    """Every account whose lower-cased email is in ``lowered_emails``."""
    return db.query(User).filter(func.lower(User.email).in_(lowered_emails)).all()


def get_users_by_uuids(db: Session, user_uuids: Collection[str]) -> list[User]:
    """Every account whose public UUID is in ``user_uuids``."""
    return db.query(User).filter(User.uuid.in_(user_uuids)).all()


def get_email_by_user_id(db: Session, user_ids: Collection[int]) -> dict[int, str]:
    """``{user.id: user.email}`` for the given primary keys (only those that exist)."""
    rows = db.query(User.id, User.email).filter(User.id.in_(user_ids)).all()
    return {row.id: row.email for row in rows}


def get_first_super_admin(db: Session) -> User | None:
    """The lowest-id ``super_admin`` account, or ``None``."""
    return db.query(User).filter(User.role == ROLE_SUPER_ADMIN).order_by(User.id).first()


def find_other_user_by_column(
    db: Session, column_name: str, value: str, exclude_user_id: int
) -> User | None:
    """Another account whose ``column_name`` equals ``value`` (identifier-conflict check)."""
    return (
        db.query(User)
        .filter(getattr(User, column_name) == value)
        .filter(User.id != exclude_user_id)
        .first()
    )


def find_other_user_by_email_ci(db: Session, email: str, exclude_user_id: int) -> User | None:
    """Another account whose email equals ``email`` ignoring case."""
    return (
        db.query(User)
        .filter(func.lower(User.email) == email.lower())
        .filter(User.id != exclude_user_id)
        .first()
    )


def list_users_by_id(db: Session, offset: int, limit: int) -> list[User]:
    """A page of accounts ordered by primary key."""
    return db.query(User).order_by(User.id).offset(offset).limit(limit).all()


def list_users_by_name(db: Session, offset: int, limit: int) -> list[User]:
    """A page of accounts ordered by case-insensitive full name."""
    return db.query(User).order_by(func.lower(User.full_name)).offset(offset).limit(limit).all()


def list_users_by_approval_status(
    db: Session, approval_status: str, offset: int, limit: int
) -> list[User]:
    """A page of accounts in ``approval_status``, oldest first (a work queue)."""
    return (
        db.query(User)
        .filter(User.approval_status == approval_status)
        .order_by(User.created_at, User.id)
        .offset(offset)
        .limit(limit)
        .all()
    )


def search_directory(
    db: Session,
    *,
    text_query: str,
    caller_user_id: int,
    org_id: int | None,
    is_org_context: bool,
    limit: int = 20,
) -> list[User]:
    """Active accounts matching ``text_query`` in the caller's tenant, excluding the caller.

    In org context only members of ``org_id`` are candidates; in personal scope only
    accounts with no org membership are. The tenant gate is a correlated EXISTS rather
    than a join so a multi-membership account cannot duplicate rows.
    """
    caller_org_member = exists().where(
        OrganizationMembership.user_id == User.id,
        OrganizationMembership.organization_id == org_id,
    )
    any_org_member = exists().where(OrganizationMembership.user_id == User.id)
    tenant_gate = caller_org_member if is_org_context else ~any_org_member
    pattern = f"%{text_query}%"
    return (
        db.query(User)
        .filter(
            User.id != caller_user_id,
            User.is_active.is_(True),
            tenant_gate,
            or_(User.email.ilike(pattern), User.full_name.ilike(pattern)),
        )
        .order_by(User.full_name, User.email)
        .limit(limit)
        .all()
    )


def search_users_paginated(
    db: Session,
    *,
    text_query: str | None,
    role: str | None,
    auth_type: str | None,
    is_active: bool | None,
    offset: int,
    limit: int,
) -> tuple[list[Any], int]:
    """``(rows, total)`` for the admin user search; every filter is optional."""
    q = db.query(User)
    if text_query:
        q = q.filter(
            or_(User.email.ilike(f"%{text_query}%"), User.full_name.ilike(f"%{text_query}%"))
        )
    if role:
        q = q.filter(User.role == role)
    if auth_type:
        q = q.filter(User.auth_type == auth_type)
    if is_active is not None:
        q = q.filter(User.is_active == is_active)
    return paginate(q, offset, limit)


def count_user_accounts(db: Session, expired_filter: ColumnElement[bool]) -> Any:
    """One row with ``total``, ``active`` and ``pwd_expired`` account counts."""
    return db.query(
        func.count().label("total"),
        func.count().filter(User.is_active.is_(True)).label("active"),
        func.count().filter(expired_filter).label("pwd_expired"),
    ).one()
