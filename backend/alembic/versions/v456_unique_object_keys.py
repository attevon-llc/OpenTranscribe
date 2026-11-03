"""One owning row per stored object key: partial unique indexes on ``media_file`` (issue #1357).

Adds ``uq_media_file_storage_path``, ``uq_media_file_thumbnail_path`` and
``uq_media_file_playback_path`` (each ignoring NULL and the empty placeholder), so two rows can
no longer name one object. Deleting an object removes every version of its key, which is why a
shared key was a data-loss shape rather than a cosmetic one.

THIS REVISION NEVER BLOCKS STARTUP. It looks for duplicates first:

* None: all three indexes are built.
* Shared ``thumbnail_path`` / ``playback_path`` (legacy URL-import thumbnails): the lowest-id row
  keeps the key, the others are set to NULL and a batch task regenerates their thumbnails.
* Shared ``storage_path`` (the original upload, which cannot be regenerated): that index is
  SKIPPED, an ERROR is logged, and the condition is reported by
  ``GET /api/admin/data-integrity/storage/keys``. ``python -m app.scripts.split_shared_storage_paths
  --apply`` gives each extra row its own copy of the object; the next start builds the index.

The indexes are built inside this migration's transaction, so a plain ``CREATE UNIQUE INDEX``
holds a SHARE lock on ``media_file`` (writes wait, reads work) for the build. A concurrent build
cannot run in a transaction; the startup retry (``app.db.migrations._retry_object_key_indexes``)
therefore builds any index this revision skipped with ``CREATE UNIQUE INDEX CONCURRENTLY`` and
repairs an INVALID one.

The SQL lives in ``app.db.object_key_indexes`` (shared with the startup retry, the status endpoint
and the script). All statements are idempotent.

The downgrade drops the three indexes. It destroys nothing; thumbnails cleared by the upgrade are
not restored.
"""

import logging

from alembic import op
from app.db import object_key_indexes

revision = "v456_unique_object_keys"
down_revision = "v455_add_self_service_erasure_request"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()
    cleared = object_key_indexes.clear_regenerable_duplicates(conn)
    if any(cleared.values()):
        logger.warning(
            "v456: cleared %s duplicate legacy key(s); thumbnails are regenerated in the background",
            cleared,
        )
    object_key_indexes.ensure_indexes(conn)


def downgrade() -> None:
    for index in object_key_indexes.INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index.name}")
