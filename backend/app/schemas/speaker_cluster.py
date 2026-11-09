"""Pydantic schemas for speaker clustering."""

from uuid import UUID

from pydantic import BaseModel
from pydantic import Field

# --- Speaker Cluster ---


class SpeakerClusterUpdate(BaseModel):
    """Schema for updating a speaker cluster."""

    label: str | None = None
    description: str | None = None


# --- Speaker Inbox ---


# --- Batch Operations ---


class ClusterUnassignRequest(BaseModel):
    """Request to unassign speakers from a cluster."""

    speaker_uuids: list[UUID] = Field(..., min_length=1)
    blacklist: bool = Field(
        default=True, description="Prevent speakers from rejoining this cluster"
    )


class BatchVerifyRequest(BaseModel):
    """Request schema for batch speaker verification."""

    speaker_uuids: list[UUID] = Field(..., min_length=1)
    profile_uuid: UUID | None = None
    display_name: str | None = None
    action: str = Field(
        default="accept",
        description="Action: 'accept' (apply suggestion), 'assign' (assign to profile), 'name' (set display_name), 'skip' (mark as reviewed/skipped)",
    )
    override_unassignment: bool = Field(
        default=False,
        description=(
            "Lift the user's earlier unassignment or rejection that blocks this speaker. "
            "Only with exactly one speaker; a batch is refused (422). Without it, a "
            "blocked speaker is skipped and listed in blocked_speaker_uuids."
        ),
    )


class ReclusterRequest(BaseModel):
    """Request schema for triggering re-clustering."""

    force: bool = Field(
        default=False, description="Reserved for future use. Currently has no effect."
    )
    threshold: float | None = Field(
        None, ge=0.0, le=1.0, description="Clustering threshold (default 0.75)"
    )


class ClusterSplitRequest(BaseModel):
    """Request schema for splitting a cluster."""

    speaker_uuids: list[UUID] = Field(
        ..., min_length=1, description="Speaker UUIDs to split into new cluster"
    )


class ClusterPromoteRequest(BaseModel):
    """Request schema for promoting a cluster to a profile."""

    name: str = Field(
        ..., min_length=1, max_length=255, description="Name for the new speaker profile"
    )
    description: str | None = None


# --- Paginated Responses ---
