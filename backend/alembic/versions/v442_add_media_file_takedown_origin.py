"""Record which authority holds a taken-down or legally held file (issue #1253).

Why
---
Quarantine and release used to be platform-administrator-only, so a file under
takedown always meant "the operator did it". Organization administrators can now
quarantine and release their OWN organization's files, which needs one new fact: who
holds the file. ``media_file.takedown_origin`` is ``'platform'`` or ``'organization'``
and the rule is "platform wins" -- an organization administrator can release what an
organization placed, never what the operator placed. Without it the operator's legal
action could be undone by the tenant it was aimed at.

What
----
* ``takedown_origin VARCHAR(16)`` plus ``ck_media_file_takedown_origin_valid``.
* Backfill: every row that is quarantined or under legal hold becomes ``'platform'``.
  Until this revision the only writer of either flag was the platform admin route, so
  that is exactly who placed them.
* A ``BEFORE INSERT OR UPDATE`` trigger keeps the column consistent with the flags no
  matter which code writes them: a held row with no origin becomes ``'platform'`` (the
  fail-closed reading), and a row with nothing held has no origin. This replaces a
  plain CHECK on purpose: a CHECK would refuse every writer that sets
  ``is_quarantined`` without knowing about the new column -- dozens of fixtures, and any
  extension code -- while the trigger makes the unsafe state unrepresentable without
  breaking them.

Affects every deployment; the column is NULL for every file that was never held.
Community edition: unchanged behaviour (nothing there ever sets ``'organization'``).

Revision ID: v442_add_media_file_takedown_origin
Revises: v441_add_legal_acceptance
Create Date: 2026-10-21
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v442_add_media_file_takedown_origin"
down_revision = "v441_add_legal_acceptance"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    ALTER TABLE media_file ADD COLUMN IF NOT EXISTS takedown_origin VARCHAR(16);

    UPDATE media_file
       SET takedown_origin = 'platform'
     WHERE (is_quarantined OR legal_hold) AND takedown_origin IS NULL;

    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_takedown_origin_valid'
        ) THEN
            ALTER TABLE media_file ADD CONSTRAINT ck_media_file_takedown_origin_valid
                CHECK (takedown_origin IS NULL
                       OR takedown_origin IN ('platform', 'organization'));
        END IF;
    END $$;

    CREATE OR REPLACE FUNCTION media_file_takedown_origin_sync() RETURNS trigger AS $fn$
    BEGIN
        IF NEW.is_quarantined OR NEW.legal_hold THEN
            IF NEW.takedown_origin IS NULL THEN
                NEW.takedown_origin := 'platform';
            END IF;
        ELSE
            NEW.takedown_origin := NULL;
        END IF;
        RETURN NEW;
    END;
    $fn$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS trg_media_file_takedown_origin ON media_file;
    CREATE TRIGGER trg_media_file_takedown_origin
        BEFORE INSERT OR UPDATE OF is_quarantined, legal_hold, takedown_origin ON media_file
        FOR EACH ROW EXECUTE FUNCTION media_file_takedown_origin_sync();
"""

DOWNGRADE_SQL = """
    DROP TRIGGER IF EXISTS trg_media_file_takedown_origin ON media_file;
    DROP FUNCTION IF EXISTS media_file_takedown_origin_sync();
    ALTER TABLE media_file DROP CONSTRAINT IF EXISTS ck_media_file_takedown_origin_valid;
    ALTER TABLE media_file DROP COLUMN IF EXISTS takedown_origin;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
