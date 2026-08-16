"""Add ``media_file.redaction_coverage`` — WHICH detectors a finished scan actually ran.

#403 / task #78, the residual `v389`-era fix
(``e6048808``) named and did not close.

Why ``redaction_status = done`` stopped being enough
----------------------------------------------------
``done`` used to mean two things at once: the scan finished, **and** every detector
examined the text. ``e6048808`` split them deliberately. An *unavailable* detector — no
Presidio installed, no model weights on disk — now resolves to ``done`` with the detector
reported in ``skipped_detectors``, rather than to ``failed``, because ``failed`` is not an
inert label: ``llm_guard.resolve_llm_masking`` turns it into a **non-retryable** refusal,
so marking every file ``failed`` on a deployment that simply lacks Presidio would
permanently break summarization, speaker identification and topic extraction for every
user with ``redact_before_llm`` on.

The cost of that (correct) choice is that ``done`` no longer answers "was this text
examined for PII". Every read path that trusts cached spans on the strength of ``done``
can therefore mask nothing, report success, and hand a transcript full of PII to an LLM
provider. Nothing durable survived the scan to say otherwise: ``skipped_detectors`` is a
key in ``detect_and_store``'s **return value**, which reaches a Celery result backend with
a TTL and a WebSocket toast, and neither is readable by a masker an hour later.

Why a column and not a wider record
-----------------------------------
The read side asks exactly one question, by primary key: *which detectors do this file's
cached spans reflect?* Nothing filters, aggregates, sorts or joins on it, and the
vocabulary is four closed names (``profanity`` / ``pii`` / ``toxicity`` / ``llm``). JSONB
was the obvious proposal and is the wrong shape for that: it buys schemaless nesting
nobody needs, costs ``->>`` gymnastics in any future operator report, and invites the
column to accrete fields until it is a second, undocumented status. ``TEXT[]`` is the
value that is actually read (``set(row.redaction_coverage or [])``), stays queryable if a
report ever wants it (``WHERE NOT ('pii' = ANY(redaction_coverage))``), and is
GIN-indexable if one ever needs to be fast.

No CHECK on the element vocabulary, deliberately. An *unknown* name in the array is
harmless — the gap is computed as ``required - covered``, so a stray name grants no
coverage — while the hazardous state, a **missing** name, is exactly what no CHECK can
detect. Pinning the vocabulary in SQL would buy nothing and would make adding a fifth
detector need a migration.

Why NULL is trusted rather than refused
---------------------------------------
Rows written before this revision carry NULL, and there is no way to recover
retroactively whether the deployment that scanned them had Presidio. Reading NULL as "no
coverage" would refuse pre-existing files on every deployment on the day of the upgrade;
reading it as "unknown, and no worse than yesterday" leaves exactly the pre-existing
hazard in place for those rows and closes it for everything scanned from here on. The
existing remedy already exists and is the honest one: ``redaction.reindex_all`` re-scans,
and re-scanning writes coverage. ``app/services/redaction/coverage.py`` states this
residual where the decision is read.

There is no backfill for the same reason ``v390`` has none: a value invented to fill the
column would claim knowledge the row does not carry, and a laundered "fully covered" is
strictly worse than an honest NULL.

All SQL is idempotent so it is safe to re-run against a partially-migrated database — the
startup runner stamps untracked databases by schema fingerprint, so a revision routinely
re-runs over its own partial output.

Revision ID: v391_add_redaction_coverage
Revises: v390_add_recorded_date_provenance
Create Date: 2026-08-13
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v392_add_redaction_coverage"
down_revision = "v391_add_recorded_date_provenance"
branch_labels = None
depends_on = None

#: Module-level so ``tests/unit/test_v391_migration_consistency.py`` replays the real
#: statement instead of asserting on this file's source text (the convention v387/v388
#: established after v386 shipped without one).
UPGRADE_SQL = """
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS redaction_coverage TEXT[];
"""

DOWNGRADE_SQL = """
    ALTER TABLE media_file DROP COLUMN IF EXISTS redaction_coverage;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
