"""Add ``erasure_ledger`` — the durable record of a GDPR Art. 17 erasure request (#442).

Before this revision the erasure path destroyed data and returned a summary dict.
Nothing recorded that a request had been made, so three things followed:

  - **Art. 30(1) demonstrability was impossible.** The erasure happened; proving it
    happened did not.
  - **A legal-hold deferral was forgotten permanently.** ``_purge_files`` skips a file
    under an Art. 17(3)(e) hold, and because ``media_file.user_id`` is a plain
    ``NO ACTION`` FK the ``user`` row cannot be deleted while one exists. Nothing
    re-ran the erasure when the hold lifted, so the retention became indefinite.
  - **A backup restore resurrected erased subjects** with nothing to reconcile
    against.

One table closes all three: ``services/erasure_ledger_service`` writes an entry before
the destructive work starts (so a crash mid-erasure leaves a ``pending`` row rather
than nothing), and ``tasks/erasure_reconciliation`` re-runs the idempotent erasure for
every entry that is not ``complete`` — and for every ``complete`` entry whose subject
is alive again.

**The design constraint worth reading before altering this table: it must not contain
the personal data it records the destruction of.** Hence no free-text column anywhere —
every textual column is a short enum with a CHECK — surrogate keys instead of an email
(and no email *hash* either; a hash of a guessable value is pseudonymous personal data,
not anonymous), and ``ck_erasure_ledger_counters_numeric``, which makes the one JSONB
column physically incapable of holding a string.

Community edition: the table exists and stays empty until an erasure is requested; the
sweep is a no-op against it. ``subject_organization_id`` is NULL on every self-host row.

All SQL is idempotent — the startup runner stamps untracked databases by schema
fingerprint, so a revision routinely re-runs over its own partial output.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v389_add_erasure_ledger"
down_revision = "v388_add_user_group_organization_id"
branch_labels = None
depends_on = None

#: Module-level so ``tests/unit/test_v389_migration_consistency.py`` replays the real
#: statements instead of asserting on this file's source text (the shape ``v387``
#: established after ``v386`` shipped without one).
CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS erasure_ledger (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL UNIQUE,
        subject_type VARCHAR(20) NOT NULL,
        subject_user_id INTEGER,
        subject_user_uuid UUID,
        subject_organization_id INTEGER,
        subject_organization_uuid UUID,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        actor_kind VARCHAR(20) NOT NULL DEFAULT 'system',
        actor_user_id INTEGER,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        sla_due_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        last_attempt_at TIMESTAMPTZ,
        attempts INTEGER NOT NULL DEFAULT 0,
        deferred_reason VARCHAR(20),
        legal_holds_outstanding INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        counters JSONB NOT NULL DEFAULT '{}'::jsonb,
        resurrections_detected INTEGER NOT NULL DEFAULT 0,
        last_resurrection_at TIMESTAMPTZ
    );
"""

#: ``subject_user_id`` / ``subject_organization_id`` are deliberately NOT foreign keys —
#: they name rows this table exists to record the destruction of. ``actor_user_id`` IS
#: one, ``ON DELETE SET NULL``, matching the five actor FKs ``v387`` converted: erasing
#: an admin must neither be blocked by nor destroy a record of an erasure they ran.
ADD_ACTOR_FK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'erasure_ledger_actor_user_id_fkey'
        ) THEN
            ALTER TABLE erasure_ledger
                ADD CONSTRAINT erasure_ledger_actor_user_id_fkey
                FOREIGN KEY (actor_user_id) REFERENCES "user"(id) ON DELETE SET NULL;
        END IF;
    END $$;
"""

#: Six CHECKs. Five are enum bodies; the sixth is the one that makes the
#: no-personal-data rule a property of the database rather than of the calling code.
#: ``jsonb_path_exists/2`` is IMMUTABLE, which is why it is legal here — a subquery
#: (``NOT EXISTS (SELECT ... FROM jsonb_each(counters) ...)``) is not allowed in a CHECK.
ADD_CHECKS_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_erasure_ledger_subject') THEN
            ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_subject
                CHECK (subject_type IN ('user', 'org_member', 'organization'));
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_erasure_ledger_status') THEN
            ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_status
                CHECK (status IN ('pending', 'complete', 'deferred', 'failed'));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_erasure_ledger_actor_kind'
        ) THEN
            ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_actor_kind
                CHECK (actor_kind IN ('data_subject', 'super_admin', 'org_admin', 'system'));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_erasure_ledger_deferred_reason'
        ) THEN
            ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_deferred_reason
                CHECK (deferred_reason IS NULL OR deferred_reason IN ('legal_hold', 'error'));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_erasure_ledger_counters_numeric'
        ) THEN
            ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_counters_numeric
                CHECK (NOT jsonb_path_exists(counters, '$.*?(@.type() != "number")'));
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_erasure_ledger_subject_identified'
        ) THEN
            ALTER TABLE erasure_ledger ADD CONSTRAINT ck_erasure_ledger_subject_identified
                CHECK (
                    (subject_type = 'user' AND subject_user_id IS NOT NULL)
                    OR (subject_type = 'organization' AND subject_organization_id IS NOT NULL)
                    OR (subject_type = 'org_member' AND subject_user_id IS NOT NULL
                        AND subject_organization_id IS NOT NULL)
                );
        END IF;
    END $$;
"""

#: ``ix_erasure_ledger_open`` is the sweep's only query — partial, because on a healthy
#: deployment every row is 'complete' and the sweep never wants any of them.
ADD_INDEXES_SQL = """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_erasure_ledger_uuid ON erasure_ledger (uuid);
    CREATE INDEX IF NOT EXISTS ix_erasure_ledger_subject_user_id
        ON erasure_ledger (subject_user_id);
    CREATE INDEX IF NOT EXISTS ix_erasure_ledger_subject_user_uuid
        ON erasure_ledger (subject_user_uuid);
    CREATE INDEX IF NOT EXISTS ix_erasure_ledger_subject_organization_id
        ON erasure_ledger (subject_organization_id);
    CREATE INDEX IF NOT EXISTS ix_erasure_ledger_open
        ON erasure_ledger (status) WHERE status <> 'complete';
"""

UPGRADE_SQL = CREATE_TABLE_SQL + ADD_ACTOR_FK_SQL + ADD_CHECKS_SQL + ADD_INDEXES_SQL

#: Dropping the table takes its constraints and indexes with it. This downgrade
#: **destroys compliance evidence** — it is the correct mirror of the upgrade, but an
#: operator running it after any erasure has occurred loses the record of it. Say so
#: out loud rather than leaving it implied.
DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS erasure_ledger;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
