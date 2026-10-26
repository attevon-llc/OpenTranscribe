"""Add organization content holds: a hold on a file or on a whole collection (issue #1276).

Why
---
``media_file.legal_hold`` is the platform's hold: a boolean the operator sets while a takedown
is open. An organization has no equivalent of its own. A legal or HR matter needs "nobody
deletes this recording, not even its owner and not retention" without involving the operator,
and it needs a hold that can cover a whole collection, including what is added later.

What
----
* ``content_hold``: one row per hold an organization administrator placed. ``scope`` is
  ``file`` or ``collection``. The reason is administrator-only text (never audited, never sent
  over a socket). A hold is active while ``released_at IS NULL`` and is released, never
  deleted: the row is the history.
* ``content_hold_file``: the files a hold covers. A file hold lists its files; a collection
  hold lists the collection's files as they were placed and as they are added later.
  ``(media_file_id)`` is indexed because the question every delete asks is "is THIS file
  held".

Platform holds stay on ``media_file.legal_hold``; ``app/services/hold_guard.py`` is the one
place that answers "is this file held by anyone".

Foreign keys: ``organization_id`` CASCADE (a hold has no meaning without its organization,
and ``erase_organization`` refuses to run while one is active); ``collection_id`` SET NULL
(the hold outlives a deleted collection's row as history); the two actor keys SET NULL (v387
convention: attribution goes, the record stays); ``content_hold_file`` CASCADEs from both
parents.

Downgrade DROPS both tables and with them every hold and its history. It exists for the
consistency test and for a developer rolling back before any hold was placed. Never run it
against a deployment that has placed one: every file such a hold protected becomes deletable.

Revision ID: v447_add_content_hold
Revises: v446_add_collection_visibility
Create Date: 2026-10-25
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v447_add_content_hold"
down_revision = "v446_add_collection_visibility"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statements.
UPGRADE_SQL = """
    CREATE TABLE IF NOT EXISTS content_hold (
        id SERIAL PRIMARY KEY,
        uuid UUID NOT NULL,
        organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
        scope VARCHAR(20) NOT NULL,
        collection_id INTEGER REFERENCES collection(id) ON DELETE SET NULL,
        reason TEXT NOT NULL,
        matter_ref VARCHAR(255),
        placed_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
        placed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        released_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
        released_at TIMESTAMPTZ,
        release_reason TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ix_content_hold_uuid ON content_hold (uuid);
    CREATE INDEX IF NOT EXISTS ix_content_hold_org_active
        ON content_hold (organization_id) WHERE released_at IS NULL;
    CREATE INDEX IF NOT EXISTS ix_content_hold_collection
        ON content_hold (collection_id) WHERE collection_id IS NOT NULL;

    CREATE TABLE IF NOT EXISTS content_hold_file (
        hold_id INTEGER NOT NULL REFERENCES content_hold(id) ON DELETE CASCADE,
        media_file_id INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
        added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (hold_id, media_file_id)
    );
    CREATE INDEX IF NOT EXISTS ix_content_hold_file_media_file
        ON content_hold_file (media_file_id);

    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_content_hold_scope') THEN
            ALTER TABLE content_hold ADD CONSTRAINT ck_content_hold_scope
                CHECK (scope IN ('file', 'collection'));
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_content_hold_reason_length') THEN
            ALTER TABLE content_hold ADD CONSTRAINT ck_content_hold_reason_length
                CHECK (char_length(reason) BETWEEN 10 AND 2000);
        END IF;
        -- Created last: the detection arm keys on it.
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_content_hold_collection_scope'
        ) THEN
            ALTER TABLE content_hold ADD CONSTRAINT ck_content_hold_collection_scope
                CHECK (scope = 'collection' OR collection_id IS NULL);
        END IF;
    END $$;
"""

DOWNGRADE_SQL = """
    DROP TABLE IF EXISTS content_hold_file;
    DROP TABLE IF EXISTS content_hold;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
