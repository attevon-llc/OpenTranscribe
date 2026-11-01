"""Reading and writing legal acceptances (DB side of ``app.legal``)."""

from __future__ import annotations

import uuid as uuid_pkg

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.legal import registry
from app.models.legal_acceptance import LegalAcceptance


def find_acceptance(
    db: Session, user_id: int, doc: registry.LegalDocument
) -> LegalAcceptance | None:
    """The user's acceptance of *doc*'s CURRENT text, if any."""
    return db.execute(
        select(LegalAcceptance).where(
            LegalAcceptance.user_id == user_id,
            LegalAcceptance.scope == "user",
            LegalAcceptance.document_key == doc.key,
            LegalAcceptance.sha256 == doc.sha256,
        )
    ).scalar_one_or_none()


#: Column width; a client-supplied header is never trusted for length.
USER_AGENT_MAX = 512


def find_org_acceptance(
    db: Session, organization_uuid: uuid_pkg.UUID, doc: registry.LegalDocument
) -> LegalAcceptance | None:
    """The organization's own acceptance (``scope='organization'``) of *doc*'s CURRENT text."""
    return db.execute(
        select(LegalAcceptance).where(
            LegalAcceptance.organization_uuid == organization_uuid,
            LegalAcceptance.scope == "organization",
            LegalAcceptance.document_key == doc.key,
            LegalAcceptance.sha256 == doc.sha256,
        )
    ).scalar_one_or_none()


def record_acceptance(
    db: Session,
    user_id: int,
    doc: registry.LegalDocument,
    ip_address: str | None,
    *,
    user_agent: str | None = None,
    locale: str | None = None,
    organization_id: int | None = None,
    organization_uuid: uuid_pkg.UUID | None = None,
    scope: str = "user",
) -> tuple[LegalAcceptance, bool]:
    """Store an acceptance; returns ``(row, created)``. Re-accepting is a no-op.

    ``scope='organization'`` records *user_id* accepting on behalf of the organization named
    by ``organization_uuid``; idempotency is then per organization, not per user.
    """
    if scope not in ("user", "organization"):
        raise ValueError(f"unknown acceptance scope: {scope!r}")
    if scope == "organization" and organization_uuid is None:
        raise ValueError("an organization acceptance needs organization_uuid")

    def _existing() -> LegalAcceptance | None:
        if organization_uuid is not None and scope == "organization":
            return find_org_acceptance(db, organization_uuid, doc)
        return find_acceptance(db, user_id, doc)

    existing = _existing()
    if existing is not None:
        return existing, False
    row = LegalAcceptance(
        user_id=user_id,
        document_key=doc.key,
        version=doc.version,
        sha256=doc.sha256,
        ip_address=ip_address,
        user_agent=user_agent[:USER_AGENT_MAX] if user_agent else None,
        locale=locale,
        organization_id=organization_id,
        organization_uuid=organization_uuid,
        scope=scope,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent identical acceptance won its partial UNIQUE index: the idempotent case.
        db.rollback()
        winner = _existing()
        if winner is None:
            raise
        return winner, False
    db.refresh(row)
    return row, True


def operator_acknowledgment_status(db: Session, user_id: int) -> tuple[bool, bool]:
    """``(legal_required, legal_acknowledged)`` for the operator acknowledgment."""
    required = registry.requires_acknowledgment(registry.OPERATOR_ACKNOWLEDGMENT_KEY)
    if not required:
        return False, True
    doc = registry.get_document(registry.OPERATOR_ACKNOWLEDGMENT_KEY)
    if doc is None:
        return True, False
    return True, find_acceptance(db, user_id, doc) is not None
