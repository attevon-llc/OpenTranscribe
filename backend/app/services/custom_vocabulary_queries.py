"""Tenant-scoped reads and writes of custom vocabulary terms.

A user's terms are stamped with the tenant they were created in (``organization_id``, NULL =
personal). Rows with no owner are shared terms: unstamped ones are instance-wide, stamped ones
belong to that organization. Every function takes the request's ``Session`` and plain
arguments, and none commits.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.models.custom_vocabulary import CustomVocabulary
from app.utils.db_helpers import org_stamp_is


def _own_terms(user_id: int, org_id: int | None) -> Any:
    """Predicate: the user's own terms in tenant ``org_id``."""
    return (CustomVocabulary.user_id == user_id) & org_stamp_is(
        CustomVocabulary.organization_id, org_id
    )


def _shared_terms(org_id: int | None) -> Any:
    """Predicate: owner-less terms visible in tenant ``org_id``."""
    visible_stamp: ColumnElement[bool] = CustomVocabulary.organization_id.is_(None)
    if org_id is not None:
        visible_stamp = visible_stamp | (CustomVocabulary.organization_id == org_id)
    return CustomVocabulary.user_id.is_(None) & visible_stamp


def _narrow(
    query: Query[CustomVocabulary], domain: str | None, active_only: bool
) -> Query[CustomVocabulary]:
    if domain:
        query = query.filter(CustomVocabulary.domain == domain)
    if active_only:
        query = query.filter(CustomVocabulary.is_active.is_(True))
    return query


def find_vocabulary_term(db: Session, term_id: int) -> CustomVocabulary | None:
    """The term with this primary key, whatever its owner or tenant."""
    return db.query(CustomVocabulary).filter(CustomVocabulary.id == term_id).first()


def list_own_vocabulary(
    db: Session,
    user_id: int,
    org_id: int | None,
    *,
    domain: str | None = None,
    active_only: bool = False,
) -> list[CustomVocabulary]:
    """The user's terms in the tenant, ordered by domain then term."""
    query = _narrow(
        db.query(CustomVocabulary).filter(_own_terms(user_id, org_id)), domain, active_only
    )
    return query.order_by(CustomVocabulary.domain, CustomVocabulary.term).all()


def list_shared_vocabulary(
    db: Session,
    org_id: int | None,
    *,
    domain: str | None = None,
    active_only: bool = False,
) -> list[CustomVocabulary]:
    """Owner-less terms visible in the tenant, ordered by domain then term."""
    query = _narrow(db.query(CustomVocabulary).filter(_shared_terms(org_id)), domain, active_only)
    return query.order_by(CustomVocabulary.domain, CustomVocabulary.term).all()


def find_own_term(
    db: Session, user_id: int, org_id: int | None, term: str, domain: str
) -> CustomVocabulary | None:
    """The user's term with this text in this domain inside the tenant, or ``None``."""
    return (
        db.query(CustomVocabulary)
        .filter(
            _own_terms(user_id, org_id),
            CustomVocabulary.term == term,
            CustomVocabulary.domain == domain,
        )
        .first()
    )


def own_term_clashes(
    db: Session, user_id: int, org_id: int | None, term: str, domain: str, exclude_id: int
) -> bool:
    """Whether another of the user's terms in the tenant already has this text and domain."""
    return (
        db.query(CustomVocabulary.id)
        .filter(
            _own_terms(user_id, org_id),
            CustomVocabulary.term == term,
            CustomVocabulary.domain == domain,
            CustomVocabulary.id != exclude_id,
        )
        .first()
        is not None
    )


def own_term_pairs(db: Session, user_id: int, org_id: int | None) -> set[tuple[str, str]]:
    """Every ``(term, domain)`` the user already has in the tenant, in one query."""
    return {
        (row.term, row.domain)
        for row in db.query(CustomVocabulary.term, CustomVocabulary.domain)
        .filter(_own_terms(user_id, org_id))
        .all()
    }


def delete_own_vocabulary(
    db: Session, user_id: int, org_id: int | None, *, domain: str | None = None
) -> None:
    """Bulk-delete the user's terms in the tenant, optionally only one domain."""
    _narrow(db.query(CustomVocabulary).filter(_own_terms(user_id, org_id)), domain, False).delete(
        synchronize_session=False
    )
