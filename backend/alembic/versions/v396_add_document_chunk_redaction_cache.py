"""Add ``document_chunk.redactions`` / ``.toxicity`` — cached detection spans (#362).

Documents are indexed into the same v6 ``transcript_chunks`` plane transcripts use and
inherit the identical masking contract (root ``CLAUDE.md``'s chat retrieval trap), but
until this revision nothing populates ``document.redaction_status`` /
``redaction_coverage`` and there is nowhere to cache detection spans for a document
chunk. ``services/chat/redactor.py`` documented this exact gap: "expect a new
``mask_document_chunks``-style function keyed on char offsets, following the same
fail-closed contract."

Design notes:
  - **Cache lives on ``document_chunk``, not ``document``.** ``TranscriptSegment`` is the
    cache granularity for transcripts (``redactions``/``toxicity`` per segment); a
    ``document_chunk`` row is the exact retrieval unit a chat turn masks — unlike a
    transcript chunk, which is *rebuilt* from multiple overlapping ``TranscriptSegment``
    rows, a document chunk in Postgres already **is** the unit indexed into OpenSearch
    (1:1, no rebuild needed). Caching per-chunk is therefore both the natural fit and
    strictly simpler than the transcript case.
  - **``redactions`` is JSONB, matching ``transcript_segment.redactions``** — a list of
    span dicts, offsets addressing ``document_chunk.text`` exactly the way the transcript
    column addresses ``transcript_segment.text``. Spans are cached, never recomputed;
    enable/disable and category selection apply at read time via the same
    ``RedactionService.mask_segment`` every other masker calls.
  - **``toxicity`` is JSONB, matching ``transcript_segment.toxicity``** — a score dict,
    not a span list (toxicity has no offsets; see ``redaction/CLAUDE.md``'s
    ``_DETECTOR_CATEGORIES`` gotcha). Documents get a real detection pass, not the
    latency-motivated inline-fallback's reduced set, so toxicity is scored here the same
    as it is for transcripts.
  - **No new columns on ``document`` itself** — ``redaction_status`` /
    ``redaction_model_version`` / ``redaction_coverage`` already exist (v394), added
    ahead of time for exactly this. This revision only adds where the *spans* live.

Revision ID: v396_add_document_chunk_redaction_cache
Revises: v395_add_watch_source_file_document_id
Create Date: 2026-08-14
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v396_add_document_chunk_redaction_cache"
down_revision = "v395_add_watch_source_file_document_id"
branch_labels = None
depends_on = None

ADD_COLUMNS_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document_chunk' AND column_name = 'redactions'
        ) THEN
            ALTER TABLE document_chunk ADD COLUMN redactions JSONB;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document_chunk' AND column_name = 'toxicity'
        ) THEN
            ALTER TABLE document_chunk ADD COLUMN toxicity JSONB;
        END IF;
    END $$;
"""

UPGRADE_SQL = ADD_COLUMNS_SQL

DOWNGRADE_SQL = """
    ALTER TABLE document_chunk DROP COLUMN IF EXISTS toxicity;
    ALTER TABLE document_chunk DROP COLUMN IF EXISTS redactions;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
