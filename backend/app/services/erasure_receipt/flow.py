"""The receipt steps an ``erase_*`` function runs around its destroy (issue #1353).

``begin``  -- before anything is destroyed: name what the subject owns and count every store.
``finish`` -- after the destroy: count again in a fresh session, build and store the receipt, put
              its outcome on the summary, and schedule the 60 s re-check.

The ledger entry is closed by the caller afterwards, from the summary, so a receipt that is not
``verified`` makes the erasure ``deferred`` and the reconciliation sweep retries it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import Any

from app.services.erasure_receipt.capture import capture
from app.services.erasure_receipt.measure import Measurement
from app.services.erasure_receipt.measure import measure_all
from app.services.erasure_receipt.receipt import build_receipt
from app.services.erasure_receipt.receipt import persist_receipt
from app.services.erasure_receipt.receipt import summary_receipt
from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.erasure import ErasureLedgerEntry

logger = logging.getLogger(__name__)

#: The same reason as ``privacy.purge.SECOND_PASS_DELAY_SECONDS``: a debounced reindex or a late
#: write can land after the first measurement.
REVERIFY_DELAY_SECONDS = 60


def begin(db: Session, scope: ErasureScope) -> tuple[Captured, Measurement]:
    """Capture the subject's identifiers and take the "before" measurement."""
    captured = capture(db, scope)
    return captured, measure_all(scope, captured)


def reverify_payload(scope: ErasureScope, captured: Captured) -> dict[str, Any]:
    """What the delayed re-check needs: ids and uuids only. The address stays in memory (the
    lockout key is deleted synchronously and is not part of the delayed check)."""
    payload = asdict(captured)
    payload.pop("email", None)
    return {"scope": asdict(scope), "captured": payload}


def _schedule_reverify(entry: ErasureLedgerEntry, scope: ErasureScope, captured: Captured) -> None:
    try:
        from app.tasks.erasure_receipt_reverify import erasure_receipt_reverify

        erasure_receipt_reverify.apply_async(
            kwargs={"ledger_uuid": str(entry.uuid), **reverify_payload(scope, captured)},
            countdown=REVERIFY_DELAY_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 - the immediate receipt stands; the sweep still runs
        logger.error("Could not schedule the erasure receipt re-check: %s", type(e).__name__)


def finish(
    db: Session,
    entry: ErasureLedgerEntry | None,
    scope: ErasureScope,
    captured: Captured,
    before: Measurement,
    summary: dict[str, Any],
) -> None:
    """Measure again, store the receipt, and make ``summary`` reflect it."""
    after = measure_all(scope, captured)
    data = build_receipt(scope, captured, before, after)
    phase = "resurrection" if entry is not None and entry.resurrections_detected else "immediate"
    row = persist_receipt(db, entry, data, phase) if entry is not None else None
    summary["receipt"] = summary_receipt(row, data)
    if entry is not None and row is None:
        # No stored receipt means nothing proves the erasure: leave the entry open for the
        # retry sweep instead of closing it as complete.
        summary["receipt"]["outcome"] = "unverified"
        summary["errors"].append(
            {"stage": "receipt", "error": "erasure receipt could not be stored"}
        )
    elif data.outcome != "verified":
        # A fixed sentence: the ledger keeps only the count, and `complete` follows from it.
        summary["errors"].append(
            {"stage": "receipt", "error": f"erasure receipt is {data.outcome}, not verified"}
        )
    if entry is not None:
        _schedule_reverify(entry, scope, captured)
