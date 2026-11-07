"""One open scheduled erasure per subject, not per user (#1105).

Why
---
v455 made "one open self-service request per user" a database rule with
``uq_erasure_ledger_open_self_request ON erasure_ledger (subject_user_id) WHERE grace period``.
That index has no ``subject_type``: once an organization admin can schedule the erasure of one
member's data (``org_member``), that entry and the same person's own account-erasure request
(``user``) collide, and so do the same person's member erasures in two organizations. The second
request would be refused as "already open" although it is a different erasure.

``uq_erasure_ledger_open_grace_subject`` keeps the race-safe "one open request" rule per
(subject type, user, organization). ``COALESCE(..., 0)`` because a unique index treats NULLs as
distinct: an ``organization`` entry has no user and a ``user`` entry no organization, and without
the coalesce two open erasures of the same organization would both be accepted. Ids start at 1,
so 0 never names a real row.

The old index is dropped in the same revision: it is strictly narrower than the new one, and
keeping it would keep the collision this revision exists to remove. ``erasure_ledger`` holds one
row per erasure request, so a plain (non-concurrent) build takes its lock for milliseconds;
``lock_timeout`` bounds the wait for that lock instead of queueing traffic behind it.

Idempotent. The downgrade REFUSES while an ``org_member`` or ``organization`` request is open (the
old index cannot hold them next to a self-service request, and dropping them silently would forget
an erasure), then restores the v455 index.

Detection marker: ``uq_erasure_ledger_open_grace_subject``.

Revision ID: v464_generalise_erasure_grace_index
Revises: v463_add_media_file_trash
Create Date: 2026-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v464_generalise_erasure_grace_index"
down_revision = "v463_add_media_file_trash"
branch_labels = None
depends_on = None

#: Module-level so a consistency test can replay the real statements.
UPGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
CREATE UNIQUE INDEX IF NOT EXISTS uq_erasure_ledger_open_grace_subject
    ON erasure_ledger (
        subject_type, COALESCE(subject_user_id, 0), COALESCE(subject_organization_id, 0)
    )
    WHERE deferred_reason = 'grace_period' AND status = 'deferred';
DROP INDEX IF EXISTS uq_erasure_ledger_open_self_request;
"""

REFUSE_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM erasure_ledger
               WHERE status = 'deferred' AND deferred_reason = 'grace_period'
                 AND subject_type <> 'user') THEN
        RAISE EXCEPTION 'Cannot downgrade v464: scheduled organization or member erasures are '
            'still open. Cancel them first (POST /api/org-admin/gdpr/erasures/<uuid>/cancel).';
    END IF;
END $$;
"""

DOWNGRADE_SQL = (
    REFUSE_SQL
    + """
SET LOCAL lock_timeout = '30s';
CREATE UNIQUE INDEX IF NOT EXISTS uq_erasure_ledger_open_self_request
    ON erasure_ledger (subject_user_id)
    WHERE deferred_reason = 'grace_period' AND status = 'deferred';
DROP INDEX IF EXISTS uq_erasure_ledger_open_grace_subject;
"""
)


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
