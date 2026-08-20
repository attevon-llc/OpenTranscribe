"""Add ``document`` / ``document_chunk`` — the document ingestion plane (#362).

Stage 6a (``services/documents/``) built a working parser with nothing to call it: no
table, no endpoint, no Celery task, no UI. This is the DB half of closing that gap.

Design notes worth keeping with the DDL:
  - **Its own table, not a ``media_file`` discriminator.** ``media_file`` is ~70 columns
    loaded whole by every gallery page and every permission subquery, and most of that is
    A/V-specific state (duration, waveform, speakers, diarization) meaningless for a PDF.
    ``file_facts`` (v390) is the precedent for a narrow sidecar living beside the wide
    table; ``document`` goes further and is first-class, because unlike ``file_facts`` a
    document is not a derived artifact of something else — it has its own upload/list/
    detail/delete lifecycle.
  - **``document.status`` reuses ``FileStatus``**, the same enum ``media_file.status``
    uses (``core/enums.py``), rather than a parallel one: the lifecycle is the same shape
    (queued → processing → completed/error) and one status vocabulary keeps the gallery's
    badges and the status-detail API consistent instead of drifting into two.
  - **``document_chunk`` is durable storage, not the OpenSearch shape.** It stores exactly
    what ``services/documents/chunking.py:DocumentChunk.to_row()`` produces. Keeping it
    separate from the index is what lets a reindex read these rows instead of re-parsing
    the original file — the same reason ``transcript_segment`` and the
    ``transcript_chunks`` index are two different things for transcripts.
  - **``document_chunk.document_id`` is ``ON DELETE CASCADE``.** A chunk is a pure
    function of its document's parse; it has no meaning once the document is gone. Same
    reasoning ``file_facts.media_file_id`` used for the one deliberate ``CASCADE`` FK in
    the schema — this is the second.
  - **``document.user_id`` / ``organization_id`` are plain ``NO ACTION`` FKs**, matching
    ``media_file``'s house rule: user deletion is blocked by any file they still own
    (legal-hold / GDPR erasure path), not silently orphaned.
  - **Redaction lifecycle mirrors ``media_file``'s trio** (``redaction_status`` /
    ``redaction_model_version`` / ``redaction_coverage``) because document text lands in
    the same ``transcript_chunks`` index transcripts do and inherits the same masking
    contract both LLM egress paths enforce.

Every constraint here is also declared in ``app/models/document.py``. The repo carries 24
DDL-only constraints the ORM cannot see (``.rag-403/ddl-orm-divergence.md``); these two
tables do not add to that count.

All SQL is idempotent so it is safe to re-run against a partially-migrated database — the
startup runner stamps untracked databases by schema fingerprint, so a revision routinely
re-runs over its own partial output.

Revision ID: v394_add_document_tables
Revises: v393_add_overlap_timing_columns
Create Date: 2026-08-14
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v394_add_document_tables"
down_revision = "v393_add_overlap_timing_columns"
branch_labels = None
depends_on = None

#: Module-level so a consistency test can replay the real statements instead of asserting
#: on this file's source text (the convention v387/v388/v390 established).
CREATE_DOCUMENT_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS document (
        id                       SERIAL PRIMARY KEY,
        uuid                     UUID NOT NULL,
        user_id                  INTEGER NOT NULL,
        organization_id          INTEGER,
        filename                 VARCHAR NOT NULL,
        storage_path             VARCHAR NOT NULL,
        file_size                BIGINT NOT NULL,
        content_type             VARCHAR NOT NULL,
        file_hash                VARCHAR,
        status                   VARCHAR NOT NULL DEFAULT 'pending',
        last_error_message       TEXT,
        error_category           VARCHAR(50),
        parser                   VARCHAR(64),
        parser_version           VARCHAR(32),
        parse_version            INTEGER,
        page_count               INTEGER,
        language                 VARCHAR(16),
        has_embedded_text        BOOLEAN,
        ocr_applied              BOOLEAN NOT NULL DEFAULT false,
        ocr_pages                INTEGER NOT NULL DEFAULT 0,
        parse_warnings           VARCHAR[],
        word_count               INTEGER NOT NULL DEFAULT 0,
        chunk_count              INTEGER NOT NULL DEFAULT 0,
        redaction_status         VARCHAR,
        redaction_model_version  VARCHAR,
        redaction_coverage       VARCHAR[],
        created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
        parsed_at                TIMESTAMPTZ,
        CONSTRAINT uq_document_uuid UNIQUE (uuid),
        CONSTRAINT ck_document_file_size CHECK (file_size >= 0),
        CONSTRAINT ck_document_page_count CHECK (page_count IS NULL OR page_count >= 0),
        CONSTRAINT ck_document_ocr_pages CHECK (ocr_pages >= 0),
        CONSTRAINT ck_document_word_count CHECK (word_count >= 0),
        CONSTRAINT ck_document_chunk_count CHECK (chunk_count >= 0)
    );
"""

CREATE_DOCUMENT_CHUNK_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS document_chunk (
        id           SERIAL PRIMARY KEY,
        document_id  INTEGER NOT NULL,
        chunk_index  INTEGER NOT NULL,
        text         TEXT NOT NULL,
        char_start   INTEGER NOT NULL,
        char_end     INTEGER NOT NULL,
        page         INTEGER,
        section_path JSONB NOT NULL DEFAULT '[]',
        block_types  JSONB NOT NULL DEFAULT '[]',
        CONSTRAINT uq_document_chunk_index UNIQUE (document_id, chunk_index),
        CONSTRAINT ck_document_chunk_char_range CHECK (char_end >= char_start)
    );
"""

#: Added separately and guarded: ``CREATE TABLE IF NOT EXISTS`` is a no-op against a table
#: that already exists, so an inline FK would be skipped on a database created by an
#: earlier, partial run of this revision.
ADD_FK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'document_user_id_fkey'
        ) THEN
            ALTER TABLE document
                ADD CONSTRAINT document_user_id_fkey
                FOREIGN KEY (user_id) REFERENCES "user"(id);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'document_organization_id_fkey'
        ) THEN
            ALTER TABLE document
                ADD CONSTRAINT document_organization_id_fkey
                FOREIGN KEY (organization_id) REFERENCES organization(id);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'document_chunk_document_id_fkey'
        ) THEN
            ALTER TABLE document_chunk
                ADD CONSTRAINT document_chunk_document_id_fkey
                FOREIGN KEY (document_id) REFERENCES document(id) ON DELETE CASCADE;
        END IF;
    END $$;
"""

CREATE_INDEX_SQL = """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_document_uuid ON document (uuid);
    CREATE INDEX IF NOT EXISTS ix_document_organization_id ON document (organization_id);
    CREATE INDEX IF NOT EXISTS ix_document_filename ON document (filename);
    CREATE INDEX IF NOT EXISTS ix_document_file_hash ON document (file_hash);
    CREATE INDEX IF NOT EXISTS ix_document_status ON document (status);
    CREATE INDEX IF NOT EXISTS ix_document_error_category ON document (error_category);
"""

UPGRADE_SQL = (
    CREATE_DOCUMENT_TABLE_SQL + CREATE_DOCUMENT_CHUNK_TABLE_SQL + ADD_FK_SQL + CREATE_INDEX_SQL
)

#: Chunks go with their document; the document table drop takes both.
DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS document_chunk;
    DROP TABLE IF EXISTS document;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
