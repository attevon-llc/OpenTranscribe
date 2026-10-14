"""Keep a user's speaker corrections across a re-run of the file (issue #1233).

Why
---
Unassigning a speaker from a cluster or profile records a correction
(``speaker_profile_blacklist``, ``speaker_cannot_link``) keyed by the speaker's row id. A
re-diarization, a re-transcription and a retry delete every speaker of the file, and the
``ON DELETE CASCADE`` on both tables deletes the corrections with them. The re-run then
creates new speakers for the same voices and the voiceprint stage matches them against
the profiles again, so the section the user said was not that person was matched straight
back to them by the same comparison that was wrong the first time.

``speaker_correction_carryover`` holds such a correction while its speaker does not exist:
the corrected speaker's time spans and what it was ruled out from (a profile, or a speaker
of another file). The re-run's segment save gives it to the new speakers whose speech lies
in those spans and deletes the row. Every foreign key is ``ON DELETE CASCADE``: a deleted
file, profile or partner speaker leaves nothing to carry.

Affects every deployment that re-runs files; empty until a corrected file is re-run.
Community edition: no difference.

Revision ID: v438_add_speaker_correction_carryover
Revises: v437_add_media_processing_provenance
Create Date: 2026-10-12
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v438_add_speaker_correction_carryover"
down_revision = "v437_add_media_processing_provenance"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    CREATE TABLE IF NOT EXISTS speaker_correction_carryover (
        id SERIAL PRIMARY KEY,
        media_file_id INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
        profile_id INTEGER REFERENCES speaker_profile(id) ON DELETE CASCADE,
        cannot_link_speaker_id INTEGER REFERENCES speaker(id) ON DELETE CASCADE,
        spans JSONB NOT NULL,
        reason VARCHAR(255),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
        CONSTRAINT ck_speaker_correction_carryover_one_target
            CHECK ((profile_id IS NULL) <> (cannot_link_speaker_id IS NULL))
    );
    CREATE INDEX IF NOT EXISTS ix_speaker_correction_carryover_id
        ON speaker_correction_carryover (id);
    CREATE INDEX IF NOT EXISTS ix_speaker_correction_carryover_media_file_id
        ON speaker_correction_carryover (media_file_id);
"""

DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS speaker_correction_carryover;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
