"""Self-service account erasure: request, cancel, and run the requests that came due (#1354).

A request IS an ``erasure_ledger`` entry (``status='deferred'``, ``deferred_reason='grace_period'``,
``scheduled_for``), so there is one SLA clock and one journal; cancelling sets ``cancelled``. The
account stays usable during the grace period and logging in never cancels (an automated session
refresh would cancel silently): only an explicit cancel does.

The request receipt is the ledger uuid, the two timestamps and a sha256 digest over those canonical
fields. It is not the erasure receipt, which is measured after the destruction and linked by the
same ledger uuid.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_GDPR_SELF_ERASURE_ENABLED
from app.core.constants import DEFAULT_GDPR_SELF_ERASURE_GRACE_DAYS
from app.core.constants import GDPR_SELF_ERASURE_ENABLED_KEY
from app.core.constants import GDPR_SELF_ERASURE_GRACE_DAYS_KEY
from app.core.constants import MAX_GDPR_SELF_ERASURE_GRACE_DAYS
from app.models.erasure import ErasureLedgerEntry
from app.models.user import User
from app.services import erasure_ledger_service as ledger
from app.services.system_settings_service import get_setting
from app.utils.uuid7 import uuid7

logger = logging.getLogger(__name__)

#: Executor batch per reconcile tick.
MAX_DUE_PER_RUN = 50


class SelfErasureError(Exception):
    """A refusal with a stable ``code`` the API maps to a 409 (or 404)."""

    def __init__(self, code: str, message: str, *, entry: ErasureLedgerEntry | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.entry = entry


def is_enabled(db: Session) -> bool:
    raw = get_setting(db, GDPR_SELF_ERASURE_ENABLED_KEY, None)
    if raw is None:
        return DEFAULT_GDPR_SELF_ERASURE_ENABLED
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def grace_days(db: Session) -> int:
    """The configured grace period, clamped to ``0..MAX_GDPR_SELF_ERASURE_GRACE_DAYS``."""
    raw = get_setting(db, GDPR_SELF_ERASURE_GRACE_DAYS_KEY, None)
    try:
        value = int(raw) if raw is not None else DEFAULT_GDPR_SELF_ERASURE_GRACE_DAYS
    except (TypeError, ValueError):
        value = DEFAULT_GDPR_SELF_ERASURE_GRACE_DAYS
    return max(0, min(value, MAX_GDPR_SELF_ERASURE_GRACE_DAYS))


def request_digest(
    ledger_uuid: uuid_pkg.UUID, requested_at: datetime, scheduled_for: datetime
) -> str:
    """sha256 over the canonical request fields; a reference the subject can quote."""
    canonical = json.dumps(
        {
            "ledger_uuid": str(ledger_uuid),
            "requested_at": requested_at.astimezone(UTC).isoformat(),
            "scheduled_for": scheduled_for.astimezone(UTC).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_last_blocked_request(db: Session, user_id: int) -> ErasureLedgerEntry | None:
    """The caller's most recent self-service request, if the system closed it without erasing.

    A blocked request (``erasure_ledger_service.block_request``) is ``cancelled`` with no
    ``cancelled_at``; the subject's own cancellation stamps it. The reason cannot be stored on
    the row (``deferred_reason`` is CHECK-limited and widening that needs a migration), and
    ``block_request`` is called for exactly one reason today, the last super administrator.
    Only the LATEST row counts: a newer request, cancellation or erasure hides an older block.
    """
    latest = (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.subject_type == "user",
            ErasureLedgerEntry.subject_user_id == user_id,
            ErasureLedgerEntry.actor_kind == "data_subject",
        )
        .order_by(ErasureLedgerEntry.requested_at.desc(), ErasureLedgerEntry.id.desc())
        .first()
    )
    if latest is None or latest.status != "cancelled" or latest.cancelled_at is not None:
        return None
    return latest


def get_open_request(db: Session, user_id: int) -> ErasureLedgerEntry | None:
    return (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.subject_type == "user",
            ErasureLedgerEntry.subject_user_id == user_id,
            ErasureLedgerEntry.status == "deferred",
            ErasureLedgerEntry.deferred_reason == "grace_period",
        )
        .first()
    )


def _is_last_super_admin(db: Session, user: User) -> bool:
    if user.role != "super_admin":
        return False
    others = (
        db.query(User.id)
        .filter(User.role == "super_admin", User.is_active.is_(True), User.id != user.id)
        .first()
    )
    return others is None


def request_self_erasure(
    db: Session, user: User, *, now: datetime | None = None
) -> ErasureLedgerEntry:
    """Open a self-service erasure request for ``user``.

    Raises:
        SelfErasureError: ``disabled``, ``last_super_admin`` or ``request_exists`` (carries the
            existing entry), ``ledger_unavailable`` when the record could not be written. Nothing
            is partially created on a refusal.
    """
    if not is_enabled(db):
        raise SelfErasureError("disabled", "Self-service erasure is not enabled.")
    existing = get_open_request(db, int(user.id))
    if existing is not None:
        raise SelfErasureError("request_exists", "A request is already open.", entry=existing)
    if _is_last_super_admin(db, user):
        raise SelfErasureError(
            "last_super_admin", "The last active super administrator cannot be erased."
        )

    requested_at = now or datetime.now(UTC)
    scheduled_for = requested_at + timedelta(days=grace_days(db))
    entry_uuid = uuid7()
    entry = ledger.record_request(
        db,
        subject_type="user",
        subject_user_id=int(user.id),
        subject_user_uuid=user.uuid,
        actor_kind="data_subject",
        actor_user_id=int(user.id),
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=scheduled_for,
        request_digest=request_digest(entry_uuid, requested_at, scheduled_for),
        requested_at=requested_at,
        entry_uuid=entry_uuid,
    )
    if entry is None:
        # record_request swallows the unique-index race as well as real failures.
        existing = get_open_request(db, int(user.id))
        if existing is not None:
            raise SelfErasureError("request_exists", "A request is already open.", entry=existing)
        raise SelfErasureError("ledger_unavailable", "The request could not be recorded.")
    return entry


def cancel_self_erasure(
    db: Session, user: User, entry_uuid: uuid_pkg.UUID, *, now: datetime | None = None
) -> ErasureLedgerEntry:
    """Withdraw the caller's own request before ``scheduled_for``.

    Raises:
        SelfErasureError: ``not_found`` (also for another user's uuid), ``already_executing`` once
            the grace period is over, ``not_cancellable`` for any other state.
    """
    entry = (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.uuid == entry_uuid,
            ErasureLedgerEntry.subject_type == "user",
            ErasureLedgerEntry.subject_user_id == int(user.id),
            ErasureLedgerEntry.actor_kind == "data_subject",
        )
        .first()
    )
    if entry is None:
        raise SelfErasureError("not_found", "No such request.")
    current = now or datetime.now(UTC)
    if entry.status == "deferred" and entry.deferred_reason == "grace_period":
        scheduled = entry.scheduled_for
        if scheduled is not None and current >= scheduled:
            raise SelfErasureError("already_executing", "The grace period is over.", entry=entry)
        return ledger.cancel_request(db, entry, now=current)
    if entry.status in ("pending", "deferred", "complete", "failed"):
        raise SelfErasureError("already_executing", "The erasure has started.", entry=entry)
    raise SelfErasureError("not_cancellable", "This request cannot be cancelled.", entry=entry)


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
    """Flip one due request to ``pending`` and erase the account. False when it was not due."""
    from app.services.gdpr_erasure_service import erase_user

    entry = db.get(ErasureLedgerEntry, entry_id)
    if (
        entry is None
        or entry.status != "deferred"
        or entry.deferred_reason != "grace_period"
        or entry.scheduled_for is None
        or entry.subject_user_id is None
        or entry.scheduled_for > now
    ):
        return False
    if _would_remove_last_super_admin(db, int(entry.subject_user_id)):
        ledger.block_request(db, entry, reason="last_super_admin")
        logger.warning(
            "Self-service erasure %s blocked: last active super administrator", entry.uuid
        )
        return False
    entry.status = "pending"
    entry.deferred_reason = None
    db.commit()
    db.refresh(entry)
    erase_user(db, int(entry.subject_user_id), ledger_entry=entry)
    return True


def execute_due_requests(
    *, limit: int = MAX_DUE_PER_RUN, now: datetime | None = None
) -> dict[str, Any]:
    """Run every request whose grace period has run out. One session per entry.

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
            logger.error("Self-service erasure failed for ledger entry %s: %s", entry_id, e)
    return {"due": len(due_ids), "executed": executed, "failures": failures}
