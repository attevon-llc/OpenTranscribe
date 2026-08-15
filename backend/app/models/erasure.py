"""GDPR Art. 17 / Art. 30 erasure ledger — the durable record that an erasure happened.

Before this table the erasure path destroyed data and returned a summary dict, and
that was the whole story: nothing recorded that a request had been made, by whom,
when, or what it covered. Art. 17 requires the erasure; **Art. 30(1) requires you to
be able to demonstrate it**, and Art. 12(3)'s one-month deadline cannot be measured
against a number that exists only in a docstring.

Three jobs, one table
---------------------
1. **Demonstrability.** One row per erasure request, with the SLA clock on it.
2. **Deferred work.** A file under a legal hold (Art. 17(3)(e)) is skipped, and the
   ``user`` row cannot be deleted while it exists — ``media_file.user_id`` is a plain
   ``NO ACTION`` FK, so ``DELETE FROM "user"`` would raise. The erasure therefore
   lands in ``deferred``, and ``tasks/erasure_reconciliation.py`` finishes it when the
   hold lifts. Without a row here that deferral is simply forgotten, forever.
3. **Restore reconciliation.** Restoring a dump taken before an erasure resurrects the
   subject. The sweep re-checks every ``complete`` entry against the live schema and
   re-erases a subject that is back.

**The trap this schema is built around: a ledger that holds the erased personal data
is not erasure.** "We erased alice@example.com" *containing* the address is a copy of
the thing that was supposed to be destroyed, in a table designed to outlive it. So:

- **There is no free-text column at all.** Every textual column is a short enum with a
  DB ``CHECK`` behind it (``subject_type``, ``status``, ``actor_kind``, ``deferred_reason``).
  There is nowhere to put an email even by accident, and
  ``tests/unit/test_erasure_ledger.py`` fails if a future column adds one.
- The subject is named by **surrogate keys only** (``id`` + ``uuid``). Those are
  meaningless once the row they point at is destroyed — which is exactly the property
  that makes them safe to keep *and* the property that makes them work for restore
  detection: they become meaningful again precisely when, and only when, a restore
  brings the row back.
- **No email hash either.** A hash of a value drawn from a guessable space is
  pseudonymisation, not anonymisation (Recital 26); it stays personal data and a
  dictionary attack recovers it. Keeping one would defeat the point.
- ``counters`` is JSONB, so it *is* a hole — and it is nailed shut in the database:
  ``ck_erasure_ledger_counters_numeric`` rejects any value that is not a JSON number,
  which means a filename, an error message or an address cannot be stored there even
  by a caller that tries.

Community-edition invariance: the table is empty until someone requests an erasure,
the sweep is a no-op against an empty table, and ``subject_organization_id`` is NULL
for every self-host row.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

#: What was asked to be erased. ``org_member`` is the org-admin scope (one member's
#: rows inside one tenant); ``user`` is the whole account; ``organization`` the tenant.
SUBJECT_TYPES = ("user", "org_member", "organization")

#: ``pending`` — recorded, destructive work not yet finished (this is also what a
#: process that died mid-erasure leaves behind, which is why it is the initial value).
#: ``complete`` — nothing survived. ``deferred`` — a legal hold stopped part of it and
#: the sweep must retry. ``failed`` — something else stopped it; also retried.
LEDGER_STATUSES = ("pending", "complete", "deferred", "failed")

#: Who asked. Recorded as a *category* rather than a name so the entry stays
#: meaningful after the actor's own account is deleted, and so an org admin's identity
#: is not retained in a compliance record they are not the subject of.
ACTOR_KINDS = ("data_subject", "super_admin", "org_admin", "system")

#: Why the erasure could not finish. An enum, not a message — see the module docstring.
DEFERRAL_REASONS = ("legal_hold", "error")


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a','b')`` CHECK body from the tuple above.

    The tuples are the single source of truth for both the ORM constraint and the
    migration, in the same shape ``models/group.MAPPING_SOURCES`` established — a
    hand-copied second list is how a CHECK and its Python validator drift apart.
    """
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


