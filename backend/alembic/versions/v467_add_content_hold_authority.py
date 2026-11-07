"""Who placed a content hold: ``content_hold.authority`` (organization or platform).

Why
---
Until now a ``content_hold`` row could only be an organization's (``organization_id`` NOT NULL,
v447). A deployment operator who had to preserve a file for a legal claim had one tool: the
takedown's ``media_file.legal_hold`` flag, which comes with a quarantine (the file disappears for
its owner), has no reason, no matter reference, no collection scope and no release record. The
community edition has no organization holds at all, so a self-hosted administrator could not hold
a file without taking it down.

This revision lets a hold be the PLATFORM's (placed by an instance administrator through
``/api/admin/holds``) as well as an organization's. A platform hold is an ordinary
``content_hold`` row, so it gets the collection-membership listener, the storage object lock, the
erasure nudge and every destroy-path guard (``services/hold_guard``) with no further change. The
takedown flag stays what it is: the takedown's own hold.

- ``authority VARCHAR(20) NOT NULL DEFAULT 'organization'``: every existing row is an
  organization's, so the default backfills them correctly (PostgreSQL 11+ stores a constant
  default without rewriting the table).
- ``organization_id`` becomes NULLable: a platform hold belongs to no organization.
- ``ck_content_hold_authority``: ``authority IN ('organization', 'platform')``.
- ``ck_content_hold_authority_org``: ``(authority = 'organization') = (organization_id IS NOT
  NULL)``, so an organization hold always names its organization and a platform hold never does.
  Every existing row satisfies both checks.

Safe on a populated table: both constraints are added ``NOT VALID`` and then validated, and every
statement runs under a 30 s ``lock_timeout``. The partial index
``ix_content_hold_platform_active`` (the platform screen's "active holds" read) is NOT built here:
the runner builds it ``CONCURRENTLY`` after the upgrade (``app.db.hold_indexes``), like v463's
trash index, and no detection rung requires it.

Detection marker: ``ck_content_hold_authority_org``, created last.

The downgrade REFUSES while any platform hold row exists (released ones included: they are the
operator's record, and ``organization_id`` cannot become NOT NULL again while they exist). With
none, it drops the index, both constraints and the column and restores ``organization_id NOT
NULL``.

Community edition: platform holds are the only kind an instance administrator can place; the
organization routes stay behind ``org.legal_hold``.

Revision ID: v467_add_content_hold_authority
Revises: v465_add_user_hidden_media
Create Date: 2026-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v467_add_content_hold_authority"
down_revision = "v465_add_user_hidden_media"
branch_labels = None
depends_on = None

#: Frozen copy of ``models.content_hold.HOLD_AUTHORITIES``; the consistency test asserts equality.
AUTHORITIES = ("organization", "platform")

#: Module-level so the consistency test can replay the real statements.
UPGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE content_hold
    ADD COLUMN IF NOT EXISTS authority VARCHAR(20) NOT NULL DEFAULT 'organization';
ALTER TABLE content_hold ALTER COLUMN organization_id DROP NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_content_hold_authority') THEN
        ALTER TABLE content_hold ADD CONSTRAINT ck_content_hold_authority
            CHECK (authority IN ('organization', 'platform')) NOT VALID;
    END IF;
END $$;
ALTER TABLE content_hold VALIDATE CONSTRAINT ck_content_hold_authority;
DO $$
BEGIN
    -- Created last: the detection arm keys on it.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_content_hold_authority_org') THEN
        ALTER TABLE content_hold ADD CONSTRAINT ck_content_hold_authority_org
            CHECK ((authority = 'organization') = (organization_id IS NOT NULL)) NOT VALID;
    END IF;
END $$;
ALTER TABLE content_hold VALIDATE CONSTRAINT ck_content_hold_authority_org;
"""

#: Refuses before anything is dropped. Dynamic SQL, because a second downgrade runs it against a
#: table that no longer has the column.
REFUSE_SQL = """
DO $$
DECLARE
    platform_rows integer;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'content_hold' AND column_name = 'authority'
    ) THEN
        EXECUTE 'SELECT count(*) FROM content_hold WHERE authority = ''platform'''
            INTO platform_rows;
        IF platform_rows > 0 THEN
            RAISE EXCEPTION 'Cannot downgrade v467: % platform legal hold row(s) exist. '
                'They have no organization, so organization_id cannot become NOT NULL again. '
                'Export the hold records and delete them first.', platform_rows;
        END IF;
    END IF;
END $$;
"""

DOWNGRADE_SQL = (
    REFUSE_SQL
    + """
SET LOCAL lock_timeout = '30s';
DROP INDEX IF EXISTS ix_content_hold_platform_active;
ALTER TABLE content_hold DROP CONSTRAINT IF EXISTS ck_content_hold_authority_org;
ALTER TABLE content_hold DROP CONSTRAINT IF EXISTS ck_content_hold_authority;
ALTER TABLE content_hold ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE content_hold DROP COLUMN IF EXISTS authority;
"""
)


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
