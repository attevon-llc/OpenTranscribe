"""Persist a file's per-file transcription request, and stop defaulting watch sources to 1/20.

Issues #1203, #1202, #1198.

Why columns
-----------
A retry, a recovery sweep or a reprocess-with-defaults used to re-dispatch a file with
whatever the caller happened to pass, which for every retry route was nothing: the
speaker range the file was submitted with, and whether the user had skipped diarization
for it, were gone. ``requested_whisper_model`` already existed for the model; these four
columns are its siblings, so the file carries everything the user chose for it:

  - ``requested_min_speakers`` / ``requested_max_speakers`` / ``requested_num_speakers``
  - ``requested_disable_diarization`` -- NULL means "not asked", which is not ``false``

NULL everywhere means "use my saved setting, then the deployment default". That is
the behaviour of every row written before this revision, so there is no backfill.

Watch sources
-------------
``watch_source.min_speakers`` / ``max_speakers`` carried ``DEFAULT 1`` / ``DEFAULT 20``
and the create modal always sent a number, so every watch folder passed 1 and 20 as
a per-file override and beat the owner's saved range. The defaults are dropped and
existing rows holding exactly the old default pair are set to NULL ("use my default").
A source whose owner really chose 1/20 is indistinguishable from one that took the
default, so it changes too -- to the owner's saved range, which is the value they would
have got had the default never been written.

Revision ID: v433_add_requested_transcription_options
Revises: v432_support_access_grant
Create Date: 2026-10-10
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v433_add_requested_transcription_options"
down_revision = "v432_support_access_grant"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
ADD_COLUMNS_SQL = """
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS requested_min_speakers INTEGER,
        ADD COLUMN IF NOT EXISTS requested_max_speakers INTEGER,
        ADD COLUMN IF NOT EXISTS requested_num_speakers INTEGER,
        ADD COLUMN IF NOT EXISTS requested_disable_diarization BOOLEAN;
"""

#: Idempotent: a second run finds no 1/20 pair left to rewrite.
WATCH_SOURCE_SQL = """
    ALTER TABLE watch_source ALTER COLUMN min_speakers DROP DEFAULT;
    ALTER TABLE watch_source ALTER COLUMN max_speakers DROP DEFAULT;
    UPDATE watch_source
       SET min_speakers = NULL, max_speakers = NULL
     WHERE min_speakers = 1 AND max_speakers = 20;
"""

UPGRADE_SQL = ADD_COLUMNS_SQL + WATCH_SOURCE_SQL

#: The rewritten watch-source rows are not restored: the old default pair carried no
#: information beyond "nobody chose".
DOWNGRADE_SQL = """
    ALTER TABLE watch_source ALTER COLUMN min_speakers SET DEFAULT 1;
    ALTER TABLE watch_source ALTER COLUMN max_speakers SET DEFAULT 20;
    ALTER TABLE media_file
        DROP COLUMN IF EXISTS requested_disable_diarization,
        DROP COLUMN IF EXISTS requested_num_speakers,
        DROP COLUMN IF EXISTS requested_max_speakers,
        DROP COLUMN IF EXISTS requested_min_speakers;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
