"""Widen ``file_facts`` so documents join the artifact/summary tiers (#403 Stage 6).

#362's document plane needs the same deterministic facts/digest/keyphrases shape
``file_facts`` (v390) already gives transcripts — same JSONB columns, same
``generator_version``/``source_fingerprint`` lifecycle, same "regenerate on a
reindex, cheap when unchanged" contract. The design question this revision
answers is **one table, widened, or a second ``document_facts`` table** — and
the deliberate answer is the former, so there stays exactly one artifact code
path (``services/ingest_artifacts``) rather than two that drift. ``file_facts.py``
said as much the day the sidecar was designed (#403 comment 1, Nuance 3): "the
sidecar is already the document analog ... same table, same shape, ``char_range``
provenance instead of ``segment_ids``. No second table, no second code path."

Three changes, together:

* ``media_file_id`` becomes **nullable**.
* ``document_id`` is added, FK'd to ``document(id) ON DELETE CASCADE`` — the
  same cascade reasoning v390 gave ``media_file_id``: the artifacts are a pure
  function of the parse, meaningless once the document is gone, and a manual
  cleanup pass is one more thing to forget.
* **Exactly one of the two is set, enforced by the database, not by
  convention.** ``ck_file_facts_exactly_one_owner`` is an arithmetic XOR
  (``(media_file_id IS NOT NULL)::int + (document_id IS NOT NULL)::int = 1``) —
  a row naming both or neither owner is a row this table's every reader
  (``ingest_artifacts/service.py``, ``chat/mapreduce.py``) would otherwise have
  to defend against by hand. The old ``UNIQUE (media_file_id)`` — which made
  regeneration an upsert rather than a read-modify-write race — is replaced by
  **two partial unique indexes**, one per owner column, because a plain
  composite ``UNIQUE (media_file_id, document_id)`` would not catch two rows
  that both left ``media_file_id`` NULL: Postgres treats NULLs as distinct, so
  a bare UNIQUE lets an unbounded number of document-owned rows share the same
  ``document_id`` once one column in the pair is always NULL for them. Partial
  indexes, scoped ``WHERE <col> IS NOT NULL``, close that (the same shape
  ``v374``'s per-owner ``tag`` uniqueness uses for the identical reason).

``app/models/file_facts.py``'s docstring is corrected in the same change: its
premise that "documents are ``media_file`` rows with ``kind='document'``" was
never what #362 built — ``document`` is its own table (v394) — and the sidecar
argument survives that correction unchanged, so only the false premise needed
fixing, not the conclusion.

Every constraint here is also declared in ``app/models/file_facts.py``. The
repo carries 24 DDL-only constraints the ORM cannot see
(``.rag-403/ddl-orm-divergence.md``); this table does not add a 25th.

All SQL is idempotent so it is safe to re-run against a partially-migrated
database — the startup runner stamps untracked databases by schema
fingerprint, so a revision routinely re-runs over its own partial output.

DOWNGRADE IS DESTRUCTIVE for document-owned rows, and deliberately so, the
same class of decision ``v382_scim_tokens`` and ``v389_add_erasure_ledger``
document for their own downgrades: restoring ``media_file_id NOT NULL`` and the
single-column uniqueness is only possible once no row can violate them, so the
downgrade deletes every row with ``document_id IS NOT NULL`` before restoring
the old shape. Unlike those two, nothing here is irrecoverable data — a
document's facts/digest/keyphrases are a deterministic function of its
``document_chunk`` rows (``ingest_artifacts.document_service.generate_document_artifacts``)
and regenerate identically on the next reindex, the same reason v390's own
CASCADE never worried about losing a media file's artifacts either.

Revision ID: v398_widen_file_facts_for_documents
Revises: v397_backfill_document_tenancy_and_hash
Create Date: 2026-08-20
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v398_widen_file_facts_for_documents"
down_revision = "v397_backfill_document_tenancy_and_hash"
branch_labels = None
depends_on = None

#: Module-level so the consistency test can replay the real statements instead of
#: asserting on this file's source text (the v387/v388/v390/v394 convention).
DROP_NOT_NULL_SQL = """
    ALTER TABLE file_facts ALTER COLUMN media_file_id DROP NOT NULL;
