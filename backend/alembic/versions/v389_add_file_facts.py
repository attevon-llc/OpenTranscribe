"""Add ``file_facts`` — deterministic ingest artifacts for the no-LLM summary tier.

#383 Phase 2 / #403 Stage 2. One row per ``media_file``, holding three JSONB payloads
produced by ``app/services/ingest_artifacts`` with **no LLM, no model load and no
OpenSearch**: exact per-file statistics (``facts``), an extractive digest whose every
sentence carries provenance (``digest``), and keyphrases.

Design notes worth keeping with the DDL:
  - **A sidecar, not columns on ``media_file``.** The plan text said
    ``MediaFile.file_facts``; ``media_file`` is ~70 columns and is loaded whole by every
    gallery page and permission subquery, while these artifacts have two readers. Stage 3
    also needs a cheap "which rows are stale" scan on the reindex path, which is an
    indexed read of a narrow table here and a scan of the widest table there.
  - **``ON DELETE CASCADE``.** The artifacts are a pure function of the transcript, so
    they are meaningless once the file is gone. This is the one FK style choice that is
    *not* the ``organization_id`` house rule (``NO ACTION``): nothing is re-exposed by
    deleting a derived row.
  - **``UNIQUE (media_file_id)``**, which is what makes regeneration an upsert instead of
    a read-modify-write race between the pipeline and a manual reindex.
  - **``source_fingerprint``** is a SHA-256 over the ordered segments (id, timings,
    resolved speaker, text). Regeneration short-circuits on a match, so Stage 3 can call
    the generator on every reindex and pay a hash for unchanged files. A speaker rename
    changes the resolved names and therefore the fingerprint, which is how issue #405's
    rename becomes a digest-regeneration trigger without a separate trigger list.
  - **``generator_version``** is ``"{facts}.{digest}.{keyphrases}"`` schema versions.
    Indexed, because "regenerate everything below version X" is the rollout mechanism for
    an algorithm change.
  - **No ``organization_id``.** Tenancy is carried by the parent ``media_file`` row and
    every read reaches this table through it; a second stamp would be a second thing to
    keep in sync and a second way to get a tenant filter wrong.

Every constraint here is also declared in ``app/models/file_facts.py``. The repo carries
24 DDL-only constraints that the ORM cannot see (``uq_transcript_segment_content`` being
the one that aborted a corpus load); this table does not add a 25th.

All SQL is idempotent so it is safe to re-run against a partially-migrated database — the
startup runner stamps untracked databases by schema fingerprint, so a revision routinely
re-runs over its own partial output.

Revision ID: v389_add_file_facts
Revises: v388_add_user_group_organization_id
Create Date: 2026-08-12
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v389_add_file_facts"
down_revision = "v388_add_user_group_organization_id"
branch_labels = None
depends_on = None

#: Module-level so ``tests/unit/test_v389_migration_consistency.py`` replays the real
#: statements instead of asserting on this file's source text (the convention v387/v388
#: established after v386 shipped without one).
CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS file_facts (
        id                 SERIAL PRIMARY KEY,
        media_file_id      INTEGER NOT NULL,
        generator_version  VARCHAR(32) NOT NULL,
        source_fingerprint VARCHAR(64) NOT NULL,
        language           VARCHAR(16),
        facts              JSONB NOT NULL,
        digest             JSONB NOT NULL,
        keyphrases         JSONB NOT NULL,
        digest_word_count  INTEGER NOT NULL DEFAULT 0,
        section_count      INTEGER NOT NULL DEFAULT 0,
        generation_ms      INTEGER,
        generated_at       TIMESTAMPTZ DEFAULT now(),
        CONSTRAINT uq_file_facts_media_file UNIQUE (media_file_id),
        CONSTRAINT ck_file_facts_digest_word_count CHECK (digest_word_count >= 0),
        CONSTRAINT ck_file_facts_section_count CHECK (section_count >= 0),
        CONSTRAINT ck_file_facts_ms CHECK (generation_ms IS NULL OR generation_ms >= 0)
    );
"""

#: Added separately and guarded: ``CREATE TABLE IF NOT EXISTS`` is a no-op against a table
#: that already exists, so an inline FK would be skipped on a database created by an
#: earlier, partial run of this revision.
ADD_FK_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'file_facts_media_file_id_fkey'
        ) THEN
            ALTER TABLE file_facts
                ADD CONSTRAINT file_facts_media_file_id_fkey
                FOREIGN KEY (media_file_id) REFERENCES media_file(id) ON DELETE CASCADE;
        END IF;
    END $$;
"""

#: "Which rows predate the current generator" — Stage 3's regeneration sweep.
CREATE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS ix_file_facts_generator_version
        ON file_facts (generator_version);
"""

UPGRADE_SQL = CREATE_TABLE_SQL + ADD_FK_SQL + CREATE_INDEX_SQL

#: The table goes, so the index and constraints go with it.
DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS file_facts;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
