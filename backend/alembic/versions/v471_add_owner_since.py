"""When the current owner acquired a row an organization content transfer can move (#1104).

Why
---
``org_content_transfer`` hands a departing member's organization content to an administrator by
rewriting ``user_id`` (``owner_id`` on ``user_group``). The rows keep their creation time, which
is correct (it is when the content was made) but was also the only clock the org-member erasure
re-run had. That re-run asks "does the erased member own rows created at or before the erasure's
``completed_at``?" to detect a restore. Content handed to an already-erased member afterwards
answered yes: the daily reconciliation recorded a resurrection that never happened and the
time-bounded re-run purged the handed-over recordings, profiles, prompts and watch sources.

``owner_since TIMESTAMPTZ NULL`` on every table the transfer re-owns, stamped by the transfer.
The re-run, the time-bounded erasure and the index phase judge a row by
``coalesce(owner_since, <creation time>)``: when its CURRENT owner got it. NULL = the row has
had one owner since it was created, so no backfill: every row written before this revision was
never transferred under it, and for those the creation time is still the right answer.

Community edition: the transfer needs an organization membership, which core never creates, so
the columns stay NULL and nothing reads differently.

Detection marker: ``user_group.owner_since``, added last.

The downgrade drops the columns, which DISCARDS every stamp: a later re-run then judges handed
over rows by their creation time again.

Revision ID: v471_add_owner_since
Revises: v470_add_requested_source_language
Create Date: 2026-11-09
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v471_add_owner_since"
down_revision = "v470_add_requested_source_language"
branch_labels = None
depends_on = None

#: Every table ``org_content_transfer`` re-owns, ``user_group`` last (the detection marker).
TABLES = (
    "media_file",
    "speaker",
    "topic_suggestion",
    "task",
    "speaker_profile",
    "speaker_collection",
    "speaker_cluster",
    "collection",
    "tag",
    "summary_prompt",
    "custom_vocabulary",
    "watch_source",
    "user_group",
)

#: Module-level so the consistency test can replay the real statements. Spelled out, not
#: generated, so the migration-safety guard reads every statement.
UPGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE speaker ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE topic_suggestion ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE task ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE speaker_profile ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE speaker_collection ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE speaker_cluster ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE collection ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE tag ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE summary_prompt ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE custom_vocabulary ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE watch_source ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
ALTER TABLE user_group ADD COLUMN IF NOT EXISTS owner_since TIMESTAMPTZ NULL;
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE user_group DROP COLUMN IF EXISTS owner_since;
ALTER TABLE watch_source DROP COLUMN IF EXISTS owner_since;
ALTER TABLE custom_vocabulary DROP COLUMN IF EXISTS owner_since;
ALTER TABLE summary_prompt DROP COLUMN IF EXISTS owner_since;
ALTER TABLE tag DROP COLUMN IF EXISTS owner_since;
ALTER TABLE collection DROP COLUMN IF EXISTS owner_since;
ALTER TABLE speaker_cluster DROP COLUMN IF EXISTS owner_since;
ALTER TABLE speaker_collection DROP COLUMN IF EXISTS owner_since;
ALTER TABLE speaker_profile DROP COLUMN IF EXISTS owner_since;
ALTER TABLE task DROP COLUMN IF EXISTS owner_since;
ALTER TABLE topic_suggestion DROP COLUMN IF EXISTS owner_since;
ALTER TABLE speaker DROP COLUMN IF EXISTS owner_since;
ALTER TABLE media_file DROP COLUMN IF EXISTS owner_since;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
