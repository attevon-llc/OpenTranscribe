"""Turn a before and an after measurement into the persisted, numbers-only receipt.

For a store the erasure DELETES: ``deleted = before - after`` and ``residual = after``, which must
be 0. A store that could not be measured afterwards is ``unverified``, never 0. A store the erasure
keeps by design is counted, not judged: ``retained`` (compliance records, legal holds), ``ttl``,
and ``de_attributed`` (the row survives with the subject's key removed; ``residual`` is what is
STILL attributed to them).

The outcome is ``residual`` when anything was left behind, else ``unverified`` when anything could
not be measured, else ``verified``. Only ``verified`` makes an erasure complete.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from app.services.erasure_receipt.inventory import INVENTORY_VERSION
from app.services.erasure_receipt.inventory import stores_for
from app.services.erasure_receipt.measure import Measurement
from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.erasure import ErasureLedgerEntry
    from app.models.erasure import ErasureReceipt

logger = logging.getLogger(__name__)

#: The only text the receipt API returns about WHY a store keeps data: a fixed code.
RETAINED_REASONS: dict[str, str] = {
    "audit_trail": "Art. 17(3)(b)/(e): the audit trail is a compliance record",
    "compliance_record": "Art. 17(3)(b)/(e), Art. 30: a record keyed by surrogate ids",
    "legal_hold": "Art. 17(3)(e): an active legal hold retains the recording",
    "until_rotation": "kept until the backup or snapshot rotates",
    "tenant_vocabulary": "the organization's own vocabulary, kept with the subject removed",
    "legal_acceptance_retention": "Art. 17(3)(e): pseudonymised, kept to the retention date",
}


@dataclass
class ReceiptData:
    outcome: str
    deleted: dict[str, int] = field(default_factory=dict)
    de_attributed: dict[str, int] = field(default_factory=dict)
    retained: dict[str, int] = field(default_factory=dict)
    ttl: dict[str, int] = field(default_factory=dict)
    residual: dict[str, int] = field(default_factory=dict)
    unverified: dict[str, int] = field(default_factory=dict)
    inventory_version: int = INVENTORY_VERSION

    def maps(self) -> dict[str, dict[str, int]]:
        return {
            "deleted": self.deleted,
            "de_attributed": self.de_attributed,
            "retained": self.retained,
            "ttl": self.ttl,
            "residual": self.residual,
            "unverified": self.unverified,
        }

    @property
    def residual_total(self) -> int:
        return sum(self.residual.values())

    def digest(self) -> str:
        """sha256 of the canonical JSON of the data fields (sorted keys, no whitespace)."""
        body = {
            "outcome": self.outcome,
            "inventory_version": self.inventory_version,
            **self.maps(),
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_receipt(
    scope: ErasureScope,
    captured: Captured,
    before: Measurement | None,
    after: Measurement,
) -> ReceiptData:
    """Compare two measurements. ``before`` is ``None`` for a re-check with nothing to diff."""
    held = bool(captured.held_file_ids) or captured.org_hold_active
    data = ReceiptData(outcome="verified")
    for store in stores_for(scope):
        key = store.key
        now = after.get(key)
        was = (before or {}).get(key)
        if store.disposition == "retained":
            if now is not None:
                data.retained[key] = now
        elif store.disposition == "ttl":
            if now is not None:
                data.ttl[key] = now
        elif now is None:
            data.unverified[key] = 1
        elif store.disposition == "de_attributed":
            data.de_attributed[key] = max((was or 0) - now, 0)
            data.residual[key] = now
        else:
            if was is not None:
                data.deleted[key] = max(was - now, 0)
            if store.retained_when_held and held and now:
                data.retained[key] = now
                data.residual[key] = 0
            else:
                data.residual[key] = now
    if data.residual_total:
        data.outcome = "residual"
    elif data.unverified:
        data.outcome = "unverified"
    return data


def persist_receipt(
    db: Session, entry: ErasureLedgerEntry, data: ReceiptData, phase: str
) -> ErasureReceipt | None:
    """Insert one receipt row and append a numeric line to the journal. Never raises: the
    erasure already happened, and a receipt that cannot be stored must not undo that."""
    from app.models.erasure import ErasureReceipt
    from app.services import erasure_ledger_service as ledger

    try:
        row = ErasureReceipt(
            ledger_entry_id=entry.id,
            attempt=int(entry.attempts or 0) + 1,
            phase=phase,
            outcome=data.outcome,
            inventory_version=data.inventory_version,
            digest=data.digest(),
            **data.maps(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.error("Erasure receipt write FAILED for entry %s: %s", entry.uuid, type(e).__name__)
        return None
    ledger.append_receipt_line(entry, row)
    return row


def summary_receipt(row: ErasureReceipt | None, data: ReceiptData) -> dict[str, Any]:
    """The additive ``receipt`` object of an erase_* response: ids and numbers only."""
    return {
        "uuid": str(row.uuid) if row is not None else None,
        "outcome": data.outcome,
        "residual_total": data.residual_total,
        "unverified_keys": sorted(data.unverified),
        "digest": data.digest(),
    }
