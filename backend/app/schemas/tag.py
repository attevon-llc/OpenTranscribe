"""Tag wire schemas: the one tag shape, its sharing, cluster, impact and mutation bodies."""

from typing import Any
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from app.schemas.base import UUIDBaseSchema


class TagBase(BaseModel):
    name: str


class Tag(TagBase, UUIDBaseSchema):
    """Tag with UUID as public identifier.

    The single tag shape on the API: served by ``GET /api/tags`` (as
    ``TagWithCount``), ``POST /api/tags``, ``POST /api/tags/files/{uuid}/tags``,
    and — since issue #326 — ``MediaFileDetail.tags``. Tag names are unique only
    per owner (``v374_add_tag_user_id``), so ``uuid`` is the stable identifier.
    """

    source: str | None = Field(
        default=None,
        description=(
            "How the tag was applied: 'manual' for a user action, 'auto_ai' for the "
            "auto-labeling LLM. Null on tags created before this field existed."
        ),
    )
    ownership: Literal["mine", "system", "shared_with_me"] = Field(
        default="mine",
        description=(
            "The caller's relationship to this tag, and therefore what they may do "
            "with it. `mine` — they own it, full control. `system` — the shared "
            "vocabulary (`user_id IS NULL`) every account sees; admin-only to "
            "rename, merge, delete or promote. `shared_with_me` — owned by another "
            "account and visible only because it sits on a file shared with the "
            "caller; read-only, and any mutation answers 404. These are the same "
            "values `GET /tags?scope=` accepts, so a scoped request returns rows "
            "carrying that ownership. Derived per request, never stored."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _default_ownership(cls, data: Any) -> Any:
        """Project ownership onto the wire without exposing the owner's id.

        ``user_id`` is deliberately never serialized — which account owns a tag
        is nobody else's business, and the SPA only needs to know what it may do.

        This validator cannot see the **caller**, so it can only distinguish
        ``system`` from owned. That is sufficient for the endpoints that return a
        bare ORM row (`POST /tags`, `POST /tags/files/{uuid}/tags`): both return
        a tag the caller just resolved through ``owned_or_system``, so it is
        theirs or the system's by construction and can never be
        ``shared_with_me``. Every surface where the third value **is** reachable
        — the list, the collision clusters, the file-detail payload — computes it
        explicitly with ``tag_scope.tag_ownership(tag, user_id)`` and passes it
        in, which this then leaves alone.

        Runs **before** ``UUIDBaseSchema.prepare_uuid_response`` (a subclass's
        before-validator is the outer one), so it takes the raw ORM row and
        hands back a plain dict, which the base validator then passes through
        untouched. Fields are read from ``cls.model_fields`` rather than listed,
        so ``TagWithCount``/``TagClusterMember``/``TagClusterSuggestion`` keep
        their own extra fields without this needing to know about them.
        """
        if isinstance(data, dict) or not hasattr(data, "user_id"):
            return data
        values: dict[str, Any] = {
            name: getattr(data, name) for name in cls.model_fields if hasattr(data, name)
        }
        if "ownership" not in values:
            values["ownership"] = "system" if data.user_id is None else "mine"
        return values


class TagWithCount(Tag):
    """Tag with usage count for filtering UI.

    ``usage_count`` is scoped to the files the caller can access, and the unused
    filter is its exact complement — both read the same count, so a tag can
    never report ``0`` here while being absent from ``/tags/unused``.
    """

    usage_count: int = 0


class TagShareTarget(BaseModel):
    """Who a tag is shared with — one user or one group, never both."""

    uuid: UUID
    target_type: Literal["user", "group"]
    display_name: str
    shared_by: str | None = None


class TagShareCreate(BaseModel):
    """Grant a tag to a user or a group. Exactly one target."""

    target_user_uuid: UUID | None = None
    target_group_uuid: UUID | None = None


class TaggedFile(BaseModel):
    """A file carrying a tag, as the manager's "what it touches" list renders it.

    Deliberately thin: the manager needs a name to show and a uuid to link
    through on, not the full detail payload. `display_title` is pre-resolved
    here rather than in the SPA, matching the fat-backend rule the rest of the
    API follows.
    """

    uuid: UUID
    display_title: str
    status: str | None = None
    formatted_duration: str | None = None

    model_config = ConfigDict(from_attributes=True)


class TagFileList(BaseModel):
    """The files a tag touches, and how many there are in total.

    `total` is the real count while `files` is capped, so the UI can say
    "and N more" instead of silently truncating.
    """

    files: list[TaggedFile] = []
    total: int = 0


class TagOnSelection(Tag):
    """A tag carried by some or all of a set of selected files.

    The bulk apply surface needs to show what the selection **already** has, not
    just offer to add: `file_count` is how many of the selected files carry this
    tag, so the UI can distinguish a tag on every file from one on a few. `GET
    /api/files` deliberately carries no per-file tags (#326), so this is the only
    way that surface can know.
    """

    file_count: int = 0
    selection_size: int = 0

    @property
    def on_every_file(self) -> bool:
        """Whether removing it would clear the tag from the whole selection."""
        return self.selection_size > 0 and self.file_count == self.selection_size


class TagClusterMember(Tag):
    """A tag sharing its normalized name with the rest of its cluster.

    ``suggested_survivor`` marks the highest-usage member, preselected by the
    backend so the merge dialog opens on a decision rather than a blank choice.
    """

    usage_count: int = 0
    suggested_survivor: bool = False


class TagClusterSuggestion(Tag):
    """A near match offered beside a cluster, never inside it.

    Fuzzy similarity is non-transitive, so a suggestion is a prompt for a human,
    not evidence of membership.
    """

    usage_count: int = 0
    similarity: float = 0.0


class TagCollisionCluster(BaseModel):
    """Tags that normalize to one name, with the merge the backend recommends.

    Grouping is exact equality on the stored normalization, so repeated requests
    over unchanged data return the same clusters in the same order.
    """

    normalized_name: str
    members: list[TagClusterMember] = []
    suggested_survivor_uuid: UUID | None = None
    suggestions: list[TagClusterSuggestion] = []

    model_config = ConfigDict(from_attributes=True)


class TagImpactEntry(BaseModel):
    """File counts for a single tag in a pending rename / merge / delete."""

    uuid: UUID
    name: str
    accessible_file_count: int
    total_file_count: int

    model_config = ConfigDict(from_attributes=True)


class TagImpact(BaseModel):
    """What a destructive tag operation would touch, before it acts.

    Tags are **global** rows (no ``user_id``/``organization_id``, globally unique
    name), so the two counts are deliberately separate: ``accessible_*`` is what
    the caller can see, ``total_*`` is what the operation actually changes. A
    confirmation built only from the accessible number would read "3 files" in
    front of a delete that strips the tag from 500.
    """

    tags: list[TagImpactEntry] = []
    accessible_file_count: int = 0
    total_file_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class TagRenameRequest(BaseModel):
    """Rename a tag; ``confirm_merge`` accepts the merge when the name collides."""

    name: str
    confirm_merge: bool = False


class TagMergeRequest(BaseModel):
    """Fold one or more tags into the tag named in the path."""

    source_uuids: list[UUID] = Field(..., min_length=1)


class TagMutationResult(BaseModel):
    """Outcome of a rename / merge / delete, always carrying the impact."""

    tag: Tag | None = None
    merged: bool = False
    requires_confirmation: bool = False
    deleted_uuids: list[UUID] = []
    impact: TagImpact

    model_config = ConfigDict(from_attributes=True)


class TagPromoteRequest(BaseModel):
    """Publish one or more owned tags into the shared vocabulary.

    Admin-only: a shared tag appears in every account's picker, and same-named
    tags owned by other users are folded into the promoted row, so this changes
    what every account sees.
    """

    tag_uuids: list[UUID] = Field(..., min_length=1)
