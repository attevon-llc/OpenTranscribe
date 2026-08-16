"""Add ``media_file.recorded_date`` **and the provenance that makes it usable**.

#403 Stage 4 / R7. Every date-scoped question in this product filters on
``upload_time`` — the moment the bytes arrived — so a 2019 recording imported today
is counted as 2026. Measured on the eval corpus: ``upload_time`` had exactly one
distinct value across all 432 files (the injection date), while the meetings
themselves span a year. Anyone importing a back-catalogue gets the wrong answer to
every date question, confidently.

Why five columns and not one
----------------------------
A derived date the user cannot see the origin of, **or correct**, is worse than no
date: it answers "3 meetings in March" when the truth is 5 and offers no way to find
out. This repo already holds the precedent — LLM speaker-ID suggestions are surfaced
with confidence for manual verification and never auto-applied — and this follows it.

  - ``recorded_date`` — the resolved answer.
  - ``recorded_date_source`` — **which** of ``container`` / ``filename`` /
    ``transcript`` / ``llm`` / ``manual`` / ``none`` produced it.
    ``ck_media_file_recorded_date_provenance`` makes a bare date *unrepresentable*:
    a non-NULL date without a source is rejected by the database, not by a convention
    someone can forget.
  - ``recorded_date_confidence`` — 0..1, CHECK-bounded.
  - ``recorded_date_candidates`` — **every** source's observation, not just the
    winner. Sources legitimately disagree (a recording made on the 14th about the
    15th's meeting is normal), and a disagreement buried in an if-chain is exactly
    the silent-wrong-answer class this epic keeps hitting. Kept so a conflict can be
    surfaced and the user asked, rather than resolved by whichever branch ran first.
  - ``recorded_date_locked`` — the user's own value outranks every derived source
    **permanently**. ``ck_media_file_recorded_date_locked_is_manual`` makes a locked
    row whose source is not ``manual`` unrepresentable, so a later re-derivation
    cannot quietly relabel a hand-entered date as machine-derived.

Why there is no backfill here
-----------------------------
``media_file.creation_date`` already exists and looks like it could seed this. It
cannot: it is populated by a silent three-tier fallback (container metadata →
filesystem mtime → ``upload_time``) that records **no provenance**, so on an existing
row a real container date is indistinguishable from a copy of ``upload_time``.
Backfilling from it would launder ``upload_time`` into a column that claims to know
when the meeting happened — the precise defect this revision exists to end. Rows
start NULL and are filled by the resolver, which re-reads the sources and records
which one answered. A NULL here means "not yet resolved" and reads honestly; a
laundered value would not.

The partial index carries ``WHERE recorded_date IS NOT NULL`` because the date filter
only ever asks for rows that *have* one, and on a fresh deployment that is none of
them.

Every constraint here is also declared in ``app/models/media.py``. The repo carries a
gate (``tests/unit/test_orm_ddl_divergence.py``) whose allowlist is empty by
measurement, not by luck; this table does not reopen it.

All SQL is idempotent so it is safe to re-run against a partially-migrated database —
the startup runner stamps untracked databases by schema fingerprint, so a revision
routinely re-runs over its own partial output.

Revision ID: v390_add_recorded_date_provenance
Revises: v389_add_file_facts
Create Date: 2026-08-13
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v391_add_recorded_date_provenance"
down_revision = "v390_add_file_facts"
branch_labels = None
depends_on = None

#: Module-level so ``tests/unit/test_v390_migration_consistency.py`` replays the real
#: statements instead of asserting on this file's source text (the convention v387/v388
#: established after v386 shipped without one).
ADD_COLUMNS_SQL = """
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS recorded_date TIMESTAMPTZ;
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS recorded_date_source VARCHAR(16);
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS recorded_date_confidence DOUBLE PRECISION;
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS recorded_date_candidates JSONB;
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS recorded_date_locked BOOLEAN NOT NULL DEFAULT FALSE;
"""

#: Guarded individually: ``ADD COLUMN IF NOT EXISTS`` is a no-op against a table that
#: already has the column, so an inline CHECK would be skipped on a database created by
#: an earlier, partial run of this revision.
#:
#: ``ck_media_file_recorded_date_provenance`` is the one that carries the design: it is
#: what makes "a date with no recorded origin" a state the database refuses to hold.
ADD_CHECKS_SQL = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_media_file_recorded_date_source'
        ) THEN
            ALTER TABLE media_file
                ADD CONSTRAINT ck_media_file_recorded_date_source
                CHECK (recorded_date_source IS NULL OR recorded_date_source IN (
                    'container', 'filename', 'transcript', 'llm', 'manual', 'none'
                ));
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_media_file_recorded_date_provenance'
        ) THEN
            ALTER TABLE media_file
                ADD CONSTRAINT ck_media_file_recorded_date_provenance
                CHECK (recorded_date IS NULL OR recorded_date_source IS NOT NULL);
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_media_file_recorded_date_confidence'
        ) THEN
            ALTER TABLE media_file
                ADD CONSTRAINT ck_media_file_recorded_date_confidence
                CHECK (recorded_date_confidence IS NULL
                       OR (recorded_date_confidence >= 0 AND recorded_date_confidence <= 1));
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'ck_media_file_recorded_date_locked_is_manual'
        ) THEN
            ALTER TABLE media_file
                ADD CONSTRAINT ck_media_file_recorded_date_locked_is_manual
                CHECK (NOT recorded_date_locked OR recorded_date_source = 'manual');
        END IF;
    END $$;
"""

#: The date filter asks only for rows that have a recorded date, so the index excludes
#: the rest — which, on a database that has not run the resolver yet, is all of them.
CREATE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS ix_media_file_recorded_date
        ON media_file (recorded_date) WHERE recorded_date IS NOT NULL;
"""

UPGRADE_SQL = ADD_COLUMNS_SQL + ADD_CHECKS_SQL + CREATE_INDEX_SQL

#: The columns go, so the CHECKs and the index go with them.
DOWNGRADE_SQL = """
    DROP INDEX IF EXISTS ix_media_file_recorded_date;
    ALTER TABLE media_file
        DROP CONSTRAINT IF EXISTS ck_media_file_recorded_date_locked_is_manual;
    ALTER TABLE media_file
        DROP CONSTRAINT IF EXISTS ck_media_file_recorded_date_confidence;
    ALTER TABLE media_file
        DROP CONSTRAINT IF EXISTS ck_media_file_recorded_date_provenance;
    ALTER TABLE media_file
        DROP CONSTRAINT IF EXISTS ck_media_file_recorded_date_source;
    ALTER TABLE media_file DROP COLUMN IF EXISTS recorded_date_locked;
    ALTER TABLE media_file DROP COLUMN IF EXISTS recorded_date_candidates;
    ALTER TABLE media_file DROP COLUMN IF EXISTS recorded_date_confidence;
    ALTER TABLE media_file DROP COLUMN IF EXISTS recorded_date_source;
    ALTER TABLE media_file DROP COLUMN IF EXISTS recorded_date;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
