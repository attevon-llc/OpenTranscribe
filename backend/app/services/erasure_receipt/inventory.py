"""The erasure store registry: every store an erasure touches, and how to count it.

One list (``STORES``), not a function per call site: the receipt, the completeness test and the
docs all read it. A store is not "covered" because a delete exists for it; it is covered because
this registry can count what is left in it afterwards. ``register_erasure_store`` is the seam for
a store an edition owns -- one that is not registered here is invisible to the receipt (or, for an
OpenSearch index, shows up as ``os.unclassified`` and blocks verification).

Bump :data:`INVENTORY_VERSION` when a store's meaning changes (a new selector, a different
disposition). Adding a store does not change what earlier receipts said.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import table
from sqlalchemy import text

from app.services.erasure_receipt.stores_external import EXTERNAL_STORES
from app.services.erasure_receipt.stores_pg import PG_STORES
from app.services.erasure_receipt.stores_pg import Disposition
from app.services.erasure_receipt.stores_pg import PgStore
from app.services.erasure_receipt.types import Captured
from app.services.erasure_receipt.types import ErasureScope

logger = logging.getLogger(__name__)

INVENTORY_VERSION = 1

#: ``(scope, captured, db) -> count``; raises when the store could not be asked.
Counter = Callable[[ErasureScope, Captured, Any], int]


@dataclass(frozen=True)
class ErasureStore:
    key: str
    group: str
    disposition: Disposition
    reason: str | None
    count: Counter
    scopes: tuple[str, ...]
    retained_when_held: bool = False


def _pg_counter(store: PgStore) -> Counter:
    def count(scope: ErasureScope, captured: Captured, db: Any) -> int:
        clause = store.where[scope.kind]
        if store.exclude_held:
            clause = f"({clause}) AND id <> ALL(CAST(:held AS integer[]))"
        statement = select(func.count()).select_from(table(store.table)).where(text(clause))
        binds = {
            "uid": scope.user_id,
            "org": scope.organization_id,
            "fids": captured.file_ids,
            "sids": captured.speaker_ids,
            "held": captured.held_file_ids,
        }
        return int(db.execute(statement, binds).scalar_one() or 0)

    return count


def _from_pg(store: PgStore) -> ErasureStore:
    return ErasureStore(
        store.key,
        "pg",
        store.disposition,
        store.reason,
        _pg_counter(store),
        tuple(store.where),
        store.retained_when_held,
    )


def _from_external(store: Any) -> ErasureStore:
    def count(scope: ErasureScope, captured: Captured, db: Any, _m: Any = store.measure) -> int:
        return int(_m(scope, captured))

    return ErasureStore(
        store.key, store.group, store.disposition, store.reason, count, store.scopes
    )


STORES: list[ErasureStore] = [
    *(_from_pg(s) for s in PG_STORES),
    *(_from_external(s) for s in EXTERNAL_STORES),
]


def register_erasure_store(store: ErasureStore) -> None:
    """Add a store an edition owns. A duplicate key is refused: two counters for one store
    would let a receipt be satisfied by whichever reads zero."""
    if any(existing.key == store.key for existing in STORES):
        raise ValueError(f"erasure store {store.key!r} is already registered")
    STORES.append(store)


def stores_for(scope: ErasureScope) -> list[ErasureStore]:
    return [s for s in STORES if scope.kind in s.scopes]
