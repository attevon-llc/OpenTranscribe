"""Add ``watch_source_file.document_id`` — auto-import routes documents too (#362).

Stage 6e wires watch sources to the document plane (``document`` / ``document_chunk``,
v393): a scanned file that isn't audio/video but IS a supported document format (PDF,
DOCX, HTML, ...) now creates a ``Document`` row instead of being skipped
``invalid_type``. The tracking row needs a link to whichever target it produced, mirroring
``media_file_id`` — a ``WatchSourceFile`` links to exactly one of the two, never both.

Design notes:
  - **``ON DELETE SET NULL``, matching ``media_file_id``.** The tracking row (history: this
    path was seen, when, with what dedup fingerprint) outlives the object it produced —
    deleting the ``Document`` should not delete the evidence that a scan imported it.
  - **No CHECK enforcing "at most one of media_file_id/document_id".** ``media_file_id`` is
    nullable with no such constraint either; a NULL is the natural "not this kind" value on
    both, and adding a two-column CHECK the sibling column never got would be an
    inconsistency of its own, not a real hardening (nothing writes to both — the ingest
    branch produces one row of one type).
  - **No column on ``watch_source`` for "route documents differently".** Documents share
    ``auto_transcribe``: a discovered document is auto-parsed under the same source toggle a
    discovered recording is auto-transcribed under, since both mean "start the pipeline
    without a human clicking anything." A separate toggle would be a control nobody asked to
    turn off independently.

Revision ID: v394_add_watch_source_file_document_id
Revises: v393_add_document_tables
Create Date: 2026-08-14
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v394_add_watch_source_file_document_id"
down_revision = "v393_add_document_tables"
branch_labels = None
depends_on = None

ADD_COLUMN_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'watch_source_file' AND column_name = 'document_id'
        ) THEN
            ALTER TABLE watch_source_file ADD COLUMN document_id INTEGER;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'watch_source_file_document_id_fkey'
        ) THEN
            ALTER TABLE watch_source_file
                ADD CONSTRAINT watch_source_file_document_id_fkey
                FOREIGN KEY (document_id) REFERENCES document(id) ON DELETE SET NULL;
        END IF;
    END $$;
"""

CREATE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS ix_watch_source_file_document_id
        ON watch_source_file (document_id);
"""

UPGRADE_SQL = ADD_COLUMN_SQL + CREATE_INDEX_SQL

DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS ix_watch_source_file_document_id;
    ALTER TABLE watch_source_file DROP CONSTRAINT IF EXISTS watch_source_file_document_id_fkey;
    ALTER TABLE watch_source_file DROP COLUMN IF EXISTS document_id;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
