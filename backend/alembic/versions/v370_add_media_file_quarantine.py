"""Add abuse/DMCA quarantine + legal-hold columns to media_file.

Adds an admin **takedown** capability independent of the processing ``status``
so a completed file can be quarantined (hidden from every read surface for
non-admins) and later released back to exactly its prior state. The original
media + transcript are never deleted by a takedown — masking/hiding is a
read-time transform; the row survives for the audit + appeal trail.

Columns (all default to the not-quarantined state so existing rows and normal
operation are unaffected):
  - ``is_quarantined``    BOOLEAN  NOT NULL DEFAULT false  (authoritative flag)
  - ``quarantine_reason`` TEXT     NULL                    (free-text reason)
  - ``quarantined_at``    TIMESTAMPTZ NULL                 (when applied)
  - ``quarantined_by``    INTEGER  NULL  FK user(id)       (admin who applied)
  - ``legal_hold``        BOOLEAN  NOT NULL DEFAULT false  (S3 legal-hold mirror)

A partial index on ``is_quarantined`` keeps the admin "list quarantined" review
query and the read-surface exclusion predicate cheap. All SQL is idempotent
(``IF NOT EXISTS``) for safe re-run on partially-migrated databases.

Revision ID: v370_add_media_file_quarantine
Revises: v369_superuser_role_invariant
Create Date: 2026-06-15
"""

from alembic import op

revision = "v370_add_media_file_quarantine"
down_revision = "v369_superuser_role_invariant"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'media_file' AND column_name = 'is_quarantined'
            ) THEN
                ALTER TABLE media_file
                    ADD COLUMN is_quarantined BOOLEAN NOT NULL DEFAULT false;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'media_file' AND column_name = 'quarantine_reason'
            ) THEN
                ALTER TABLE media_file ADD COLUMN quarantine_reason TEXT;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'media_file' AND column_name = 'quarantined_at'
            ) THEN
                ALTER TABLE media_file ADD COLUMN quarantined_at TIMESTAMPTZ;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'media_file' AND column_name = 'quarantined_by'
            ) THEN
                ALTER TABLE media_file
                    ADD COLUMN quarantined_by INTEGER REFERENCES "user"(id);
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'media_file' AND column_name = 'legal_hold'
            ) THEN
                ALTER TABLE media_file
                    ADD COLUMN legal_hold BOOLEAN NOT NULL DEFAULT false;
            END IF;
        END $$;
    """)

    # Partial index: only quarantined rows are indexed (review list + exclusion).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_media_file_is_quarantined "
        "ON media_file (is_quarantined) WHERE is_quarantined = true"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_media_file_is_quarantined")
    op.execute("ALTER TABLE media_file DROP COLUMN IF EXISTS legal_hold")
    op.execute("ALTER TABLE media_file DROP COLUMN IF EXISTS quarantined_by")
    op.execute("ALTER TABLE media_file DROP COLUMN IF EXISTS quarantined_at")
    op.execute("ALTER TABLE media_file DROP COLUMN IF EXISTS quarantine_reason")
    op.execute("ALTER TABLE media_file DROP COLUMN IF EXISTS is_quarantined")
