"""Wire shapes for the tag API.

A tag is identified on the wire by its ``uuid``; the integer id never leaves the
server. ``ownership`` is always computed for the caller by
``tag_scope.tag_ownership`` and passed in explicitly.
"""

from __future__ import annotations

from typing import Any
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from app.schemas.base import UUIDBaseSchema

OwnershipLabel = Literal["mine", "system", "shared_with_me"]


class TagBase(BaseModel):
    """A tag name as supplied by a client."""

    name: str


class Tag(TagBase, UUIDBaseSchema):
    """A tag as served by every tag-carrying response."""

    source: str | None = Field(
        default=None,
        description="How the tag came about: manual, auto_ai or ai_accepted (null = legacy manual).",
    )
    ownership: OwnershipLabel = Field(
        default="mine",
        description="The tag's relation to the caller: their own, system vocabulary, "
        "or someone else's that they can see.",
    )

    @model_validator(mode="before")
    @classmethod
    def _from_orm_row(cls, data: Any) -> Any:
        """Read the declared fields off an ORM row.

        A row without an explicit ``ownership`` gets a caller-blind guess from
        its owner column. Responses never rely on it: an organization tag needs
        the caller to classify, so endpoints always pass ``ownership`` in.
        """
        if isinstance(data, dict) or not hasattr(data, "user_id"):
            return data
        values = {name: getattr(data, name) for name in cls.model_fields if hasattr(data, name)}
        if "ownership" not in values:
            values["ownership"] = "system" if data.user_id is None else "mine"
        return values


class TagWithCount(Tag):
    """A tag in the caller's list, with how many of their files carry it."""

    usage_count: int = Field(default=0, description="Accessible files carrying the tag.")


class TagShareTarget(BaseModel):
    """One grant of a tag to a user or a group."""

    uuid: UUID
    target_type: Literal["user", "group"]
    display_name: str
    shared_by: str | None = None


class TagShareCreate(BaseModel):
    """Grant a tag to exactly one user or one group."""

    target_user_uuid: UUID | None = None
    target_group_uuid: UUID | None = None


class TaggedFile(BaseModel):
    """A file carrying a tag, as listed from the tag's side."""

    model_config = ConfigDict(from_attributes=True)

    uuid: UUID
    display_title: str
    status: str | None = None
    formatted_duration: str | None = None


class TagFileList(BaseModel):
    """A page of the files carrying one tag."""

    files: list[TaggedFile] = []
    total: int = Field(default=0, description="Every accessible file carrying the tag.")


class TagOnSelection(Tag):
    """A tag carried by at least one file of a gallery selection."""

    file_count: int = Field(default=0, description="Selected files that carry the tag.")
    selection_size: int = Field(default=0, description="Distinct files in the selection.")


class TagClusterMember(Tag):
    """A tag in a cluster of rows that share one normalized name."""

    usage_count: int = 0
    suggested_survivor: bool = Field(
        default=False, description="The member a merge of this cluster should keep."
    )


class TagClusterSuggestion(Tag):
    """A near-match tag offered next to a cluster; never a member of it."""

    usage_count: int = 0
    similarity: float = Field(default=0.0, description="Name similarity ratio, 0 to 1.")


class TagCollisionCluster(BaseModel):
    """Tags whose names normalize to the same form."""

    model_config = ConfigDict(from_attributes=True)

    normalized_name: str
    members: list[TagClusterMember] = []
    suggested_survivor_uuid: UUID | None = None
    suggestions: list[TagClusterSuggestion] = []


class TagImpactEntry(BaseModel):
    """How many files one tag is on, for the caller and overall."""

    model_config = ConfigDict(from_attributes=True)

    uuid: UUID
    name: str
    accessible_file_count: int
    total_file_count: int


class TagImpact(BaseModel):
    """What a destructive tag operation would touch, before it runs."""

    model_config = ConfigDict(from_attributes=True)

    tags: list[TagImpactEntry] = []
    accessible_file_count: int = Field(
        default=0, description="Distinct files the caller can open that carry any of the tags."
    )
    total_file_count: int = Field(
        default=0, description="Distinct files carrying any of the tags, across all accounts."
    )


class TagRenameRequest(BaseModel):
    """New name for a tag."""

    name: str
    confirm_merge: bool = Field(
        default=False,
        description="Fold the tag into an existing one when the new name already exists.",
    )


class TagMergeRequest(BaseModel):
    """Tags to fold into the target tag."""

    source_uuids: list[UUID] = Field(min_length=1)


class TagPromoteRequest(BaseModel):
    """Tags to move into the shared system vocabulary."""

    tag_uuids: list[UUID] = Field(min_length=1)


class TagMutationResult(BaseModel):
    """Outcome of a rename, merge, delete or promote."""

    model_config = ConfigDict(from_attributes=True)

    tag: Tag | None = None
    merged: bool = False
    requires_confirmation: bool = Field(
        default=False,
        description="Nothing was changed: the new name belongs to another tag, which "
        "is returned in ``tag``. Repeat with confirm_merge to fold into it.",
    )
    deleted_uuids: list[UUID] = []
    impact: TagImpact
