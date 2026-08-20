"""Add quarantine/legal-hold columns to ``document`` + ``task.document_id`` (#362 lane C3/C4).

Two independent, additive changes bundled into one revision because both are small and
both belong to the same "documents finish joining the platforms they were left out of"
effort:

1. **``document`` gets the exact six columns ``v370``/``v371`` gave ``media_file``**:
   ``is_quarantined`` / ``quarantine_reason`` / ``quarantined_at`` / ``quarantined_by`` /
   ``pre_quarantine_status`` / ``legal_hold``. Today there is no takedown path for a
   document at all — an abuse/DMCA notice against an uploaded PDF has nowhere to land.
   The shape is copied verbatim rather than reinvented so ``services/takedown_service.py``
   can extend its **existing** enforcement (``exclude_quarantined`` /
   ``is_hidden_for`` / audit events) to a second model instead of growing a parallel
   implementation — the same reasoning ``file_facts`` (v398) gave for widening one table
   instead of forking it. ``quarantined_by`` is ``ON DELETE SET NULL`` from the start
   (unlike ``media_file.quarantined_by``, which needed the separate ``v387`` repair) —
   the takedown record must outlive the reviewing admin's account exactly the way
   ``v387`` established for its media counterpart, and there is no pre-existing NO ACTION
   row here to migrate away from.

2. **``task.document_id``**, nullable, FK'd to ``document(id)``, mirroring
   ``task.media_file_id`` exactly (same NO ACTION rule — a task row is cheap history, and
   forcing a decision at document-delete time is wrong for the same reason it is wrong for
   media). Today ``documents.parse``/``documents.index`` (``app/tasks/document_tasks.py``,
   ``document_indexing_task.py``) track progress only in Redis
   (``services/documents/progress.py``) and never write a ``task`` row, so a parse that
   dies mid-flight leaves no durable record and ``GET /tasks`` cannot list it. No XOR
   CHECK is added — ``task.media_file_id`` has never had one either, because plenty of
   tasks (the imohash recompute, model-switch reindex, …) belong to neither a file nor
   a document, and a task now legitimately belongs to at most one of the two.

Both blocks are independently idempotent so a partially-migrated database re-running
either half is safe; a database with document quarantine already applied and
``task.document_id`` still missing (or vice versa) is handled by the same guarded DDL
that runs today.

Revision ID: v399_add_document_quarantine_and_task_link
Revises: v398_widen_file_facts_for_documents
Create Date: 2026-08-20
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v399_add_document_quarantine_and_task_link"
down_revision = "v398_widen_file_facts_for_documents"
branch_labels = None
depends_on = None

#: Module-level so the consistency test can replay the exact statements instead of
#: asserting on this file's source text (the v390/v394/v398 convention).
ADD_DOCUMENT_QUARANTINE_COLUMNS_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document' AND column_name = 'is_quarantined'
        ) THEN
            ALTER TABLE document ADD COLUMN is_quarantined BOOLEAN NOT NULL DEFAULT false;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document' AND column_name = 'quarantine_reason'
        ) THEN
            ALTER TABLE document ADD COLUMN quarantine_reason TEXT;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document' AND column_name = 'quarantined_at'
        ) THEN
            ALTER TABLE document ADD COLUMN quarantined_at TIMESTAMPTZ;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document' AND column_name = 'quarantined_by'
        ) THEN
            ALTER TABLE document
                ADD COLUMN quarantined_by INTEGER REFERENCES "user"(id) ON DELETE SET NULL;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document' AND column_name = 'pre_quarantine_status'
        ) THEN
            ALTER TABLE document ADD COLUMN pre_quarantine_status VARCHAR(50);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'document' AND column_name = 'legal_hold'
        ) THEN
            ALTER TABLE document ADD COLUMN legal_hold BOOLEAN NOT NULL DEFAULT false;
        END IF;
    END $$;
"""

ADD_DOCUMENT_QUARANTINE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS ix_document_is_quarantined
        ON document (is_quarantined) WHERE is_quarantined = true;
"""

ADD_TASK_DOCUMENT_ID_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'task' AND column_name = 'document_id'
        ) THEN
            ALTER TABLE task ADD COLUMN document_id INTEGER;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'task_document_id_fkey'
        ) THEN
            ALTER TABLE task
                ADD CONSTRAINT task_document_id_fkey
                FOREIGN KEY (document_id) REFERENCES document(id);
        END IF;
    END $$;
"""

ADD_TASK_DOCUMENT_ID_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS ix_task_document_id ON task (document_id);
"""

UPGRADE_SQL = (
    ADD_DOCUMENT_QUARANTINE_COLUMNS_SQL
    + ADD_DOCUMENT_QUARANTINE_INDEX_SQL
    + ADD_TASK_DOCUMENT_ID_SQL
    + ADD_TASK_DOCUMENT_ID_INDEX_SQL
)

DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS ix_task_document_id;
    ALTER TABLE task DROP CONSTRAINT IF EXISTS task_document_id_fkey;
    ALTER TABLE task DROP COLUMN IF EXISTS document_id;
    DROP INDEX IF EXISTS ix_document_is_quarantined;
    ALTER TABLE document DROP COLUMN IF EXISTS legal_hold;
    ALTER TABLE document DROP COLUMN IF EXISTS pre_quarantine_status;
    ALTER TABLE document DROP COLUMN IF EXISTS quarantined_by;
    ALTER TABLE document DROP COLUMN IF EXISTS quarantined_at;
    ALTER TABLE document DROP COLUMN IF EXISTS quarantine_reason;
    ALTER TABLE document DROP COLUMN IF EXISTS is_quarantined;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
