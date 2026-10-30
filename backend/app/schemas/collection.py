"""Collection wire schemas: create/update bodies, the collection shapes and membership bodies.

``CollectionResponse`` (a collection carrying its media files) lives in ``media.py`` beside
``MediaFile``, which this module must not import (``MediaFileDetail`` embeds ``Collection``).
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.schemas.base import UUIDBaseSchema
from app.schemas.content_hold import HoldSummary
from app.schemas.user import UserBrief


# Collection schemas
class CollectionBase(BaseModel):
    name: str
    description: str | None = None
    is_public: bool = False


CollectionVisibilityMode = Literal["organization", "restricted"]


class CollectionCreate(CollectionBase):
    default_prompt_id: UUID | None = None
    visibility: CollectionVisibilityMode = Field(
        default="organization",
        description=(
            "'organization' (every member of the organization sees it) or 'restricted' "
            "(only its creator, the organization admins and the people it is shared with). "
            "A personal collection can only be 'organization': anything else is a 422."
        ),
    )


class CollectionVisibilityUpdate(BaseModel):
    """Body of ``PATCH /collections/{uuid}/visibility``."""

    visibility: CollectionVisibilityMode


class CollectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_public: bool | None = None
    default_prompt_id: UUID | None = None


class Collection(CollectionBase, UUIDBaseSchema):
    """Collection with UUID as public identifier.

    ``user_id`` is the creator's UUID; NULL for an organization collection whose
    creator's account is gone (the collection stays with the organization).
    """

    user_id: UUID | None = None
    default_prompt_id: UUID | None = None
    default_prompt_name: str | None = None
    source: str | None = None
    visibility: CollectionVisibilityMode = "organization"
    # Server-computed: the caller may switch ``visibility`` (an organization collection they
    # created, or any of their organization's as an admin). Never re-derive it client-side.
    can_change_visibility: bool = False
    #: An organization hold on the collection itself (issue #1276); never a reason.
    hold: HoldSummary = Field(default_factory=HoldSummary)
    created_at: datetime
    updated_at: datetime


class CollectionWithCount(Collection):
    media_count: int = 0
    is_shared: bool = False  # True if shared with (not owned by) the caller
    my_permission: str = "owner"  # caller's effective permission
    shared_by: UserBrief | None = None  # who shared it (for shared collections)
    share_count: int = 0  # Number of shares on this collection


class CollectionMemberAdd(BaseModel):
    media_file_ids: list[UUID]  # Changed from int to UUID


class CollectionMemberRemove(BaseModel):
    media_file_ids: list[UUID]  # Changed from int to UUID


class CollectionOnSelection(BaseModel):
    """A collection holding some or all of a selection of files.

    The mirror of :class:`TagOnSelection`, so the gallery's two organizing
    modals report membership in the same shape.
    """

    uuid: UUID
    name: str
    file_count: int = 0
    selection_size: int = 0

    model_config = ConfigDict(from_attributes=True)
