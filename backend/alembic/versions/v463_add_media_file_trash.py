"""Trash state on ``media_file``: ``deleted_at``, ``deleted_by_user_id``, ``purge_after``, ``trash_origin``.

Why
---
Deleting a file destroys it immediately and irreversibly today (issue #1101, umbrella #1091). The
trash makes a person's delete recoverable for a window: the row stays, every read surface stops
showing it, and a beat task purges it through the existing ``purge_media_file`` once
``purge_after`` has passed. This revision only adds the state; the read rule that hides a trashed
file lives in ``app/services/file_availability.py`` and the trash/restore/purge behaviour arrives
with the trash service.

Trash is a lifecycle state on the row, not a ``status`` value (that is the processing state, and a
quarantine already keeps ``pre_quarantine_status`` to restore it) and not a separate table (every
read path already loads ``media_file``; a column predicate needs no join).

- ``deleted_at``: when the file was moved to the trash. NULL = live.
- ``deleted_by_user_id``: who moved it. FK ``ON DELETE SET NULL``: the trashed row must outlive
  the account of the person who trashed it. NULL is also valid for a system-initiated trash.
- ``purge_after``: stamped at trash time (``deleted_at`` + the trash window), so changing the
  window never shortens or extends a file already in the trash. The purge selector and the restore
  rule both key on it.
- ``trash_origin``: ``user`` (a person deleted it) or ``retention``. Retention currently keeps
  hard-deleting and never writes this column; the value stays allowed so the column shape does not
  have to change if that is revisited.

``ck_media_file_trash_state`` makes a half-trashed row unrepresentable: a live row has all four
NULL, a trashed row has ``deleted_at``, ``purge_after`` and ``trash_origin``.

Safe on a populated table: nullable columns with no default do not rewrite, every constraint is
added ``NOT VALID`` then validated (a validate takes only ``SHARE UPDATE EXCLUSIVE``), and every
statement runs under a 30 s ``lock_timeout``. No backfill: every existing row is live.

The partial index ``ix_media_file_trash_due`` the purge selector uses is NOT built here:
``CREATE INDEX CONCURRENTLY`` cannot run in a transaction and waits on every transaction that can
see ``media_file``, so the runner builds it after the upgrade (``app.db.trash_indexes``, through
``concurrent_indexes.ensure_concurrently``), exactly as v458 does for its speaker index.

Detection marker: ``ck_media_file_trash_state``, created last.

The downgrade drops the index, the constraints and the columns. It DESTROYS THE TRASH STATE: every
trashed file becomes live again. Purge or restore the trash first.

Revision ID: v463_add_media_file_trash
Revises: v461_add_privacy_purge_pending
Create Date: 2026-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v463_add_media_file_trash"
down_revision = "v462_add_speaker_identity_event"
branch_labels = None
depends_on = None

#: Frozen copy of ``models.media.TRASH_ORIGINS``; the consistency test asserts equality.
TRASH_ORIGINS = ("user", "retention")

#: Module-level so the consistency test can replay the real statements.
UPGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL;
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS deleted_by_user_id INTEGER NULL;
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS purge_after TIMESTAMPTZ NULL;
ALTER TABLE media_file ADD COLUMN IF NOT EXISTS trash_origin VARCHAR(16) NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_media_file_deleted_by_user') THEN
        ALTER TABLE media_file ADD CONSTRAINT fk_media_file_deleted_by_user
            FOREIGN KEY (deleted_by_user_id) REFERENCES "user"(id) ON DELETE SET NULL NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_trash_origin') THEN
        ALTER TABLE media_file ADD CONSTRAINT ck_media_file_trash_origin
            CHECK (trash_origin IS NULL OR trash_origin IN ('user', 'retention')) NOT VALID;
    END IF;
END $$;
ALTER TABLE media_file VALIDATE CONSTRAINT fk_media_file_deleted_by_user;
ALTER TABLE media_file VALIDATE CONSTRAINT ck_media_file_trash_origin;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_trash_state') THEN
        ALTER TABLE media_file ADD CONSTRAINT ck_media_file_trash_state CHECK (
            (deleted_at IS NULL AND purge_after IS NULL AND trash_origin IS NULL
                AND deleted_by_user_id IS NULL)
            OR (deleted_at IS NOT NULL AND purge_after IS NOT NULL AND trash_origin IS NOT NULL)
        ) NOT VALID;
    END IF;
END $$;
ALTER TABLE media_file VALIDATE CONSTRAINT ck_media_file_trash_state;
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS ix_media_file_trash_due;
ALTER TABLE media_file DROP CONSTRAINT IF EXISTS ck_media_file_trash_state;
ALTER TABLE media_file DROP CONSTRAINT IF EXISTS ck_media_file_trash_origin;
ALTER TABLE media_file DROP CONSTRAINT IF EXISTS fk_media_file_deleted_by_user;
ALTER TABLE media_file DROP COLUMN IF EXISTS trash_origin;
ALTER TABLE media_file DROP COLUMN IF EXISTS purge_after;
ALTER TABLE media_file DROP COLUMN IF EXISTS deleted_by_user_id;
ALTER TABLE media_file DROP COLUMN IF EXISTS deleted_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
