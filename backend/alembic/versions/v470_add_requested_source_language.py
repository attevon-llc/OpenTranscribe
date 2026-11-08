"""The language a user forced for one transcription run (issue #1263).

Why
---
Reprocess can now force the transcription language for one run (a track whose language was
mislabelled, or one auto-detect gets wrong). A retry or stuck-file recovery re-dispatches from
the file's stored request (``requested_*`` columns), so the choice must be stored with the
rest of it or a retry would quietly fall back to auto-detect.

``media_file.requested_source_language TEXT NULL``: an ISO 639 code or ``auto``. NULL = not
asked (the user's saved language setting applies), exactly the behaviour before this revision,
so no backfill. Nullable with no default, so adding it rewrites nothing.

Detection marker: ``ck_media_file_requested_source_language``, created last.

The downgrade drops the CHECK and the column, which DISCARDS every stored choice.

Revision ID: v470_add_requested_source_language
Revises: v469_add_media_file_audio_streams
Create Date: 2026-11-08
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v470_add_requested_source_language"
down_revision = "v469_add_media_file_audio_streams"
branch_labels = None
depends_on = None

#: Module-level so the consistency test can replay the real statements.
UPGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS requested_source_language TEXT NULL;
DO $$
BEGIN
    -- Created last: the detection arm keys on it.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_requested_source_language'
    ) THEN
        ALTER TABLE media_file ADD CONSTRAINT ck_media_file_requested_source_language
            CHECK (requested_source_language IS NULL
                   OR requested_source_language ~ '^(auto|[a-z]{2,3})$') NOT VALID;
    END IF;
END $$;
ALTER TABLE media_file VALIDATE CONSTRAINT ck_media_file_requested_source_language;
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE media_file DROP CONSTRAINT IF EXISTS ck_media_file_requested_source_language;
ALTER TABLE media_file DROP COLUMN IF EXISTS requested_source_language;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
