"""Self-service erasure requests and the index-level reconciliation cursor (#1354, #1355).

A self-service request IS an ``erasure_ledger`` entry: ``status='deferred'``,
``deferred_reason='grace_period'`` and a ``scheduled_for`` time, so there is one SLA clock and one
journal and no second table that could disagree about whether a subject is being erased. Cancelling
sets the new terminal status ``cancelled``.

Adds ``scheduled_for``, ``cancelled_at``, ``request_digest`` (sha256 hex, CHECKed) and
``index_checked_at`` (the cursor of the index-level resurrection check); widens the status and
deferral-reason CHECKs; and adds two partial indexes: ``uq_erasure_ledger_open_self_request`` (one
open request per user, race-safe) and ``ix_erasure_ledger_due_grace`` (the executor's query).

Chained after ``v452_add_erasure_receipt`` because the append-only (v453) and consent (v454) lanes
were built in parallel with the same parent; the coordinator re-chains at merge.

All SQL is idempotent. The downgrade REFUSES while any request is still open (it would otherwise
silently turn a pending erasure into a failed CHECK or a forgotten one); otherwise it deletes the
cancelled entries (their status cannot exist on the old CHECK) and removes what this revision added.
"""

from alembic import op

revision = "v455_add_self_service_erasure_request"
down_revision = "v454_add_media_file_voiceprint_consent"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
    ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;
    ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
    ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS request_digest VARCHAR(64);
    ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS index_checked_at TIMESTAMPTZ;

    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_status;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_status
        CHECK (status IN ('pending', 'complete', 'deferred', 'failed', 'cancelled'));
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_deferred_reason;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_deferred_reason
        CHECK (deferred_reason IS NULL
               OR deferred_reason IN ('legal_hold', 'error', 'grace_period'));
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_request_digest;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_request_digest
        CHECK (request_digest IS NULL OR request_digest ~ '^[0-9a-f]{64}$');

    CREATE INDEX IF NOT EXISTS ix_erasure_ledger_due_grace ON erasure_ledger (scheduled_for)
        WHERE status = 'deferred' AND deferred_reason = 'grace_period';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_erasure_ledger_open_self_request
        ON erasure_ledger (subject_user_id)
        WHERE deferred_reason = 'grace_period' AND status = 'deferred';
"""

REFUSE_SQL = """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM erasure_ledger
                   WHERE status = 'deferred' AND deferred_reason = 'grace_period') THEN
            RAISE EXCEPTION 'Cannot downgrade v455: self-service erasure requests are still open. '
                'Complete or cancel them first (GET /api/users/me/erasure-request).';
        END IF;
    END $$;
"""

DOWNGRADE_SQL = (
    REFUSE_SQL
    + """
    DROP INDEX IF EXISTS uq_erasure_ledger_open_self_request;
    DROP INDEX IF EXISTS ix_erasure_ledger_due_grace;
    DELETE FROM erasure_ledger WHERE status = 'cancelled';
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_request_digest;
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_deferred_reason;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_deferred_reason
        CHECK (deferred_reason IS NULL OR deferred_reason IN ('legal_hold', 'error'));
    ALTER TABLE erasure_ledger DROP CONSTRAINT IF EXISTS ck_erasure_ledger_status;
    ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_status
        CHECK (status IN ('pending', 'complete', 'deferred', 'failed'));
    ALTER TABLE erasure_ledger DROP COLUMN IF EXISTS index_checked_at;
    ALTER TABLE erasure_ledger DROP COLUMN IF EXISTS request_digest;
    ALTER TABLE erasure_ledger DROP COLUMN IF EXISTS cancelled_at;
    ALTER TABLE erasure_ledger DROP COLUMN IF EXISTS scheduled_for;
"""
)


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
