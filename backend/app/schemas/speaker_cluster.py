"""Pydantic schemas for speaker clustering."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field

# --- Speaker Cluster ---


class SpeakerClusterBase(BaseModel):
    """Base schema for speaker clusters."""

    label: str | None = None
    description: str | None = None


class SpeakerClusterUpdate(BaseModel):
    """Schema for updating a speaker cluster."""

    label: str | None = None
    description: str | None = None


class SpeakerClusterMemberResponse(BaseModel):
    """Response schema for a cluster member."""

    uuid: UUID
    speaker_uuid: UUID
    speaker_name: str
    display_name: str | None = None
    suggested_name: str | None = None
    media_file_uuid: UUID | None = None
    media_file_title: str | None = None
    confidence: float = 0.0
    verified: bool = False
    predicted_gender: str | None = None
    predicted_age_range: str | None = None
    gender_confidence: float | None = None
    gender_confirmed_by_user: bool = False
    has_audio_clip: bool = False
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class GenderComposition(BaseModel):
    """Gender composition summary for a cluster."""

    male_count: int = 0
    female_count: int = 0
    unknown_count: int = 0
    total_with_gender: int = 0
    dominant_gender: str | None = None
    gender_coherence: float | None = None
    gender_label: str | None = None
    has_gender_conflict: bool = False


class SpeakerClusterResponse(SpeakerClusterBase):
    """Response schema for a speaker cluster."""

    uuid: UUID
    user_id: int
    member_count: int = 0
    promoted_to_profile_id: int | None = None
    promoted_to_profile_uuid: UUID | None = None
    promoted_to_profile_name: str | None = None
    suggested_name: str | None = None
    quality_score: float | None = None
    gender_composition: GenderComposition | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Speaker Inbox ---


class SpeakerInboxItem(BaseModel):
    """Schema for an item in the unverified speakers inbox."""

    speaker_uuid: UUID
    speaker_name: str
    display_name: str | None = None
    suggested_name: str | None = None
    suggestion_source: str | None = None
    confidence: float | None = None
    media_file_uuid: UUID | None = None
    media_file_title: str | None = None
    media_file_duration: float | None = None
    cluster_uuid: UUID | None = None
    cluster_label: str | None = None
    cluster_member_count: int = 0
    verified: bool = False
    predicted_gender: str | None = None
    predicted_age_range: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


# --- Batch Operations ---


class MinorityAnalysisItem(BaseModel):
    """Analysis of a minority-gender speaker in a cluster."""

    speaker_uuid: UUID
    speaker_name: str
    predicted_gender: str
    sim_to_centroid: float
    avg_sim_to_majority: float
    avg_sim_to_minority_peers: float | None = None
    outlier_score: float
    recommendation: str  # likely_outlier, borderline, likely_valid


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


class PaginatedClusterResponse(BaseModel):
    """Paginated list of clusters."""

    items: list[SpeakerClusterResponse] = []
    total: int = 0
    page: int = 1
    per_page: int = 20
    pages: int = 0
    last_clustered_at: datetime | None = None
