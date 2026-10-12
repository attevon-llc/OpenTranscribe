"""Record, per file, which provider and model completed each processing step.

Issue #1229 (WP7 of ``docs/design/v060_pipeline_composition_plan.md``).

Why
---
A file's details page could say which Whisper model and which diarization model ran, but
not where each step ran, which engine detected the speakers (the local PyAnnote engine
and the pyannote.ai cloud service both recorded ``diarization_provider = 'pyannote'``),
how voiceprints were computed, or which LLM wrote the summary, the topics and the
speaker-name suggestions. Those steps run in different tasks, sometimes concurrently.

``media_file.processing_provenance`` is one JSONB document,
``{"v": 1, "steps": {"<step>": {...facts...}}}``, written by exactly one function
(``app/services/file_provenance.record_stage``) as a single atomic ``jsonb_set`` so two
stages finishing at once cannot overwrite each other. It never holds a URL, a key or a
configuration id.

NULL means "processed before this was recorded". There is no backfill on purpose: the
existing columns cannot say where a step ran, and the read side composes what they do
say, marked as legacy, rather than guessing the rest.

Revision ID: v437_add_media_processing_provenance
Revises: v436_lite_local_speaker_detection_to_automatic
Create Date: 2026-10-11

Numbering: first written as v435 beside the WP3 branch's v434; renumbered to v437 to follow
v436 when the branches merged (``backend/alembic/CLAUDE.md``).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v437_add_media_processing_provenance"
down_revision = "v436_lite_local_speaker_detection_to_automatic"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    ALTER TABLE media_file ADD COLUMN IF NOT EXISTS processing_provenance JSONB;
"""

DOWNGRADE_SQL = """
    ALTER TABLE media_file DROP COLUMN IF EXISTS processing_provenance;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
