"""Add ``media_file.duration_source`` — where ``duration`` came from (issue #969).

The column ``duration`` has always stored two different quantities under one
name: the recording's real length (from the container, via ffprobe/exiftool) and
the transcript's SPEECH EXTENT (``max(segment.end)``, where the last word ends).
``app/tasks/transcription/storage.py::update_media_file_transcription_status``
unconditionally overwrote the first with the second on every completed file —
trailing silence, music or applause was discarded, silently, on 100% of rows.
Measured on the live dev DB: 10 of 10 completed rows equal their transcript's
speech extent to the millisecond; real-world error on YouTube content is ~11 s,
5x the 2.0 s window ``recovery_tasks.youtube_metadata_backfill`` matches rows in
— which is why that feature has never worked.

A second, undocumented defect compounds it: PyExifTool returns GROUP-PREFIXED
tags (``Composite:Duration`` for a WAV) that the metadata extractor's exact-match
field mapping never recognised, so an audio file's container duration was never
even captured in the first place. Fixed alongside this migration in
``metadata_extractor.py`` (group-insensitive matching for technical fields, plus
a direct ``probe_media_duration()`` ffprobe fallback) and in ``storage.py`` (the
speech extent is now written only as a LAST RESORT, when no container duration
is already present).

Why a column, not just a code fix
----------------------------------
Without provenance, a row corrected by this fix and a row still holding the old
overwrite are indistinguishable — so a backfill of existing corrupted rows
(``app.tasks.recovery_tasks.media_duration_backfill``) cannot target idempotently,
and its result cannot be verified. Same problem ``recorded_date_source`` (v391)
exists to solve, same shape here:

  - ``duration_source`` — which of ``container`` / ``transcript_extent`` /
    ``none`` produced the current value. ``ck_media_file_duration_source``
    restricts it to that vocabulary.

Why there is no backfill here
-----------------------------
Unlike v391's ``recorded_date``, this column is NOT paired with a
"non-NULL value requires non-NULL source" CHECK. Every pre-existing row already
has a `duration` (however wrong) and a NULL source — that pairing would make the
live production schema instantly unrepresentable on upgrade. A NULL source means
"written before #969 was fixed; provenance unknown", which is exactly the
predicate ``media_duration_backfill`` selects on. This revision is schema only.

Revision ID: v394_add_media_duration_provenance
Revises: v393_add_overlap_timing_columns
Create Date: 2026-09-21
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v394_add_media_duration_provenance"
down_revision = "v393_add_overlap_timing_columns"
branch_labels = None
depends_on = None

#: Module-level so a consistency test replays the real statements rather than asserting
#: on this file's source text (the convention v387/v388 established).
ADD_COLUMN_SQL = """
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS duration_source VARCHAR(20);
"""

#: Guarded individually: ``ADD COLUMN IF NOT EXISTS`` is a no-op against a table that
#: already has the column, so an inline CHECK would be skipped on a database created by
#: an earlier, partial run of this revision.
ADD_CHECK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_media_file_duration_source'
        ) THEN
            ALTER TABLE media_file
                ADD CONSTRAINT ck_media_file_duration_source
                CHECK (duration_source IS NULL OR duration_source IN (
                    'container', 'transcript_extent', 'none'
                ));
        END IF;
    END $$;
"""

UPGRADE_SQL = ADD_COLUMN_SQL + ADD_CHECK_SQL

#: The column goes, so the CHECK goes with it.
DOWNGRADE_SQL = """
    ALTER TABLE media_file
        DROP CONSTRAINT IF EXISTS ck_media_file_duration_source;
    ALTER TABLE media_file DROP COLUMN IF EXISTS duration_source;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