#: Rejects any ``counters`` value that is not a JSON number. ``$.*`` walks the top
#: level; an object or array value fails too (its ``type()`` is not ``number``), so
#: nesting is not an escape hatch. ``jsonb_path_exists/2`` is IMMUTABLE, which is what
#: makes it legal in a CHECK — a subquery would not be.
#:
#: ⚠️ Written as ``$.*?(`` with NO space before the filter, because that is how
#: Postgres normalizes and stores a ``jsonpath``. The natural spelling
#: ``$.* ? (@.type() ...)`` is accepted and means the same thing, but comes back out
#: of ``pg_get_constraintdef`` without the spaces — and
#: ``tests/unit/test_orm_ddl_divergence.py`` compares the two texts, so the readable
#: form fails the drift gate for a difference that is purely Postgres's formatting.
#: Match what the database stores rather than allowlisting the mismatch.
COUNTERS_NUMERIC_SQL = """NOT jsonb_path_exists(counters, '$.*?(@.type() != "number")')"""

#: The subject has to be identified in a way the reconciliation sweep can act on.
SUBJECT_IDENTIFIED_SQL = (
    "(subject_type = 'user' AND subject_user_id IS NOT NULL) OR "
    "(subject_type = 'organization' AND subject_organization_id IS NOT NULL) OR "
    "(subject_type = 'org_member' AND subject_user_id IS NOT NULL "
    "AND subject_organization_id IS NOT NULL)"
)


class ErasureLedgerEntry(Base):
    """One Art. 17 erasure request: what was asked, when, and whether it finished."""

    __tablename__ = "erasure_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The receipt. Returned to the caller in the erasure summary so a data subject or
    #: a regulator can be given a reference that is not a database row id.
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )

    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    #: **Deliberately not a foreign key.** The row it names is the one being destroyed:
    #: a real FK would either block the delete (NO ACTION) or null the column on
    #: cascade (SET NULL) — and nulling it destroys the only key the restore-detection
    #: sweep has. Referential integrity is exactly the property this column must not have.
    subject_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: Carried alongside the id because a restored dump resets the id sequence, so a
    #: *new* account can later be issued an id an erased subject once held. Matching on
    #: both means resurrection detection cannot fire on an innocent bystander.
    subject_user_uuid: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    subject_organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    subject_organization_uuid: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="system")
    #: The acting staff member, ``ON DELETE SET NULL`` like every other actor FK since
    #: ``v387``: erasing an admin must not be blocked by, or destroy, the compliance
    #: record of an erasure they once performed. ``actor_kind`` survives the NULL.
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: ``requested_at + ERASURE_SLA_DAYS``. Stored rather than computed so the deadline
    #: an entry was created under survives a later change to the constant — and so the
    #: SLA is a value something can be *measured* against instead of a number echoed
    #: into an API response.
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    deferred_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    legal_holds_outstanding: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Numbers only — enforced by ``ck_erasure_ledger_counters_numeric``. The erasure
    #: summary's ``errors`` list is deliberately NOT stored: its entries carry file
    #: UUIDs and driver messages that can quote paths and filenames.
    counters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    #: How many times a subject this entry says was erased has been found alive again
    #: (a restored backup). Non-zero is a compliance incident worth surfacing, so it is
    #: counted rather than silently re-erased.
    resurrections_detected: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_resurrection_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_sql_in("subject_type", SUBJECT_TYPES), name="ck_erasure_ledger_subject"),
        CheckConstraint(_sql_in("status", LEDGER_STATUSES), name="ck_erasure_ledger_status"),
        CheckConstraint(_sql_in("actor_kind", ACTOR_KINDS), name="ck_erasure_ledger_actor_kind"),
        CheckConstraint(
            f"deferred_reason IS NULL OR {_sql_in('deferred_reason', DEFERRAL_REASONS)}",
            name="ck_erasure_ledger_deferred_reason",
        ),
        CheckConstraint(COUNTERS_NUMERIC_SQL, name="ck_erasure_ledger_counters_numeric"),
        CheckConstraint(SUBJECT_IDENTIFIED_SQL, name="ck_erasure_ledger_subject_identified"),
        # The sweep's only query: everything that is not finished. Partial, because on
        # a healthy deployment nearly every row is 'complete' and therefore noise.
        Index(
            "ix_erasure_ledger_open",
            "status",
            postgresql_where=text("status <> 'complete'"),
        ),
        {"extend_existing": True},
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ErasureLedgerEntry {self.uuid} {self.subject_type} "
            f"status={self.status} attempts={self.attempts}>"
        )
