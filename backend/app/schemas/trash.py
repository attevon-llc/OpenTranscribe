"""Wire contracts of the trash routes (issue #1101). Ids are uuids; no filename is audited, but
the list shows the caller the filename of a file they may already restore."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TrashedFile(BaseModel):
    """One row of ``GET /files/trash``."""

    uuid: str
    filename: str
    title: str | None = None
    owner_uuid: str | None = None
    owner_name: str | None = None
    deleted_at: datetime
    purge_after: datetime
    trash_origin: str
    deleted_by_uuid: str | None = None
    deleted_by_name: str | None = None
    #: Under a legal or organization hold: restorable, never purged while held.
    held: bool
    can_restore: bool
    can_purge: bool


class TrashPage(BaseModel):
    """A page of the caller's trash, newest deletion first."""

    items: list[TrashedFile]
    total: int
    page: int
    page_size: int
    #: The current trash window, for "purged in N days" copy on files trashed from now on.
    retention_days: int


class TrashPurgeQueued(BaseModel):
    """``202`` body of "delete forever" and "empty trash": the files made due now."""

    queued: int
