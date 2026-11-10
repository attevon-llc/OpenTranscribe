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

import hashlib
import json
import logging
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy.orm import Session

from app.auth.audit import AuditOutcome
from app.auth.audit_events import TENANT_SCOPE_ORG
from app.auth.audit_events import TENANT_SCOPE_PERSONAL
from app.auth.audit_events import AuditEventType
from app.core.constants import DEFAULT_GDPR_ORG_ERASURE_DELAY_HOURS
from app.core.constants import GDPR_ORG_ERASURE_DELAY_HOURS_KEY
from app.core.constants import GDPR_ORG_MEMBER_ERASURE_DELAY_HOURS_KEY
from app.core.constants import MAX_GDPR_ORG_ERASURE_DELAY_HOURS
from app.core.constants import MAX_GDPR_SELF_ERASURE_GRACE_DAYS
from app.core.constants import MIN_GDPR_ORG_ERASURE_DELAY_HOURS
from app.models.erasure import ErasureLedgerEntry
from app.models.user import User
from app.services import audit_outbox
from app.services import erasure_ledger_service as ledger
from app.services.system_settings_service import get_setting
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.organization import Organization

logger = logging.getLogger(__name__)

#: Executor batch per tick.
MAX_DUE_PER_RUN = 50

#: The scopes an organization admin may schedule, list and cancel.
ORG_SUBJECT_TYPES = ("org_member", "organization")


class ScheduledErasureError(Exception):
    """A refusal with a stable ``code`` the API maps to a 409 (or 503 for ``ledger_unavailable``)."""

    def __init__(self, code: str, message: str, *, entry: ErasureLedgerEntry | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.entry = entry


#: The settings key and default of each delay an organization admin's request waits.
_DELAY_KEYS = {
    "organization": GDPR_ORG_ERASURE_DELAY_HOURS_KEY,
    "org_member": GDPR_ORG_MEMBER_ERASURE_DELAY_HOURS_KEY,
}

#: The longest any scheduled erasure may wait: the self-service grace cap (21 days). The SLA
#: clock (``sla_due_at``, 30 days) starts at the request, so a longer delay plus one retry
#: could run past Art. 12(3)'s one month.
MAX_SCHEDULED_DELAY = timedelta(days=MAX_GDPR_SELF_ERASURE_GRACE_DAYS)


def delay_for(db: Session, subject_type: str) -> timedelta:
    """The configured delay of an organization-admin erasure, clamped to its range."""
    raw = get_setting(db, _DELAY_KEYS[subject_type], None)
    try:
        hours = int(raw) if raw is not None else DEFAULT_GDPR_ORG_ERASURE_DELAY_HOURS
    except (TypeError, ValueError):
        hours = DEFAULT_GDPR_ORG_ERASURE_DELAY_HOURS
    hours = max(MIN_GDPR_ORG_ERASURE_DELAY_HOURS, min(hours, MAX_GDPR_ORG_ERASURE_DELAY_HOURS))
    return timedelta(hours=hours)


def request_digest(
    ledger_uuid: uuid_pkg.UUID, requested_at: datetime, scheduled_for: datetime
) -> str:
    """sha256 over the canonical request fields; a reference the requester can quote."""
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


def open_request(
    db: Session, subject_type: str, user_id: int | None, organization_id: int | None
) -> ErasureLedgerEntry | None:
    """The open scheduled erasure of this subject, if any (the v464 unique index's key)."""
    query = db.query(ErasureLedgerEntry).filter(
        ErasureLedgerEntry.subject_type == subject_type,
        ErasureLedgerEntry.status == "deferred",
        ErasureLedgerEntry.deferred_reason == "grace_period",
    )
    for column, value in (
        (ErasureLedgerEntry.subject_user_id, user_id),
        (ErasureLedgerEntry.subject_organization_id, organization_id),
    ):
        query = query.filter(column.is_(None) if value is None else column == value)
    return query.first()


def open_entries_for_organization(db: Session, organization_id: int) -> list[ErasureLedgerEntry]:
    """An organization's open scheduled erasures (its members' and its own), soonest first."""
    return (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.subject_type.in_(ORG_SUBJECT_TYPES),
            ErasureLedgerEntry.subject_organization_id == organization_id,
            ErasureLedgerEntry.status == "deferred",
            ErasureLedgerEntry.deferred_reason == "grace_period",
        )
        .order_by(ErasureLedgerEntry.scheduled_for.asc(), ErasureLedgerEntry.id.asc())
        .all()
    )


