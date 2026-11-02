"""Delayed re-check of an erasure (issue #1353): count again a minute after the erasure.

A debounced reindex or a late write can put a document back after the immediate receipt was
taken. This measures the same stores again, stores a second receipt (``phase = "delayed"``), and
re-opens the ledger entry when anything is left, so the reconciliation sweep retries it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.constants import UtilityPriority

logger = logging.getLogger(__name__)


@celery_app.task(
    name="erasure.receipt_reverify",
    priority=UtilityPriority.BACKGROUND,
)
def erasure_receipt_reverify(
    ledger_uuid: str, scope: dict[str, Any], captured: dict[str, Any]
) -> dict[str, Any]:
    """Measure the subject's stores again and store a ``delayed`` receipt."""
    from app.db.session_utils import session_scope
    from app.models.erasure import ErasureLedgerEntry
    from app.services import erasure_ledger_service as ledger
    from app.services.erasure_receipt.measure import measure_all
    from app.services.erasure_receipt.receipt import build_receipt
    from app.services.erasure_receipt.receipt import persist_receipt
    from app.services.erasure_receipt.types import Captured
    from app.services.erasure_receipt.types import ErasureScope

    erasure_scope = ErasureScope(
        kind=scope["kind"],
        user_id=scope.get("user_id"),
        organization_id=scope.get("organization_id"),
        file_ids=tuple(scope.get("file_ids") or ()),
    )
    subject = Captured(**{**captured, "file_owners": [tuple(p) for p in captured["file_owners"]]})
    data = build_receipt(erasure_scope, subject, None, measure_all(erasure_scope, subject))
    with session_scope() as db:
        entry = db.query(ErasureLedgerEntry).filter(ErasureLedgerEntry.uuid == ledger_uuid).first()
        if entry is None:
            return {"status": "no_entry"}
        persist_receipt(db, entry, data, "delayed")
        if data.outcome != "verified":
            ledger.reopen_entry(db, entry)
    return {"status": data.outcome, "residual_total": data.residual_total}
