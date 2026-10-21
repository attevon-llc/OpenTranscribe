"""Wire shapes for the organization-scoped takedown routes (issue #1253).

Separate from the platform review-queue schemas in ``schemas/admin.py`` on purpose:
that queue carries the takedown reason, the acting operator's address and the
organization uuid for every row. An organization administrator sees none of those for
a file the OPERATOR holds, so the field is absent from the payload, not blanked by the
UI, and a shared serializer cannot null-fill it back in.
"""

from pydantic import BaseModel
from pydantic import Field


class OrgQuarantineRequest(BaseModel):
    """Take one of the caller's organization's files down.

    There is deliberately no ``legal_hold`` field: whether an organization takedown
    also places a legal hold is an open authorization decision (a hold blocks the
    owner's account deletion), so the organization route never places one.
    """

    reason: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Takedown reason (notice reference, policy clause, report id).",
    )


class OrgReleaseRequest(BaseModel):
    """Release a file the caller's organization took down."""

    reason: str | None = Field(
        None,
        max_length=2000,
        description="Why the file is being released. Recorded in the audit log only.",
    )


class OrgQuarantinedFile(BaseModel):
    """A file in the organization's takedown queue.

    ``quarantine_reason`` and ``quarantined_by_display`` are only present
    (``response_model_exclude_unset``) for a file an organization placed. For a file the
    operator holds the row says that it is held and by whom ("platform"), and that the
    caller cannot release it, and nothing about why.
    """

    uuid: str
    filename: str | None = None
    title: str | None = None
    owner_uuid: str
    owner_display: str
    quarantined_at: str | None = None
    legal_hold: bool = False
    is_quarantined: bool = True
    takedown_origin: str
    # Computed server-side so the UI never re-derives the authority table.
    can_release: bool
    quarantine_reason: str | None = None
    quarantined_by_display: str | None = None


class OrgQuarantinedFilesList(BaseModel):
    """Paginated takedown queue of one organization."""

    files: list[OrgQuarantinedFile]
    total: int
