"""Erasure receipts and single-file erasure (issue #1353). API only: there is no viewer.

* ``admin_router`` (mounted at ``/admin``, super_admin): list erasures, read a ledger entry's
  receipts, erase one file, and manage the allow-list of OpenSearch indices known to hold no
  personal data.
* ``org_router`` (mounted at ``/org-admin``, organization admin): read the receipts of a member or
  file erasure of the caller's OWN organization, and erase one file of it. Anything else (another
  tenant, a whole-organization erasure, a missing entry) is the same 404.

A receipt carries counts keyed by store name and a digest, never an address, a name or a file
uuid, and every read is audited with ids only.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import require_org_admin
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.org_admin import org_id_of
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import ERASURE_RECEIPT_READ_RATE_LIMIT
from app.db.base import get_db
from app.models.erasure import ErasureLedgerEntry
from app.models.erasure import ErasureReceipt
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.erasure_receipt import ErasureLedgerItem
from app.schemas.erasure_receipt import ErasureLedgerPage
from app.schemas.erasure_receipt import ErasureReceiptResponse
from app.schemas.erasure_receipt import IgnoredIndices
from app.services.erasure_receipt.inventory import STORES
from app.services.erasure_receipt.receipt import RETAINED_REASONS
from app.services.erasure_receipt.stores_external import IGNORED_INDICES_KEY

admin_router = APIRouter()
org_router = APIRouter()

_NOT_FOUND = "Erasure not found"


def _reasons(row: ErasureReceipt) -> dict[str, str]:
    by_key = {s.key: s.reason for s in STORES if s.reason}
    wanted = {*row.retained, *row.de_attributed}
    return {
        k: RETAINED_REASONS[by_key[k]] for k in sorted(wanted) if by_key.get(k) in RETAINED_REASONS
    }


def _response(row: ErasureReceipt, entry: ErasureLedgerEntry) -> ErasureReceiptResponse:
    return ErasureReceiptResponse(
        uuid=row.uuid,
        ledger_uuid=entry.uuid,
        subject_type=entry.subject_type,
        attempt=row.attempt,
        phase=row.phase,
        generated_at=row.generated_at,
        outcome=row.outcome,
        deleted=row.deleted,
        de_attributed=row.de_attributed,
        retained=row.retained,
        ttl=row.ttl,
        residual=row.residual,
        unverified=row.unverified,
        inventory_version=row.inventory_version,
        digest=row.digest,
        retained_reasons=_reasons(row),
    )


def _receipts(db: Session, entry: ErasureLedgerEntry) -> list[ErasureReceipt]:
    return list(
        db.execute(
            select(ErasureReceipt)
            .where(ErasureReceipt.ledger_entry_id == entry.id)
            .order_by(ErasureReceipt.generated_at.desc(), ErasureReceipt.id.desc())
        )
        .scalars()
        .all()
    )


def _audit_view(entry: ErasureLedgerEntry, viewer: User, org_id: int | None) -> None:
    audit_logger.log(
        event_type=AuditEventType.GDPR_RECEIPT_VIEW,
        outcome=AuditOutcome.SUCCESS,
        user_id=int(viewer.id),
        organization_id=org_id,
        details={"ledger_uuid": str(entry.uuid), "subject_type": entry.subject_type},
    )


@admin_router.get("/gdpr/erasures", response_model=ErasureLedgerPage)
@limiter.limit(ERASURE_RECEIPT_READ_RATE_LIMIT, key_func=user_or_ip_key)
def list_erasures(
    request: Request,
    response: Response,  # required by slowapi
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> ErasureLedgerPage:
    """Ledger entries newest first, each with the outcome of its latest receipt."""
    query = select(ErasureLedgerEntry)
    count = select(func.count()).select_from(ErasureLedgerEntry)
    if status_filter:
        query = query.where(ErasureLedgerEntry.status == status_filter)
        count = count.where(ErasureLedgerEntry.status == status_filter)
    entries = (
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
    return ErasureLedgerPage(
        items=[
            ErasureLedgerItem(
                uuid=e.uuid,
                subject_type=e.subject_type,
                status=e.status,
                actor_kind=e.actor_kind,
                requested_at=e.requested_at,
                sla_due_at=e.sla_due_at,
                completed_at=e.completed_at,
                attempts=e.attempts,
                latest_receipt_outcome=latest.get(int(e.id)),
            )
            for e in entries
        ],
        total=int(db.execute(count).scalar_one()),
        page=page,
        per_page=per_page,
    )


@admin_router.get(
    "/gdpr/erasures/{ledger_uuid}/receipts", response_model=list[ErasureReceiptResponse]
)
@limiter.limit(ERASURE_RECEIPT_READ_RATE_LIMIT, key_func=user_or_ip_key)
def read_receipts(
    request: Request,
    response: Response,  # required by slowapi
    ledger_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> list[ErasureReceiptResponse]:
    entry = db.execute(
        select(ErasureLedgerEntry).where(ErasureLedgerEntry.uuid == ledger_uuid)
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    _audit_view(entry, current_user, None)
    return [_response(r, entry) for r in _receipts(db, entry)]


@org_router.get(
    "/gdpr/erasures/{ledger_uuid}/receipts", response_model=list[ErasureReceiptResponse]
)
@limiter.limit(ERASURE_RECEIPT_READ_RATE_LIMIT, key_func=user_or_ip_key)
def read_org_receipts(
    request: Request,
    response: Response,  # required by slowapi
    ledger_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> list[ErasureReceiptResponse]:
    org_id = org_id_of(ctx)
    entry = db.execute(
        select(ErasureLedgerEntry).where(
            ErasureLedgerEntry.uuid == ledger_uuid,
            ErasureLedgerEntry.subject_type.in_(("org_member", "file")),
            ErasureLedgerEntry.subject_organization_id == org_id,
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    _audit_view(entry, ctx.user, org_id)
    return [_response(r, entry) for r in _receipts(db, entry)]


def _find_file(db: Session, file_uuid: UUID, org_id: int | None) -> MediaFile:
    query = select(MediaFile).where(MediaFile.uuid == file_uuid)
    if org_id is not None:
        query = query.where(MediaFile.organization_id == org_id)
    media_file = db.execute(query).scalar_one_or_none()
    if media_file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return media_file


@admin_router.post("/gdpr/erase-file/{file_uuid}")
def admin_erase_file(
    file_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict[str, Any]:
    """GDPR Art. 17 erasure of ONE recording, through the ledger, with a receipt."""
    from app.services.gdpr_erasure_service import erase_file

    _find_file(db, file_uuid, None)
    return erase_file(
        db,
        file_uuid,
        actor_user_id=int(current_user.id),
        actor_email=str(current_user.email),
        actor_role="super_admin",
    )


@org_router.post("/gdpr/erase-file/{file_uuid}")
def org_erase_file(
    file_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
) -> dict[str, Any]:
    """The same, for a recording of the caller's own organization (404 for any other)."""
    from app.services.gdpr_erasure_service import erase_file

    _find_file(db, file_uuid, org_id_of(ctx))
    return erase_file(
        db,
        file_uuid,
        actor_user_id=int(ctx.user.id),
        actor_email=str(ctx.user.email),
        actor_role="org_admin",
    )


@admin_router.get("/gdpr/erasure-receipt/ignored-indices", response_model=IgnoredIndices)
def read_ignored_indices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> IgnoredIndices:
    """OpenSearch indices an operator classified as holding no personal data."""
    from app.services.system_settings_service import get_setting

    raw = get_setting(db, IGNORED_INDICES_KEY, "[]") or "[]"
    try:
        names = [str(n) for n in json.loads(raw)]
    except ValueError:
        names = []
    return IgnoredIndices(names=names)


@admin_router.put("/gdpr/erasure-receipt/ignored-indices", response_model=IgnoredIndices)
def write_ignored_indices(
    body: IgnoredIndices,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> IgnoredIndices:
    """Replace the allow-list. Names only; without an entry an unclassified index keeps every
    erasure receipt ``unverified``. Audited with the count, not the names."""
    from app.services.system_settings_service import set_setting

    names = sorted({n.strip() for n in body.names if n.strip()})
    set_setting(
        db,
        IGNORED_INDICES_KEY,
        json.dumps(names),
        "OpenSearch indices classified as holding no personal data (erasure receipts)",
    )
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=int(current_user.id),
        details={"setting": IGNORED_INDICES_KEY, "count": len(names)},
    )
    return IgnoredIndices(names=names)
