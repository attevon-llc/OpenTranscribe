"""Wire shapes for organization content holds (issue #1276).

Two audiences, kept apart on purpose. ``HoldSummary`` is on every file and collection response
and tells ANYONE who can read the row whether it is held and by whom: no reason, no matter
reference, and the hold uuids only when the caller administers the holding organization.
``HoldOut`` is the administrator's own record (reason and matter reference included) and exists
only on ``/org-admin/holds``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator

from app.models.content_hold import REASON_MAX_LENGTH
from app.models.content_hold import REASON_MIN_LENGTH

MAX_FILES_PER_HOLD = 500


class HoldSummary(BaseModel):
    """Whether a file or collection is held, for the badge and the refusal messages."""

    held: bool = False
    #: ``platform`` and/or ``organization``. Empty when not held.
    held_by: list[str] = []
    #: Organization hold uuids, present only for an administrator of that organization.
    hold_uuids: list[str] = []


class HoldCreate(BaseModel):
    """Place a hold on named files, or on a whole collection."""

    scope: Literal["file", "collection"]
    file_uuids: list[UUID] = Field(default_factory=list, max_length=MAX_FILES_PER_HOLD)
    collection_uuid: UUID | None = None
    reason: str = Field(
        ...,
        min_length=REASON_MIN_LENGTH,
        max_length=REASON_MAX_LENGTH,
        description="Why the hold exists. Visible to this organization's administrators only.",
    )
    matter_ref: str | None = Field(
        None, max_length=255, description="An optional matter or case reference."
    )

    @model_validator(mode="after")
    def _scope_names_its_target(self) -> HoldCreate:
        if self.scope == "file" and (not self.file_uuids or self.collection_uuid is not None):
            raise ValueError("a file hold names files and no collection")
        if self.scope == "collection" and (self.collection_uuid is None or self.file_uuids):
            raise ValueError("a collection hold names one collection and no files")
        return self


class HoldRelease(BaseModel):
    reason: str = Field(
        ...,
        min_length=REASON_MIN_LENGTH,
        max_length=REASON_MAX_LENGTH,
        description="Why the hold is being released. Kept on the hold record.",
    )


class HeldFile(BaseModel):
    uuid: UUID
    title: str | None = None
    filename: str | None = None


class HoldOut(BaseModel):
    """An administrator's view of one hold."""

    uuid: UUID
    scope: str  # "file" or "collection"
    #: Set for a collection hold while the collection exists.
    collection_uuid: UUID | None = None
    collection_name: str | None = None
    matter_ref: str | None = None
    reason: str
    placed_at: datetime
    #: A name (or an address only while still a member); ``None`` once the account is gone.
    placed_by_display: str | None = None
    released_at: datetime | None = None
    released_by_display: str | None = None
    release_reason: str | None = None
    active: bool
    file_count: int = 0
    #: Present on the single-hold read only, capped; the list carries counts.
    files: list[HeldFile] | None = None


class HoldList(BaseModel):
    holds: list[HoldOut]
    total: int
