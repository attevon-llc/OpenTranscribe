"""Scheduled (grace-period) erasure: run the requests that came due, whatever their scope (#1105).

A scheduled erasure IS an ``erasure_ledger`` entry with ``status='deferred'``,
``deferred_reason='grace_period'`` and a ``scheduled_for`` time (v455). There is one state machine
for every subject type (decision D6 of the self-service erasure plan): the account holder's own
request (``user``), an organization admin's request for one member's data (``org_member``) and
for the whole organization (``organization``) all wait in the same rows and are run here.

The executor MUST branch on ``subject_type``. Running every due entry as ``erase_user`` turned a
delayed member erasure into the deletion of that person's whole account, across every
organization they belong to: the one outcome an organization admin has no authority for.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.erasure import ErasureLedgerEntry
from app.models.user import User
from app.services import erasure_ledger_service as ledger

logger = logging.getLogger(__name__)

#: Executor batch per tick.
MAX_DUE_PER_RUN = 50


def _required_id(value: int | None, column: str) -> int:
    """A subject id the ledger's CHECK constraint guarantees for this ``subject_type``."""
    if value is None:
        raise ValueError(f"erasure ledger entry has no {column}")
    return value


def run_entry(db: Session, entry: ErasureLedgerEntry) -> dict[str, Any]:
    """Run the erasure ``entry`` describes, updating the entry in place.

    The one dispatch from a ledger entry to its erasure, used by the due-request executor and by
    the reconciliation sweep's retries. The existing entry is passed down so the run updates it
    rather than opening a second one: an SLA clock that restarted on every tick would never
    expire, which is precisely the failure mode the ledger exists to prevent.
    """
    from app.services import gdpr_erasure_service as erasure
    from app.services.erasure_org_member_rerun import cutoff_of

    if entry.subject_type == "user":
        return erasure.erase_user(
            db, _required_id(entry.subject_user_id, "subject_user_id"), ledger_entry=entry
        )
    if entry.subject_type == "file":
        return erasure.erase_file(db, entry.subject_file_uuid, ledger_entry=entry)
    if entry.subject_type == "organization":
        return erasure.erase_organization(
            db,
            _required_id(entry.subject_organization_id, "subject_organization_id"),
            ledger_entry=entry,
        )
    if entry.subject_type == "org_member":
        return erasure.erase_org_member_data(
            db,
            _required_id(entry.subject_user_id, "subject_user_id"),
            _required_id(entry.subject_organization_id, "subject_organization_id"),
            ledger_entry=entry,
            created_before=cutoff_of(entry),
        )
    raise ValueError(f"unknown erasure subject type {entry.subject_type!r}")


def _would_remove_last_super_admin(db: Session, user_id: int) -> bool:
    """Execution-time form of the last-super-admin rule, serialized by a row lock.

    Every active super administrator row is locked (id order, so two executions cannot
    deadlock) until the caller commits the ``pending`` flip. A super administrator whose own
    erasure is already ``pending`` no longer counts as remaining: their account may still exist
    while the destroy runs, which is exactly when a concurrent execution would otherwise see them.
    """
    active = (
        db.query(User.id)
        .filter(User.role == "super_admin", User.is_active.is_(True))
        .order_by(User.id)
        .with_for_update()
        .all()
    )
    active_ids = {int(r[0]) for r in active}
    if user_id not in active_ids:
        return False
    being_erased = {
        int(r[0])
        for r in db.query(ErasureLedgerEntry.subject_user_id).filter(
            ErasureLedgerEntry.subject_type == "user",
            ErasureLedgerEntry.status == "pending",
            ErasureLedgerEntry.subject_user_id.in_(active_ids),
        )
    }
    return not (active_ids - {user_id} - being_erased)


def _execute_one(db: Session, entry_id: int, now: datetime) -> bool:
    """Flip one due entry to ``pending`` and run its erasure. False when it was not due."""
    entry = db.get(ErasureLedgerEntry, entry_id)
    if (
        entry is None
        or entry.status != "deferred"
        or entry.deferred_reason != "grace_period"
        or entry.scheduled_for is None
        or entry.scheduled_for > now
    ):
        return False
    if entry.subject_type == "user" and _would_remove_last_super_admin(
        db, _required_id(entry.subject_user_id, "subject_user_id")
    ):
        ledger.block_request(db, entry, reason="last_super_admin")
        logger.warning("Scheduled erasure %s blocked: last active super administrator", entry.uuid)
        return False
    entry.status = "pending"
    entry.deferred_reason = None
    db.commit()
    db.refresh(entry)
    run_entry(db, entry)
    return True


def execute_due_requests(
    *, limit: int = MAX_DUE_PER_RUN, now: datetime | None = None
) -> dict[str, Any]:
    """Run every scheduled erasure whose time has come. One session per entry.

    A failure on one entry is counted and logged by id; the next tick finds it ``pending`` and the
    ordinary retry path (``erasure_ledger_service.open_entries``) finishes it.
    """
    from app.db.session_utils import session_scope

    current = now or datetime.now(UTC)
    with session_scope() as db:
        due_ids = [int(e.id) for e in ledger.due_grace_entries(db, limit=limit, now=current)]
    executed = failures = 0
    for entry_id in due_ids:
        try:
            with session_scope() as db:
                if _execute_one(db, entry_id, current):
                    executed += 1
        except Exception as e:  # noqa: BLE001 - one bad request must not stop the pass
            failures += 1
            logger.error("Scheduled erasure failed for ledger entry %s: %s", entry_id, e)
    return {"due": len(due_ids), "executed": executed, "failures": failures}
