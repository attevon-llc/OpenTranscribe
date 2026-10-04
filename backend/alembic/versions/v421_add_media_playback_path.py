"""Add media_file.playback_path: a browser-playable rendition of the original.

Some accepted formats do not play in Firefox or Chromium (AIFF, WMA, ALAC, AVI, WMV, ...).
Preprocessing now queues an AAC/M4A rendition for exactly those files and records its
object key here. ``/stream-url`` serves it, and the original stays the download.

Nullable with no default: NULL means "play the original", which is right for every
existing row. Rows uploaded before this revision get no rendition until they are
reprocessed.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "v421_add_media_playback_path"
down_revision = "v420_add_tag_organization_id"
branch_labels = None
depends_on = None

#: Module-level so the consistency test replays the real statement.
UPGRADE_SQL = """
    ALTER TABLE media_file
        ADD COLUMN IF NOT EXISTS playback_path VARCHAR;
"""

DOWNGRADE_SQL = """
    ALTER TABLE media_file
        DROP COLUMN IF EXISTS playback_path;
"""


def upgrade():
    op.execute(UPGRADE_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
