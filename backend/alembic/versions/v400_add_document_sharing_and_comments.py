"""Add ``document_share`` + widen ``comment`` for documents (#362 lane C3-remainder/C5).

Two independent, additive changes, both closing the same gap: a document today has no
sharing model and no notes/comments, while its ``media_file`` sibling has had both for a
long time. Bundled into one revision because both are small and both are prerequisites for
the same lane's frontend work (the document picker's chat-scope estimator, the document
detail page's "Share" action, and the document analogue of ``CommentSection``).

1. **``document_share``** — the direct-share counterpart of ``collection_share``, not a
   second copy of the collection machinery. A document has no ``collection`` concept yet
   (``collection_member.media_file_id`` only ever pointed at ``media_file``, and widening
   that is a separate, larger change this revision deliberately does not make — see
   ``app/services/permission_service.py``'s new document methods for the narrower rule this
   table backs). So a document share grants access to exactly **one** document, the same
   ``target_user_id`` XOR ``target_group_id`` shape and ``viewer``/``editor`` permission
   ``collection_share`` already uses — copied field-for-field (including its four
   constraint/index names, just re-scoped to ``document_id``) so
   ``PermissionService.get_document_permission`` reads as the file-sharing rule's obvious
   sibling rather than a new design. This is also the missing grant source
   ``app/tasks/search_indexing_task.py:_document_accessible_user_ids`` was written to be
   extended by ("the named seam a future document-sharing lane extends") — see that
   function's docstring, updated in the same change as this revision to read from it.

2. **``comment`` gets a nullable ``document_id``** (FK'd to ``document(id) ON DELETE
   CASCADE``) and ``media_file_id`` becomes nullable, with a database-enforced XOR —
   ``ck_comment_exactly_one_owner`` — exactly the shape ``v398`` gave ``file_facts`` for the
   identical reason: a comment naming both or neither owner is a row every reader
   (``api/endpoints/comments.py``) would otherwise have to defend against by hand.
   ``document_id`` is ``ON DELETE CASCADE`` (unlike ``comment.media_file_id``, which has
   never had an ``ondelete`` at all and relies on ``MediaFile.comments``'s ORM-level
   ``cascade="all, delete-orphan"`` running before the file row is deleted):
   ``documents.py:delete_document`` does a bare ``db.delete(doc)`` with no ORM-cascaded
   ``comments`` relationship on ``Document`` prior to this revision, so a NO ACTION FK would
   turn "delete a commented document" into an unhandled ``IntegrityError``. CASCADE here
   matches the reasoning ``document.chunks`` and ``document.facts_row`` already use: a
   comment on a document is meaningless once the document is gone, the same way a chunk is.

   A document also gets a nullable ``comment.document_chunk_id`` (FK'd to
   ``document_chunk(id) ON DELETE SET NULL``) — the chunk/page anchor a document note
   needs, the document analogue of ``timestamp``'s role for a media comment. ``SET NULL``
   rather than CASCADE: a re-chunking reparse legitimately deletes and recreates
   ``document_chunk`` rows (``services/documents/chunking.py``), and a comment anchored to
   a chunk that no longer exists should degrade to an unanchored document-level note, not
   be destroyed — the same asymmetry ``document.quarantined_by`` (SET NULL) has against
   ``document_chunk.document_id`` (CASCADE): one is "this row's identity", the other is
   "a pointer that may go stale."

Both blocks are independently idempotent so a partially-migrated database re-running either
half is safe — the startup runner stamps untracked databases by schema fingerprint, so a
revision routinely re-runs over its own partial output.

Revision ID: v400_add_document_sharing_and_comments
Revises: v399_add_document_quarantine_and_task_link
Create Date: 2026-08-20
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v400_add_document_sharing_and_comments"
down_revision = "v399_add_document_quarantine_and_task_link"
branch_labels = None
depends_on = None

#: Module-level so the consistency test can replay the exact statements instead of
#: asserting on this file's source text (the v390/v394/v398/v399 convention).
CREATE_DOCUMENT_SHARE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS document_share (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL DEFAULT gen_random_uuid(),
        document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
        shared_by_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
        target_type VARCHAR(20) NOT NULL,
        target_user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE,
        target_group_id INTEGER REFERENCES user_group(id) ON DELETE CASCADE,
        permission VARCHAR(20) NOT NULL DEFAULT 'viewer',
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now(),
        CONSTRAINT document_share_uuid_key UNIQUE (uuid),
        CONSTRAINT _document_share_target_check CHECK (
            (target_user_id IS NOT NULL AND target_group_id IS NULL)
            OR (target_user_id IS NULL AND target_group_id IS NOT NULL)
        ),
        CONSTRAINT _document_share_permission_check CHECK (permission IN ('viewer', 'editor')),
        CONSTRAINT _document_share_target_type_check CHECK (target_type IN ('user', 'group'))
    );
"""

