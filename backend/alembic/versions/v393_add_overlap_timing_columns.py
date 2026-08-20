"""Add the timing columns for transcribe-diarize overlap and progressive presentation.

Three markers now exist that had nowhere durable to land:

``diarize_request_sent`` / ``diarize_joined``
    Diarization is handed to the sidecar as soon as the audio is in memory and collected
    after transcription, so the GPU stage costs ``max(transcribe, diarize)`` rather than
    their sum. Without these two the row shows only the total and there is no way to see
    how much of diarization actually landed inside transcription's window — which is the
    whole claim the change rests on.

``transcript_ready``
    The transcript becomes durable and readable at progress 0.78, well before the job
    finishes. The gap between this and ``completion_notified`` is the perceived-latency
    saving, and it is invisible without a column.

``fully_indexed_duration_ms`` already existed but has been equal to
``user_perceived_duration_ms`` on every row, because the timing row was persisted at the end
of postprocess while indexing, clustering, summary and redaction write their markers
afterwards. That flush ordering is fixed alongside this migration; these columns are what
the late flush has to write into.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v393_add_overlap_timing_columns"
down_revision = "v392_add_redaction_coverage"
branch_labels = None
depends_on = None

#: Module-level so a consistency test can replay the real statement rather than asserting
#: on this file's source text (the convention v387/v388 established).
UPGRADE_SQL = """
    ALTER TABLE file_pipeline_timing
        ADD COLUMN IF NOT EXISTS diarize_request_sent_ms BIGINT,
        ADD COLUMN IF NOT EXISTS diarize_joined_ms BIGINT,
        ADD COLUMN IF NOT EXISTS transcript_ready_ms BIGINT;
"""

DOWNGRADE_SQL = """
    ALTER TABLE file_pipeline_timing
        DROP COLUMN IF EXISTS diarize_request_sent_ms,
        DROP COLUMN IF EXISTS diarize_joined_ms,
        DROP COLUMN IF EXISTS transcript_ready_ms;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
