"""Audit guard follow-ups: widen the erasure_ledger lifecycle list, protect erasure_receipt.

Why
---
``v453`` froze the columns of ``erasure_ledger`` the application may UPDATE. ``v455`` then added
``cancelled_at`` (set when a self-service erasure request is withdrawn) and ``index_checked_at``
(the cursor of the index-level resurrection check). Both are written after insert, and the
guard refused those UPDATEs: a withdrawal failed and the reconcile cursor never advanced.
``scheduled_for`` and ``request_digest`` are only ever written at insert, so they stay frozen.

This revision re-creates the ``erasure_ledger`` guard triggers with the widened list (``v453``
is frozen and already applied on migrated databases, so it cannot be edited), and closes two
smaller gaps:

* ``erasure_receipt`` is the tamper-evidence record of an erasure but had no guard at all. It is
  insert-only, so it gets the row guard (UPDATE/DELETE) and the TRUNCATE guard.
* ``legal_acceptance`` has its own row guard (``v451``) but no TRUNCATE guard. It gets the same
  statement-level trigger the other audit tables carry.

Functions are not touched. Only triggers are dropped and re-created, so the change is brief and
no row is rewritten. Idempotent. The downgrade restores the ``v453`` list for ``erasure_ledger``
and removes the triggers this revision added; it destroys no data (a ledger row whose new
columns were already written stays as is).

Revision ID: v457_audit_guard_followups
Revises: v456_unique_object_keys
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v457_audit_guard_followups"
down_revision = "v456_unique_object_keys"
branch_labels = None
depends_on = None

#: Frozen copy of the generic-guard part of ``app.db.audit_protection`` at this revision:
#: (table, mutable, nulls). The consistency test asserts equality with the registry.
GUARDED = (
    (
        "erasure_ledger",
        "{status,completed_at,last_attempt_at,attempts,deferred_reason,"
        "legal_holds_outstanding,error_count,counters,resurrections_detected,"
        "last_resurrection_at,cancelled_at,index_checked_at}",
        "{actor_user_id}",
    ),
    ("erasure_receipt", "{}", "{}"),
)

#: Tables that keep their own row guard and only gain the TRUNCATE trigger. Created last: the
#: detection arm keys on it.
TRUNCATE_ONLY = ("legal_acceptance",)

#: The ``erasure_ledger`` arguments ``v453`` installed (restored by the downgrade).
V453_ERASURE_LEDGER = (
    "{status,completed_at,last_attempt_at,attempts,deferred_reason,"
    "legal_holds_outstanding,error_count,counters,resurrections_detected,"
    "last_resurrection_at}",
    "{actor_user_id}",
)


def _row_and_truncate(table: str, mutable: str, nulls: str) -> str:
    return f"""
DROP TRIGGER IF EXISTS trg_audit_guard_truncate_{table} ON {table};
CREATE TRIGGER trg_audit_guard_truncate_{table} BEFORE TRUNCATE ON {table}
  FOR EACH STATEMENT EXECUTE FUNCTION audit_row_guard('{mutable}', '{nulls}');
DROP TRIGGER IF EXISTS trg_audit_guard_{table} ON {table};
CREATE TRIGGER trg_audit_guard_{table} BEFORE UPDATE OR DELETE ON {table}
  FOR EACH ROW EXECUTE FUNCTION audit_row_guard('{mutable}', '{nulls}');
"""


def _truncate_only(table: str) -> str:
    return f"""
DROP TRIGGER IF EXISTS trg_audit_guard_truncate_{table} ON {table};
CREATE TRIGGER trg_audit_guard_truncate_{table} BEFORE TRUNCATE ON {table}
  FOR EACH STATEMENT EXECUTE FUNCTION audit_row_guard('{{}}', '{{}}');
"""


#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = "".join(_row_and_truncate(*g) for g in GUARDED) + "".join(
    _truncate_only(t) for t in TRUNCATE_ONLY
)

DOWNGRADE_SQL = (
    _row_and_truncate("erasure_ledger", *V453_ERASURE_LEDGER)
    + "DROP TRIGGER IF EXISTS trg_audit_guard_erasure_receipt ON erasure_receipt;"
    "DROP TRIGGER IF EXISTS trg_audit_guard_truncate_erasure_receipt ON erasure_receipt;"
    "DROP TRIGGER IF EXISTS trg_audit_guard_truncate_legal_acceptance ON legal_acceptance;"
)


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