def find_organization_entry(
    db: Session, organization_id: int, ledger_uuid: uuid_pkg.UUID
) -> ErasureLedgerEntry | None:
    """A scheduled erasure of this organization (or one of its members) by uuid, else None."""
    return (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.uuid == ledger_uuid,
            ErasureLedgerEntry.subject_type.in_(ORG_SUBJECT_TYPES),
            ErasureLedgerEntry.subject_organization_id == organization_id,
        )
        .first()
    )


def member_of_organization(db: Session, organization_id: int, user_uuid: str) -> User:
    """The user ``user_uuid`` if they are a member of the organization.

    Raises:
        ScheduledErasureError: ``user_not_found`` or ``not_a_member``.
    """
    from app.models.organization import OrganizationMembership

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if user is None:
        raise ScheduledErasureError("user_not_found", "User not found")
    membership = (
        db.query(OrganizationMembership.user_id)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    if membership is None:
        raise ScheduledErasureError("not_a_member", "User is not a member of your organization")
    return user


def requester_names(
    db: Session, organization_id: int, entries: list[ErasureLedgerEntry]
) -> dict[int, str]:
    """Display names of the admins who requested ``entries``, resolved now.

    Only for requesters who are still members of this organization: the ledger keeps no names,
    and an organization admin must not learn another tenant's labels through this list.
    """
    from app.models.organization import OrganizationMembership

    actor_ids = {int(e.actor_user_id) for e in entries if e.actor_user_id is not None}
    if not actor_ids:
        return {}
    rows = (
        db.query(User.id, User.full_name, User.email)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .filter(
            OrganizationMembership.organization_id == organization_id,
            User.id.in_(actor_ids),
        )
        .all()
    )
    return {int(r.id): str(r.full_name or r.email) for r in rows}


def schedule_for_organization(
    db: Session,
    *,
    organization_id: int,
    subject_type: str,
    actor: User,
    subject_user: User | None = None,
    client: dict[str, Any] | None = None,
) -> ErasureLedgerEntry:
    """An organization admin's request: :func:`schedule` with the configured delay."""
    from app.models.organization import Organization

    return schedule(
        db,
        subject_type=subject_type,
        actor=actor,
        actor_kind="org_admin",
        delay=delay_for(db, subject_type),
        subject_user=subject_user,
        organization=db.get(Organization, organization_id),
        client=client,
    )


def _stage(
    db: Session,
    event: AuditEventType,
    *,
    actor_user_id: int | None,
    subject_type: str,
    entry_uuid: uuid_pkg.UUID,
    subject_user_id: int | None,
    organization_id: int | None,
    client: dict[str, Any] | None,
    details: dict[str, Any],
) -> None:
    """Stage the governance event in ``db``'s transaction (the outbox, v459): it commits or rolls
    back with the ledger change it describes. Ids, codes and times only.

    ``client`` is the API layer's ``middleware.audit.get_request_context`` (address and agent of
    the request), or None for the executor.
    """
    ctx = client or {}
    audit_outbox.stage(
        db,
        event_type=event,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor_user_id,
        source_ip=ctx.get("source_ip"),
        user_agent=ctx.get("user_agent"),
        organization_id=organization_id,
        target_user_id=subject_user_id,
        resource_type="erasure_request",
        resource_uuid=str(entry_uuid),
        tenant_scope=TENANT_SCOPE_ORG if organization_id is not None else TENANT_SCOPE_PERSONAL,
        details={"subject_type": subject_type, **details},
    )


def schedule(
    db: Session,
    *,
    subject_type: str,
    actor: User,
    actor_kind: str,
    delay: timedelta,
    subject_user: User | None = None,
    organization: Organization | None = None,
    now: datetime | None = None,
    client: dict[str, Any] | None = None,
) -> ErasureLedgerEntry:
    """Record a scheduled erasure: a ``deferred`` / ``grace_period`` ledger entry and its
    ``gdpr.erasure.scheduled`` event, committed together. Nothing is destroyed now.

    Raises:
        ScheduledErasureError: ``request_exists`` (carries the open entry) or
            ``ledger_unavailable``. Nothing is partially created on a refusal.
        ValueError: a delay outside ``0..MAX_SCHEDULED_DELAY`` (a programming error: every
            caller clamps its setting first).
    """
    if delay < timedelta(0) or delay > MAX_SCHEDULED_DELAY:
        raise ValueError(f"scheduled erasure delay {delay} is outside 0..{MAX_SCHEDULED_DELAY}")
    user_id = int(subject_user.id) if subject_user is not None else None
    org_id = int(organization.id) if organization is not None else None
    existing = open_request(db, subject_type, user_id, org_id)
    if existing is not None:
        raise ScheduledErasureError(
            "request_exists", "An erasure of this subject is already scheduled.", entry=existing
        )

    requested_at = now or datetime.now(UTC)
    scheduled_for = requested_at + delay
    entry_uuid = uuid7()
    _stage(
        db,
        AuditEventType.ERASURE_SCHEDULED,
        actor_user_id=int(actor.id),
        subject_type=subject_type,
        entry_uuid=entry_uuid,
        subject_user_id=user_id,
        organization_id=org_id,
        client=client,
        details={"actor_kind": actor_kind, "scheduled_for": scheduled_for.isoformat()},
    )
    entry = ledger.record_request(
        db,
        subject_type=subject_type,
        subject_user_id=user_id,
        subject_user_uuid=subject_user.uuid if subject_user is not None else None,
        subject_organization_id=org_id,
        subject_organization_uuid=organization.uuid if organization is not None else None,
        actor_kind=actor_kind,
        actor_user_id=int(actor.id),
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=scheduled_for,
        request_digest=request_digest(entry_uuid, requested_at, scheduled_for),
        requested_at=requested_at,
        entry_uuid=entry_uuid,
    )
    audit_outbox.deliver_staged(db)
    if entry is None:
        # record_request swallows the unique-index race as well as real failures, and its
        # rollback took the staged event with it.
        existing = open_request(db, subject_type, user_id, org_id)
        if existing is not None:
            raise ScheduledErasureError(
                "request_exists", "An erasure of this subject is already scheduled.", entry=existing
            )
        raise ScheduledErasureError("ledger_unavailable", "The request could not be recorded.")
    return entry


def cancel(
    db: Session,
    entry: ErasureLedgerEntry,
    *,
    actor: User,
    now: datetime | None = None,
    client: dict[str, Any] | None = None,
) -> ErasureLedgerEntry:
    """Withdraw a scheduled erasure before ``scheduled_for``, with its ``gdpr.erasure.cancelled``
    event in the same commit. The caller has already decided ``actor`` may.

    Raises:
        ScheduledErasureError: ``already_executing`` once the time has come or the erasure has
            started, ``not_cancellable`` for any other state.
    """
    current = now or datetime.now(UTC)
    if entry.status == "deferred" and entry.deferred_reason == "grace_period":
        if entry.scheduled_for is not None and current >= entry.scheduled_for:
            raise ScheduledErasureError("already_executing", "The delay is over.", entry=entry)
        _stage(
            db,
            AuditEventType.ERASURE_CANCELLED,
            actor_user_id=int(actor.id),
            subject_type=entry.subject_type,
            entry_uuid=entry.uuid,
            subject_user_id=entry.subject_user_id,
            organization_id=entry.subject_organization_id,
            client=client,
            details={"actor_kind": entry.actor_kind},
        )
        cancelled = ledger.cancel_request(db, entry, now=current)
        audit_outbox.deliver_staged(db)
        return cancelled
    if entry.status in ("pending", "deferred", "complete", "failed"):
        raise ScheduledErasureError("already_executing", "The erasure has started.", entry=entry)
    raise ScheduledErasureError("not_cancellable", "This request cannot be cancelled.", entry=entry)


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
        user_id = _required_id(entry.subject_user_id, "subject_user_id")
        # Checked again on EVERY run, not only the first: a retry (a hold released, a restore
        # re-erasure) can come long after the other super administrators were demoted or erased.
        if _would_remove_last_super_admin(db, user_id):
            ledger.block_request(db, entry, reason="last_super_admin")
            logger.warning("Erasure %s blocked: last active super administrator", entry.uuid)
            return {"complete": False, "blocked": "last_super_admin", "legal_holds_skipped": 0}
        return erasure.erase_user(db, user_id, ledger_entry=entry)
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
    """Flip one due entry to ``pending`` and run its erasure. False when it was not due.

    The row lock serialises the hourly tick and the daily sweep: a second executor waits, then
    sees ``pending`` and leaves it.
    """
    entry = db.get(ErasureLedgerEntry, entry_id, with_for_update=True)
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
    _stage(
        db,
        AuditEventType.ERASURE_EXECUTION_STARTED,
        actor_user_id=None,
        subject_type=entry.subject_type,
        entry_uuid=entry.uuid,
        subject_user_id=entry.subject_user_id,
        organization_id=entry.subject_organization_id,
        client=None,
        details={"actor_kind": entry.actor_kind},
    )
    entry.status = "pending"
    entry.deferred_reason = None
    db.commit()
    audit_outbox.deliver_staged(db)
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
        except Exception as e:  # boundary: per-item isolation, counted in failures
            failures += 1
            logger.exception("Scheduled erasure failed for ledger entry %s: %s", entry_id, e)
    return {"due": len(due_ids), "executed": executed, "failures": failures}