"""

ADD_DOCUMENT_ID_COLUMN_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'file_facts' AND column_name = 'document_id'
        ) THEN
            ALTER TABLE file_facts ADD COLUMN document_id INTEGER;
        END IF;
    END $$;
"""

#: Separate, guarded block: a database that already has the column (from an
#: earlier partial run of this same revision) must still get the FK.
ADD_DOCUMENT_FK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'file_facts_document_id_fkey'
        ) THEN
            ALTER TABLE file_facts
                ADD CONSTRAINT file_facts_document_id_fkey
                FOREIGN KEY (document_id) REFERENCES document(id) ON DELETE CASCADE;
        END IF;
    END $$;
"""

#: The old single-column UNIQUE, replaced below by two partial unique indexes.
#: Dropped by name (not by shape-search, unlike v374's `tag` migration) because
#: this table has only ever had the one constraint this revision's own
#: predecessor created.
DROP_OLD_UNIQUE_SQL = """
    ALTER TABLE file_facts DROP CONSTRAINT IF EXISTS uq_file_facts_media_file;
"""

CREATE_PARTIAL_UNIQUE_INDEXES_SQL = """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_file_facts_media_file_id
        ON file_facts (media_file_id) WHERE media_file_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_file_facts_document_id
        ON file_facts (document_id) WHERE document_id IS NOT NULL;
"""

ADD_XOR_CHECK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_file_facts_exactly_one_owner'
        ) THEN
            ALTER TABLE file_facts
                ADD CONSTRAINT ck_file_facts_exactly_one_owner
                CHECK (
                    (CASE WHEN media_file_id IS NOT NULL THEN 1 ELSE 0 END)
                    + (CASE WHEN document_id IS NOT NULL THEN 1 ELSE 0 END)
                    = 1
                );
        END IF;
    END $$;
"""

UPGRADE_SQL = (
    DROP_NOT_NULL_SQL
    + ADD_DOCUMENT_ID_COLUMN_SQL
    + ADD_DOCUMENT_FK_SQL
    + DROP_OLD_UNIQUE_SQL
    + CREATE_PARTIAL_UNIQUE_INDEXES_SQL
    + ADD_XOR_CHECK_SQL
)

#: Destructive for document-owned rows — see the module docstring. Idempotent: a
#: second run finds no matching rows and no constraint to re-add. The DELETE is
#: guarded on the column's own existence (not just `IF EXISTS` on a constraint) —
#: unlike a `DROP CONSTRAINT`/`DROP INDEX`, a bare `DELETE ... WHERE document_id ...`
#: referencing an already-dropped column is a hard error, not a no-op, which a first
#: unguarded draft of this revision learned by re-running the downgrade twice.
DOWNGRADE_SQL = """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'file_facts' AND column_name = 'document_id'
        ) THEN
            DELETE FROM file_facts WHERE document_id IS NOT NULL;
        END IF;
    END $$;
    ALTER TABLE file_facts DROP CONSTRAINT IF EXISTS ck_file_facts_exactly_one_owner;
    DROP INDEX IF EXISTS uq_file_facts_document_id;
    DROP INDEX IF EXISTS uq_file_facts_media_file_id;
    ALTER TABLE file_facts DROP CONSTRAINT IF EXISTS file_facts_document_id_fkey;
    ALTER TABLE file_facts DROP COLUMN IF EXISTS document_id;
    ALTER TABLE file_facts ALTER COLUMN media_file_id SET NOT NULL;
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_file_facts_media_file'
        ) THEN
            ALTER TABLE file_facts ADD CONSTRAINT uq_file_facts_media_file UNIQUE (media_file_id);
        END IF;
    END $$;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
