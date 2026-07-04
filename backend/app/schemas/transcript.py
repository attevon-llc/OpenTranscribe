"""Transcript-specific schemas for transcript segment operations."""

from pydantic import BaseModel
from pydantic import Field


class SegmentSpeakerUpdate(BaseModel):
    """Schema for updating a transcript segment's speaker assignment.

    Attributes:
        speaker_uuid: UUID of the speaker to assign (null to unassign speaker)
    """

    speaker_uuid: str | None = Field(
        None, description="UUID of the speaker to assign to this segment (null to unassign)"
    )
