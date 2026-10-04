"""Add user.platform_super_admin_link_authorized (issue #993).

The escape hatch for ``auth.account_linking.assert_provider_id_link_permitted``'s
rule 1, which otherwise unconditionally refuses to JIT-link/refresh an external
identity onto a ``role == super_admin`` row. Nothing in core ever sets this column
to True — it exists for a deployment with its own out-of-band, audited,
non-self-serve admin-grant mechanism that needs a super_admin it deliberately
granted to an externally-authenticated user to actually be usable on login.

DEFAULT FALSE means zero behavior change for every existing row: the guard stays
exactly as unconditional as it was until a caller outside this migration's scope
sets the column explicitly.

Numbered v397, not v394, deliberately: as of this writing `origin/feat/doc-ingestion`
(issue #362) already reserves v394-v396 (`add_document_tables`,
`add_watch_source_file_document_id`, `add_document_chunk_redaction_cache`), chained off
this same `v393_add_overlap_timing_columns` parent but unmerged. `db/CLAUDE.md`'s
renumbering notes 1-3 describe three prior instances of exactly this collision — two
branches independently taking the same next number off a shared `master` head — each
requiring a rename across four+ places once discovered at merge time. Skipping past the
already-reserved range avoids a fourth instance; the two chains still fork at this
parent until one is rebased onto the other, which is the ordinary multi-branch state,
not a naming collision.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v397_add_platform_super_admin_link_authorized"
down_revision = "v393_add_overlap_timing_columns"
branch_labels = None
depends_on = None

#: Module-level so a consistency test can replay the real statement rather than
#: asserting on this file's source text (the convention v387/v388/v393 established).
UPGRADE_SQL = """
    ALTER TABLE "user"
        ADD COLUMN IF NOT EXISTS platform_super_admin_link_authorized
            BOOLEAN NOT NULL DEFAULT FALSE;
"""

DOWNGRADE_SQL = """
    ALTER TABLE "user"
        DROP COLUMN IF EXISTS platform_super_admin_link_authorized;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
