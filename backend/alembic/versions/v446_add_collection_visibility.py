"""Add a visibility mode to collections: organization-wide or restricted (issue #1275).

Why
---
An organization collection is shared by every member of the organization (v422). Some
collections must not be: a legal matter, an HR review. ``collection.visibility`` lets the
creator or an organization admin restrict one to the creator, the organization admins and
the people it is explicitly shared with (``collection_share``, which already exists for
exactly this); everyone else in the organization neither sees nor can open it.

What
----
* ``collection.visibility`` ``VARCHAR(20) NOT NULL DEFAULT 'organization'``. Every existing
  row, and every row an older writer inserts, is organization-wide, so nothing changes
  until someone restricts a collection.
* ``ck_collection_visibility``: the value is one of the two modes, and ``restricted`` only
  exists on an organization collection (a personal collection has no organization to hide
  it from).
* ``ix_collection_org_visibility (organization_id, visibility)`` for the per-organization
  visibility scan every collection list makes.

Downgrade drops all three. Restricted collections become organization-wide again, which
widens who can see them: restore from a backup instead if that is not acceptable.

Revision ID: v446_add_collection_visibility
Revises: v444_add_takedown_notification
Create Date: 2026-10-25
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v446_add_collection_visibility"
down_revision = "v444_add_takedown_notification"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    ALTER TABLE collection
        ADD COLUMN IF NOT EXISTS visibility VARCHAR(20) NOT NULL DEFAULT 'organization';

    CREATE INDEX IF NOT EXISTS ix_collection_org_visibility
        ON collection (organization_id, visibility);

    DO $$
    BEGIN
        -- Created last: the detection arm keys on it.
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_collection_visibility'
        ) THEN
            ALTER TABLE collection ADD CONSTRAINT ck_collection_visibility
                CHECK (
                    visibility IN ('organization', 'restricted')
                    AND (visibility = 'organization' OR organization_id IS NOT NULL)
                );
        END IF;
    END $$;
"""

DOWNGRADE_SQL = """
    ALTER TABLE collection DROP CONSTRAINT IF EXISTS ck_collection_visibility;
    DROP INDEX IF EXISTS ix_collection_org_visibility;
    ALTER TABLE collection DROP COLUMN IF EXISTS visibility;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
