"""Write and finalise :class:`ErasureLedgerEntry` rows (GDPR Art. 17 / Art. 30, issue #442).

The only module that writes the erasure ledger. Two calls bracket every erasure:

    entry = record_request(db, subject=..., actor_kind=...)   # committed FIRST
    ...destructive work...
    record_outcome(db, entry, summary)

**The commit ordering is the point, not an implementation detail.** The entry is written
and committed *before* anything is destroyed, so a worker that dies mid-erasure leaves a
``pending`` row behind. ``tasks/erasure_reconciliation`` re-runs the (idempotent)
erasure for it on the next tick. Recording the outcome afterwards instead would mean the
one case that most needs a record — the crash — is the one case with no record.

What must NOT be recorded
-------------------------
A ledger that holds the erased personal data is not erasure. ``models/erasure`` gives
the table no free-text column at all, and this module is the matching half:

- ``_numeric_counters`` keeps only integer-valued keys from the erasure summary. In
  particular it drops ``summary["errors"]`` entirely — those entries carry file UUIDs
  and raw driver messages, which quote storage paths and filenames. Only ``error_count``
  survives, and that is a number.
- Nothing here ever reads ``user.email`` or ``organization.name``. The subject is
  identified by ``id`` + ``uuid`` only. There is no email hash: a hash of a value drawn
  from a guessable space is pseudonymous personal data (Recital 26), so keeping one
  would re-create the thing the erasure destroyed.

The database enforces both halves independently — ``ck_erasure_ledger_counters_numeric``
rejects a non-numeric ``counters`` value even if this module were changed to send one.

The journal, and why the ledger cannot only be a table
------------------------------------------------------
``erasure_ledger`` lives in Postgres, so **restoring a dump taken before an erasure
destroys the record of that erasure along with the erasure itself**. A ledger that a
restore can roll back cannot be the thing that detects a restore rolling data back —
it would have to survive its own failure mode.

So every entry is also appended to a line-delimited journal on the data volume
(:func:`journal_path`), which is outside the database and therefore outside the dump.
:func:`restore_from_journal` re-creates any journalled entry the database no longer
has, as ``pending``, and the reconciliation sweep then re-runs the idempotent erasure.
That is the path that makes "the restore consults the ledger" true.

Its limits, stated rather than implied: the journal is one file on one volume. Losing
that volume, or restoring it from the same point in time as the database, loses the
same information. Replicating it off-host is deployment configuration, not code — the
audit stream (stdout / OpenSearch) is the second copy that already leaves the host.
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.models.erasure import ErasureLedgerEntry

logger = logging.getLogger(__name__)

#: GDPR Art. 12(3): erasure must complete within one month of the request. Lives here
#: rather than in ``gdpr_erasure_service`` so the value that stamps ``sla_due_at`` and
#: the value reported to the caller cannot drift apart.
ERASURE_SLA_DAYS = 30

#: Journal filename under ``DATA_DIR``. Append-only, one JSON object per line.
JOURNAL_RELATIVE_PATH = Path("gdpr") / "erasure-journal.jsonl"


def journal_path() -> Path:
    """Absolute path of the out-of-database erasure journal.

    Resolved through ``settings`` on every call rather than at import, so a test can
    point ``DATA_DIR`` somewhere disposable without the module having already frozen it.
    """
    from app.core.config import settings

    return Path(settings.DATA_DIR) / JOURNAL_RELATIVE_PATH


def _journal_record(entry: ErasureLedgerEntry) -> dict[str, Any]:
    """The journal line for an entry — surrogate keys and numbers only.

    Same rule as the table: no email, no filename, no error text. What is here is
    exactly what :func:`restore_from_journal` needs to re-open the entry, and nothing
    that would make the journal a copy of the data the erasure destroyed.
    """
    return {
        "uuid": str(entry.uuid),
        "subject_type": entry.subject_type,
        "subject_user_id": entry.subject_user_id,
        "subject_user_uuid": str(entry.subject_user_uuid) if entry.subject_user_uuid else None,
        "subject_organization_id": entry.subject_organization_id,
        "subject_organization_uuid": (
            str(entry.subject_organization_uuid) if entry.subject_organization_uuid else None
        ),
        "status": entry.status,
        "actor_kind": entry.actor_kind,
        "requested_at": entry.requested_at.isoformat() if entry.requested_at else None,
        "sla_due_at": entry.sla_due_at.isoformat() if entry.sla_due_at else None,
    }


def _append_journal(entry: ErasureLedgerEntry) -> None:
    """Append one line to the journal. Never raises.

    An unwritable journal must not abort an erasure — the deletion is the legally
    binding act — but it is logged at ERROR, because an erasure with no journal line is
    one a backup restore can silently undo.
    """
    try:
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_journal_record(entry), separators=(",", ":")) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Erasure journal append FAILED for entry %s: %s — a restore of an older "
            "database would silently undo this erasure",
            entry.uuid,
            e,
        )


def restore_from_journal(db: Session) -> int:
    """Re-open journalled entries the database no longer has. Returns how many.

    The reconciliation sweep calls this first. After a restore of a dump taken *before*
    an erasure, the subject's rows are back **and** the ledger row that recorded the
    erasure is gone — the one case an in-database ledger cannot detect about itself.
    The journal is on the data volume, not in the dump, so it still has the entry.

    Re-created as ``pending`` with the ORIGINAL ``uuid``, ``requested_at`` and
    ``sla_due_at``: the request was made when it was made, and a restore must not reset
    the Art. 12(3) clock. Existing rows are never touched, so this is idempotent and
    safe to run on every tick.
    """
    path = journal_path()
    if not path.exists():
        return 0

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:  # noqa: BLE001
        logger.error("Erasure journal unreadable at %s: %s", path, e)
        return 0

    known = {
        str(u)
        for (u,) in db.query(ErasureLedgerEntry.uuid).all()  # one column, not whole rows
    }
    restored = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            logger.warning("Skipping unparseable erasure journal line")
            continue
        if record.get("uuid") in known:
            continue
        try:
            db.add(
                ErasureLedgerEntry(
                    uuid=uuid_pkg.UUID(record["uuid"]),
                    subject_type=record["subject_type"],
                    subject_user_id=record.get("subject_user_id"),
                    subject_user_uuid=(
                        uuid_pkg.UUID(record["subject_user_uuid"])
                        if record.get("subject_user_uuid")
                        else None
                    ),
                    subject_organization_id=record.get("subject_organization_id"),
                    subject_organization_uuid=(
                        uuid_pkg.UUID(record["subject_organization_uuid"])
                        if record.get("subject_organization_uuid")
                        else None
                    ),
                    status="pending",
                    actor_kind=record.get("actor_kind") or "system",
                    requested_at=datetime.fromisoformat(record["requested_at"]),
                    sla_due_at=datetime.fromisoformat(record["sla_due_at"]),
                    counters={},
                )
            )
            db.commit()
            known.add(record["uuid"])
            restored += 1
        except Exception as e:  # noqa: BLE001 — one bad line must not stop the rest
            db.rollback()
            logger.error("Could not restore erasure journal entry %s: %s", record.get("uuid"), e)

    if restored:
        logger.error(
            "GDPR: %d erasure record(s) were missing from the database and have been "
            "restored from the journal — this deployment's database was rolled back "
            "past an Art. 17 erasure",
            restored,
        )
    return restored


def _numeric_counters(summary: dict[str, Any]) -> dict[str, int]:
    """Keep only the integer counters from an erasure summary.

    Everything else is dropped, and the drop is the security property rather than
    tidiness: ``summary["errors"]`` holds file UUIDs and driver messages that can quote
    filenames and storage paths, ``subject``/``organization_id`` are already columns,
    and ``complete``/``already_erased`` are booleans (``bool`` is an ``int`` subclass in
    Python but ``True`` would land in JSONB as a JSON boolean, which the CHECK rejects —
    so they are excluded explicitly rather than left to fail at INSERT).

    Args:
        summary: The dict an ``erase_*`` function returns.

    Returns:
        ``{key: int}`` for every key whose value is a plain non-boolean integer.
    """
    return {
        key: int(value)
        for key, value in summary.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def record_request(
    db: Session,
    *,
    subject_type: str,
    subject_user_id: int | None = None,
    subject_user_uuid: uuid_pkg.UUID | None = None,
    subject_organization_id: int | None = None,
    subject_organization_uuid: uuid_pkg.UUID | None = None,
    actor_kind: str = "system",
    actor_user_id: int | None = None,
) -> ErasureLedgerEntry | None:
    """Open a ledger entry and COMMIT it before any data is destroyed.

    Never raises. A ledger write that fails must not abort the erasure — the deletion is
    the legally binding act and has to proceed — but the failure is logged loudly,
    because the resulting erasure is one nothing will ever reconcile.

    Args:
        subject_type: One of ``models.erasure.SUBJECT_TYPES``.
        subject_user_id / subject_user_uuid: The data subject's surrogate keys. Both,
            because a restored dump resets the id sequence and a later account can be
            issued an id this subject once held.
        subject_organization_id / subject_organization_uuid: The tenant's, for the org
            and org-member scopes.
        actor_kind: One of ``models.erasure.ACTOR_KINDS`` — a category, so the entry
            stays meaningful after the actor's own account is deleted.
        actor_user_id: The acting staff member, when there is one.

    Returns:
        The committed entry, or ``None`` if it could not be written.
    """
    now = datetime.now(UTC)
    entry = ErasureLedgerEntry(
        subject_type=subject_type,
        subject_user_id=subject_user_id,
        subject_user_uuid=subject_user_uuid,
        subject_organization_id=subject_organization_id,
        subject_organization_uuid=subject_organization_uuid,
        status="pending",
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        requested_at=now,
        sla_due_at=now + timedelta(days=ERASURE_SLA_DAYS),
        counters={},
    )
    try:
        db.add(entry)
        db.commit()
        db.refresh(entry)
    except Exception as e:  # noqa: BLE001 — a ledger failure must not abort the erasure
        db.rollback()
        logger.error(
            "Erasure ledger write FAILED for %s subject user=%s org=%s: %s — the erasure "
            "will proceed but nothing will reconcile it",
            subject_type,
            subject_user_id,
            subject_organization_id,
            e,
        )
        return None

    _append_journal(entry)
    _audit(entry, action="gdpr_erasure_requested", outcome=AuditOutcome.SUCCESS)
    return entry


def record_outcome(
    db: Session,
    entry: ErasureLedgerEntry | None,
    summary: dict[str, Any],
) -> ErasureLedgerEntry | None:
    """Close (or defer) a ledger entry from the erasure summary. Never raises.

    Status is derived, not passed in, so the ledger cannot disagree with the summary the
    caller received:

    - ``legal_holds_skipped > 0`` → ``deferred`` / ``legal_hold``. The sweep retries it.
    - any other error → ``deferred`` / ``error``. Also retried: a transient object-store
      outage is exactly the case where "we tried once" is not an erasure.
    - otherwise → ``complete``, with ``completed_at`` set.

    ``deferred`` rather than ``failed`` in both cases because ``failed`` would read as
    terminal, and no erasure state here is terminal until the data is gone.
    """
    if entry is None:
        return None

    now = datetime.now(UTC)
    holds = int(summary.get("legal_holds_skipped") or 0)
    errors = summary.get("errors") or []

    if holds:
        status, reason = "deferred", "legal_hold"
    elif errors:
        status, reason = "deferred", "error"
    else:
        status, reason = "complete", None

    try:
        entry.status = status
        entry.deferred_reason = reason
        entry.legal_holds_outstanding = holds
        entry.error_count = len(errors)
        entry.counters = _numeric_counters(summary)
        entry.attempts = int(entry.attempts or 0) + 1
        entry.last_attempt_at = now
        entry.completed_at = now if status == "complete" else None
        db.commit()
        db.refresh(entry)
    except Exception as e:  # noqa: BLE001 — the deletion already happened; never re-raise
        db.rollback()
        logger.error("Erasure ledger outcome write FAILED for entry %s: %s", entry.uuid, e)
        return entry

    _audit(
        entry,
        action="gdpr_erasure_recorded",
        outcome=AuditOutcome.SUCCESS if status == "complete" else AuditOutcome.PARTIAL,
    )
    return entry


def record_resurrection(db: Session, entry: ErasureLedgerEntry) -> None:
    """Note that a subject this entry says was erased has been found alive again.

    Counted rather than silently re-erased: a resurrection means a restore replayed data
    past an Art. 17 erasure, which is a compliance incident an operator needs to see even
    once the sweep has cleaned it up. Never raises.
    """
    try:
        entry.resurrections_detected = int(entry.resurrections_detected or 0) + 1
        entry.last_resurrection_at = datetime.now(UTC)
        entry.status = "pending"
        entry.completed_at = None
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.error("Erasure ledger resurrection write FAILED for entry %s: %s", entry.uuid, e)
        return

    logger.warning(
        "GDPR: erased subject is present again (ledger %s, %s) — a restore replayed data "
        "past an Art. 17 erasure; re-erasing",
        entry.uuid,
        entry.subject_type,
    )
    _audit(entry, action="gdpr_erasure_resurrection_detected", outcome=AuditOutcome.FAILURE)


def open_entries(db: Session, *, limit: int) -> list[ErasureLedgerEntry]:
    """Entries the reconciliation sweep still owes work on, oldest request first.

    Everything that is not ``complete``: ``pending`` (including the row a crashed worker
    left behind), ``deferred`` (a legal hold or a store outage), and ``failed``.
    """
    return (
        db.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.status != "complete")
        .order_by(ErasureLedgerEntry.requested_at.asc())
        .limit(limit)
        .all()
    )


def completed_entries(db: Session, *, limit: int) -> list[ErasureLedgerEntry]:
    """Finished entries, newest first — the sweep's resurrection-check population."""
    return (
        db.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.status == "complete")
        .order_by(ErasureLedgerEntry.completed_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def overdue_entries(db: Session, *, now: datetime | None = None) -> list[ErasureLedgerEntry]:
    """Open entries past their Art. 12(3) one-month deadline.

    The SLA was previously a constant echoed into an API response and measured by
    nothing. This is the measurement.
    """
    cutoff = now or datetime.now(UTC)
    return (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.status != "complete",
            ErasureLedgerEntry.sla_due_at < cutoff,
        )
        .order_by(ErasureLedgerEntry.sla_due_at.asc())
        .all()
    )