CREATE_DOCUMENT_SHARE_INDEXES_SQL = """
    CREATE INDEX IF NOT EXISTS ix_document_share_uuid ON document_share (uuid);
    CREATE INDEX IF NOT EXISTS ix_document_share_document_id ON document_share (document_id);
    CREATE INDEX IF NOT EXISTS ix_document_share_shared_by_id ON document_share (shared_by_id);
    CREATE INDEX IF NOT EXISTS ix_document_share_target_user_id
        ON document_share (target_user_id);
    CREATE INDEX IF NOT EXISTS ix_document_share_target_group_id
        ON document_share (target_group_id);
    CREATE UNIQUE INDEX IF NOT EXISTS _document_share_user_uc
        ON document_share (document_id, target_user_id) WHERE target_user_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS _document_share_group_uc
        ON document_share (document_id, target_group_id) WHERE target_group_id IS NOT NULL;
"""

DROP_COMMENT_NOT_NULL_SQL = """
    ALTER TABLE comment ALTER COLUMN media_file_id DROP NOT NULL;
"""

ADD_COMMENT_DOCUMENT_ID_COLUMN_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'comment' AND column_name = 'document_id'
        ) THEN
            ALTER TABLE comment ADD COLUMN document_id INTEGER;
        END IF;
    END $$;
"""

ADD_COMMENT_DOCUMENT_FK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'comment_document_id_fkey'
        ) THEN
            ALTER TABLE comment
                ADD CONSTRAINT comment_document_id_fkey
                FOREIGN KEY (document_id) REFERENCES document(id) ON DELETE CASCADE;
        END IF;
    END $$;
"""

ADD_COMMENT_DOCUMENT_CHUNK_ID_COLUMN_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'comment' AND column_name = 'document_chunk_id'
        ) THEN
            ALTER TABLE comment ADD COLUMN document_chunk_id INTEGER;
        END IF;
    END $$;
"""

ADD_COMMENT_DOCUMENT_CHUNK_FK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'comment_document_chunk_id_fkey'
        ) THEN
            ALTER TABLE comment
                ADD CONSTRAINT comment_document_chunk_id_fkey
                FOREIGN KEY (document_chunk_id) REFERENCES document_chunk(id) ON DELETE SET NULL;
        END IF;
    END $$;
"""

CREATE_COMMENT_DOCUMENT_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_comment_document_id ON comment (document_id);
    CREATE INDEX IF NOT EXISTS idx_comment_document_chunk_id ON comment (document_chunk_id);
"""

ADD_COMMENT_XOR_CHECK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_comment_exactly_one_owner'
        ) THEN
            ALTER TABLE comment
                ADD CONSTRAINT ck_comment_exactly_one_owner
                CHECK (
                    (CASE WHEN media_file_id IS NOT NULL THEN 1 ELSE 0 END)
                    + (CASE WHEN document_id IS NOT NULL THEN 1 ELSE 0 END)
                    = 1
                );
        END IF;
    END $$;
"""

UPGRADE_SQL = (
    CREATE_DOCUMENT_SHARE_TABLE_SQL
    + CREATE_DOCUMENT_SHARE_INDEXES_SQL
    + DROP_COMMENT_NOT_NULL_SQL
    + ADD_COMMENT_DOCUMENT_ID_COLUMN_SQL
    + ADD_COMMENT_DOCUMENT_FK_SQL
    + ADD_COMMENT_DOCUMENT_CHUNK_ID_COLUMN_SQL
    + ADD_COMMENT_DOCUMENT_CHUNK_FK_SQL
    + CREATE_COMMENT_DOCUMENT_INDEX_SQL
    + ADD_COMMENT_XOR_CHECK_SQL
)

#: Destructive for document-owned comment rows, the same class of decision v398's own
#: downgrade documents for file_facts — guarded on the column's own existence so a second
#: run of the downgrade is a no-op rather than a hard error against an already-dropped
#: column.
DOWNGRADE_SQL = """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'comment' AND column_name = 'document_id'
        ) THEN
            DELETE FROM comment WHERE document_id IS NOT NULL;
        END IF;
    END $$;
    ALTER TABLE comment DROP CONSTRAINT IF EXISTS ck_comment_exactly_one_owner;
    DROP INDEX IF EXISTS idx_comment_document_chunk_id;
    DROP INDEX IF EXISTS idx_comment_document_id;
    ALTER TABLE comment DROP CONSTRAINT IF EXISTS comment_document_chunk_id_fkey;
    ALTER TABLE comment DROP COLUMN IF EXISTS document_chunk_id;
    ALTER TABLE comment DROP CONSTRAINT IF EXISTS comment_document_id_fkey;
    ALTER TABLE comment DROP COLUMN IF EXISTS document_id;
    ALTER TABLE comment ALTER COLUMN media_file_id SET NOT NULL;
    DROP TABLE IF EXISTS document_share;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
