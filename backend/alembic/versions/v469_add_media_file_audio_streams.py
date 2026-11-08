"""Audio tracks of a media file and the one chosen for transcription (issue #1263).

Why
---
A video container can carry several audio tracks (dubbed languages, commentary). Every decode
path read the first one and the user could neither see the others nor pick one. Two columns:

- ``media_file.audio_streams JSONB NULL``: what ffprobe reported at ingest, one object per audio
  stream (``index`` = the N of ``-map 0:a:N``, ``codec``, ``channels``, ``language``, ``title``,
  ``default``). NULL = not probed yet (a file ingested before this revision, until it is next
  processed).
- ``media_file.audio_stream_index INTEGER NULL``: the track the user chose. NULL = the first
  audio stream, exactly what every decode path read before this revision, so no backfill and a
  single-track file behaves unchanged.

Both columns are nullable with no default, so adding them rewrites nothing.

Detection marker: ``ck_media_file_audio_stream_index`` (``audio_stream_index >= 0``), created last.

The downgrade drops the CHECK and both columns, which DISCARDS the stored track list and the
user's choices; a file that is processed again is probed again, but the choices are not
re-derivable.

Revision ID: v469_add_media_file_audio_streams
Revises: v468_add_organization_owner
Create Date: 2026-11-08
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v469_add_media_file_audio_streams"
down_revision = "v468_add_organization_owner"
branch_labels = None
depends_on = None

#: Module-level so the consistency test can replay the real statements.
UPGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS audio_streams JSONB NULL;
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS audio_stream_index INTEGER NULL;
DO $$
BEGIN
    -- Created last: the detection arm keys on it.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_audio_stream_index'
    ) THEN
        ALTER TABLE media_file ADD CONSTRAINT ck_media_file_audio_stream_index
            CHECK (audio_stream_index IS NULL OR audio_stream_index >= 0) NOT VALID;
    END IF;
END $$;
ALTER TABLE media_file VALIDATE CONSTRAINT ck_media_file_audio_stream_index;
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE media_file DROP CONSTRAINT IF EXISTS ck_media_file_audio_stream_index;
ALTER TABLE media_file DROP COLUMN IF EXISTS audio_stream_index;
ALTER TABLE media_file DROP COLUMN IF EXISTS audio_streams;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
