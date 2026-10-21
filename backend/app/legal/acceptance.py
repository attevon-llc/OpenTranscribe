"""Reading and writing legal acceptances (DB side of ``app.legal``)."""

from __future__ import annotations

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
            LegalAcceptance.document_key == doc.key,
            LegalAcceptance.sha256 == doc.sha256,
        )
    ).scalar_one_or_none()


def record_acceptance(
    db: Session, user_id: int, doc: registry.LegalDocument, ip_address: str | None
) -> tuple[LegalAcceptance, bool]:
    """Store an acceptance; returns ``(row, created)``. Re-accepting is a no-op."""
    existing = find_acceptance(db, user_id, doc)
    if existing is not None:
        return existing, False
    row = LegalAcceptance(
        user_id=user_id,
        document_key=doc.key,
        version=doc.version,
        sha256=doc.sha256,
        ip_address=ip_address,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent identical acceptance won the UNIQUE key: that is the idempotent case.
        db.rollback()
        winner = find_acceptance(db, user_id, doc)
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
