"""Take one measurement of every store in the inventory, in a FRESH session.

``None`` means the store could not be asked. It is never turned into 0: an unreachable store must
not read as an empty one. Failures are logged by exception CLASS only (a driver message can quote
a path, a key or a filename).
"""

from __future__ import annotations

import logging

from app.db.base import SessionLocal
from app.services.erasure_receipt.inventory import ErasureStore
from app.services.erasure_receipt.inventory import stores_for
from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope

logger = logging.getLogger(__name__)

#: ``store key -> count``, or ``None`` when it could not be measured.
Measurement = dict[str, int | None]


def _one(store: ErasureStore, scope: ErasureScope, captured: Captured, db) -> int | None:
    try:
        count = store.count(scope, captured, db)
        db.rollback()  # a failed statement must not poison the next store's read
        return count
    except Exception as e:  # noqa: BLE001 - unmeasurable is a reported state, not a crash
        db.rollback()
        logger.warning(
            "erasure receipt: %s could not be measured (%s)", store.key, type(e).__name__
        )
        return None


def measure_all(scope: ErasureScope, captured: Captured) -> Measurement:
    """Count every store that applies to ``scope``. Opens and closes its own session."""
    db = SessionLocal()
    try:
        return {s.key: _one(s, scope, captured, db) for s in stores_for(scope)}
    finally:
        db.close()
