"""Per-user hide of media files (issue #1102).

Why
---
A user who can read a file (their own, or one shared with them) may want it out of their own
gallery without deleting it for anyone. Hide is a per-user VIEW preference, not access control:
it never changes what anyone else sees, never blocks a direct link, and never removes a file a
person explicitly picked for chat. A relation, not a column, because it is per user.

``user_hidden_media`` holds one row per (user, file):

- ``user_id``: ``ON DELETE CASCADE``, a deleted account leaves no hide rows.
- ``media_file_id``: ``ON DELETE CASCADE``, a purged file takes everyone's hide rows with it.
- ``pk_user_hidden_media (user_id, media_file_id)``: the ``ON CONFLICT DO NOTHING`` target, so
  hiding twice is a no-op; also the index of "this user's hidden files".
- ``ix_user_hidden_media_media_file``: the purge cascade and the per-file anti-join.

A new table, so nothing is backfilled and no existing row is locked. The per-user cap
(``HIDDEN_MEDIA_PER_USER_CAP``) is enforced at write time by ``services/hidden_media_service``,
which keeps the OpenSearch ``terms`` exclusion bounded.

Idempotent. The detection marker is ``pk_user_hidden_media``, created with the table. The
downgrade drops the table: every user's hidden files become visible again, nothing else changes.

Revision ID: v465_add_user_hidden_media
Revises: v464_generalise_erasure_grace_index
Create Date: 2026-11-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v465_add_user_hidden_media"
down_revision = "v464_generalise_erasure_grace_index"
branch_labels = None
depends_on = None

#: Module-level so a consistency test can replay the real statements.
UPGRADE_SQL = """
SET LOCAL lock_timeout = '30s';
CREATE TABLE IF NOT EXISTS user_hidden_media (
    user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    media_file_id INTEGER NOT NULL REFERENCES media_file(id) ON DELETE CASCADE,
    hidden_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT pk_user_hidden_media PRIMARY KEY (user_id, media_file_id)
);
CREATE INDEX IF NOT EXISTS ix_user_hidden_media_media_file ON user_hidden_media (media_file_id);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS user_hidden_media;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
