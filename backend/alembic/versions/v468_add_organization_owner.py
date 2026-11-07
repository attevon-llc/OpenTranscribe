"""One owner per organization: ``organization.owner_user_id`` (issue #1105).

Why
---
Scheduling the erasure of a whole organization (v0.7.0, ``POST /org-admin/gdpr/erase-organization``)
was open to every organization administrator. Erasing the tenant is a decision for one accountable
person, so an organization now has an OWNER: an organization administrator the organization names,
who alone can schedule the erasure of the organization and who can hand the role to another
administrator of the same organization.

- ``owner_user_id INTEGER NULL`` with ``fk_organization_owner_user`` (``ON DELETE SET NULL``: an
  organization outlives its owner's account; ownership then falls back to the rule in
  ``services/organization_owner``: no owner, no organization erasure by an organization admin).
- Backfill: the earliest ``org:admin`` membership (``created_at``, then ``id``) of each organization
  that has no owner yet. An organization with no administrator keeps NULL.

Community edition: no organizations, so the backfill touches nothing.

Safe on a populated table: the FK is added ``NOT VALID`` and then validated, and every statement
runs under a 30 s ``lock_timeout``. The column is NULLable, so adding it rewrites nothing.

Detection marker: ``fk_organization_owner_user``, created last.

The downgrade drops the FK and the column, which DISCARDS who the owners were; re-running the
upgrade re-derives them from the memberships, not from what was lost.

Revision ID: v468_add_organization_owner
Revises: v467_add_content_hold_authority
Create Date: 2026-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v468_add_organization_owner"
down_revision = "v467_add_content_hold_authority"
branch_labels = None
depends_on = None

#: Module-level so the consistency test can replay the real statements.
UPGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE organization ADD COLUMN IF NOT EXISTS owner_user_id INTEGER NULL;
UPDATE organization o SET owner_user_id = m.user_id
FROM (
    SELECT DISTINCT ON (organization_id) organization_id, user_id
    FROM organization_membership
    WHERE role = 'org:admin'
    ORDER BY organization_id, created_at, id
) m
WHERE o.id = m.organization_id AND o.owner_user_id IS NULL;
DO $$
BEGIN
    -- Created last: the detection arm keys on it.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_organization_owner_user') THEN
        ALTER TABLE organization ADD CONSTRAINT fk_organization_owner_user
            FOREIGN KEY (owner_user_id) REFERENCES "user" (id) ON DELETE SET NULL NOT VALID;
    END IF;
END $$;
ALTER TABLE organization VALIDATE CONSTRAINT fk_organization_owner_user;
"""

DOWNGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE organization DROP CONSTRAINT IF EXISTS fk_organization_owner_user;
ALTER TABLE organization DROP COLUMN IF EXISTS owner_user_id;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