def _audit(entry: ErasureLedgerEntry, *, action: str, outcome: AuditOutcome) -> None:
    """Emit the ledger event to the audit trail. Never raises.

    The audit stream is written to stdout and (optionally) OpenSearch — **neither of
    which is inside the Postgres dump**. That makes it the copy of the ledger that
    survives restoring a backup taken before the erasure, which is the failure the ledger
    exists to catch and which a purely in-database record could not catch about itself.

    Carries surrogate keys only, for the same reason the table has no free-text column.

    ⚠️ **``user_id`` is the SUBJECT, never the actor**, and the actor goes in
    ``details.actor_user_id``. These records share ``ADMIN_USER_DELETE`` with the
    erasure record ``gdpr_erasure_service`` emits between them, which has always put
    the target in ``user_id`` — so a first version of this function that used the
    actor gave one event type two opposite meanings for one field, in the same
    three-record sequence. That is issue #443's ambiguity made concrete: "which
    erasures touched user X" would have returned the middle record and missed the
    two around it, and "which erasures did admin Y run" the reverse. Whatever #443
    settles on, these three must move together.
    """
    try:
        audit_logger.log(
            event_type=AuditEventType.ADMIN_USER_DELETE,
            outcome=outcome,
            user_id=entry.subject_user_id,
            organization_id=entry.subject_organization_id,
            details={
                "action": action,
                "ledger_uuid": str(entry.uuid),
                "subject_type": entry.subject_type,
                "subject_user_id": entry.subject_user_id,
                "subject_user_uuid": str(entry.subject_user_uuid)
                if entry.subject_user_uuid
                else None,
                "subject_organization_id": entry.subject_organization_id,
                "status": entry.status,
                "actor_kind": entry.actor_kind,
                "actor_user_id": entry.actor_user_id,
                "attempts": entry.attempts,
                "sla_due_at": entry.sla_due_at.isoformat() if entry.sla_due_at else None,
            },
        )
    except Exception as e:  # noqa: BLE001 — audit must never break the erasure
        logger.warning("Erasure ledger audit event failed: %s", e)
