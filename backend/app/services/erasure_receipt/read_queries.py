"""Read-side queries behind the erasure ledger and receipt endpoints."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.erasure import ErasureLedgerEntry
from app.models.erasure import ErasureReceipt
from app.models.media import MediaFile

#: Subject types an organization admin may read receipts for.
ORG_VISIBLE_SUBJECT_TYPES = ("org_member", "file")


def list_ledger_page(
    db: Session, *, status: str | None, page: int, per_page: int
) -> tuple[list[ErasureLedgerEntry], int, dict[int, str]]:
    """One page of ledger entries (newest first), the total, and each entry's latest receipt outcome."""
    query = select(ErasureLedgerEntry)
    count = select(func.count()).select_from(ErasureLedgerEntry)
    if status:
        query = query.where(ErasureLedgerEntry.status == status)
        count = count.where(ErasureLedgerEntry.status == status)
    entries = list(
        db.execute(
            query.order_by(ErasureLedgerEntry.requested_at.desc(), ErasureLedgerEntry.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    latest: dict[int, str] = {}
    if entries:
        for receipt in db.execute(
            select(ErasureReceipt)
            .where(ErasureReceipt.ledger_entry_id.in_([e.id for e in entries]))
            .order_by(ErasureReceipt.generated_at, ErasureReceipt.id)
        ).scalars():
            latest[int(receipt.ledger_entry_id)] = str(receipt.outcome)
    return entries, int(db.execute(count).scalar_one()), latest


def get_ledger_entry(db: Session, ledger_uuid: UUID) -> ErasureLedgerEntry | None:
    """The ledger entry with this uuid, or ``None``."""
    return db.execute(
        select(ErasureLedgerEntry).where(ErasureLedgerEntry.uuid == ledger_uuid)
    ).scalar_one_or_none()


def get_org_ledger_entry(db: Session, ledger_uuid: UUID, org_id: int) -> ErasureLedgerEntry | None:
    """The member or file erasure of organization *org_id* with this uuid, or ``None``."""
    return db.execute(
        select(ErasureLedgerEntry).where(
            ErasureLedgerEntry.uuid == ledger_uuid,
            ErasureLedgerEntry.subject_type.in_(ORG_VISIBLE_SUBJECT_TYPES),
            ErasureLedgerEntry.subject_organization_id == org_id,
        )
    ).scalar_one_or_none()


def list_receipts(db: Session, entry: ErasureLedgerEntry) -> list[ErasureReceipt]:
    """Receipts of *entry*, newest first."""
    return list(
        db.execute(
            select(ErasureReceipt)
            .where(ErasureReceipt.ledger_entry_id == entry.id)
            .order_by(ErasureReceipt.generated_at.desc(), ErasureReceipt.id.desc())
        )
        .scalars()
        .all()
    )


def find_file(db: Session, file_uuid: UUID, org_id: int | None) -> MediaFile | None:
    """The recording with this uuid, restricted to organization *org_id* when given."""
    query = select(MediaFile).where(MediaFile.uuid == file_uuid)
    if org_id is not None:
        query = query.where(MediaFile.organization_id == org_id)
    return db.execute(query).scalar_one_or_none()
