"""Indexes on ``media_file`` that the runner builds concurrently after the upgrade (v463, #1101)."""

from __future__ import annotations

from app.db.concurrent_indexes import IndexSpec

#: The trash purge selector reads due rows by ``purge_after`` among trashed rows only, so a
#: partial index stays as small as the trash.
TRASH_DUE = IndexSpec(
    "ix_media_file_trash_due",
    "media_file",
    ("purge_after",),
    where="deleted_at IS NOT NULL",
)

TRASH_INDEXES = (TRASH_DUE,)
