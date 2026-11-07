"""Durable record of a privacy purge's unfinished cluster-centroid work (issue #1334 item 2).

Why
---
A voiceprint purge (``privacy.purge_voiceprints``, issue #1280) deletes the cluster ROWS in its
relational commit and their ``cluster_<uuid>`` OpenSearch documents afterwards. Once the rows are
gone nothing in Postgres names those documents, so pass 1 handed the ids it could not confirm
deleted to pass 2 in the Celery message. A worker that died between the relational commit and that
dispatch took the ids with it: the centroid documents survived, nothing would ever retry them, and
a later purge of the same scope measured a clean residual and reported done.

``privacy_purge_pending`` holds one row per purge that still owes that work. The relational half
inserts it in the SAME transaction that deletes the cluster rows, so the two commit together; the
final pass deletes it once every listed document is measured gone, and the
``privacy.purge_reverify`` beat task finishes (or reports incomplete) any row a dead worker left.

- ``task_id``: the purge's task id (the ``task`` row's key). No foreign key: the row must outlive
  the task row until its documents are gone.
- ``scope``: ``PurgeScope.to_payload()`` (kind, organization/user ids, file ids), so the re-verify
  can write its audit event to the same tenant. Surrogate ids only; no name, no text.
- ``cluster_uuids``: JSON array of the centroid ids not yet confirmed deleted (``ck_..._array``).
- ``reverify_attempts`` / ``last_reverify_at``: what the re-verify has tried.

Every deployment gets the table; it is empty except while a purge is running or a store could not
be purged. The detection arm keys on ``ck_privacy_purge_pending_array``, created last. The
downgrade drops the table: drain it first (any row is a centroid document still to delete).

Revision ID: v461_add_privacy_purge_pending
Revises: v460_add_llm_token_budget
Create Date: 2026-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v461_add_privacy_purge_pending"
down_revision = "v460_add_llm_token_budget"
branch_labels = None
depends_on = None

#: Module-level so a consistency test can replay the real statements.
UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS privacy_purge_pending (
    task_id VARCHAR(255) PRIMARY KEY,
    scope JSONB NOT NULL,
    cluster_uuids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverify_attempts INTEGER NOT NULL DEFAULT 0,
    last_reverify_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_privacy_purge_pending_created_at
    ON privacy_purge_pending (created_at);
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_privacy_purge_pending_array'
    ) THEN
        ALTER TABLE privacy_purge_pending ADD CONSTRAINT ck_privacy_purge_pending_array
            CHECK (jsonb_typeof(cluster_uuids) = 'array' AND jsonb_typeof(scope) = 'object');
    END IF;
END $$;
"""

DOWNGRADE_SQL = "DROP TABLE IF EXISTS privacy_purge_pending;"


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
