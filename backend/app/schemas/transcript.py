"""Transcript segment wire schemas: segment, edit body, grouping and the paged response."""

from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.schemas.base import UUIDBaseSchema
from app.schemas.speaker import Speaker


class SegmentSpeakerUpdate(BaseModel):
    """Schema for updating a transcript segment's speaker assignment.

    Attributes:
        speaker_uuid: UUID of the speaker to assign (null to unassign speaker)
    """

    speaker_uuid: str | None = Field(
        None, description="UUID of the speaker to assign to this segment (null to unassign)"
    )


class TranscriptSegmentBase(BaseModel):
    start_time: float
    end_time: float
    text: str
    speaker_id: UUID | None = None
    is_overlap: bool = False
    overlap_group_id: UUID | None = None
    overlap_confidence: float | None = None


class TranscriptSegmentUpdate(BaseModel):
    """Fields a client may edit via ``PUT .../transcript/segments/{segment_uuid}``.

    Deliberately excludes ``id`` (the segment is identified by the path UUID, and the
    primary key must never be client-writable) and ``speaker_id`` (speaker reassignment
    has its own endpoint, ``PUT /transcripts/segments/{uuid}/speaker``, which resolves
    the UUID to the ORM's integer FK correctly — see issue #722). ``extra="forbid"`` so a
    client sending either (or any other unsupported field) gets a 422, not a silently
    dropped write.
    """

    model_config = ConfigDict(extra="forbid")

    start_time: float | None = None
    end_time: float | None = None
    text: str | None = None


class TranscriptSegment(TranscriptSegmentBase, UUIDBaseSchema):
    """Transcript segment with UUID as public identifier"""

    media_file_id: UUID
    speaker: Speaker | None = None
    confidence: float | None = None  # ASR confidence score (0.0–1.0)

    # Formatted fields for frontend display
    formatted_timestamp: str | None = None  # e.g., "0:45.2"
    display_timestamp: str | None = None  # e.g., "0:45.2" for transcript UI
    speaker_label: str | None = (
        None  # ALWAYS original speaker ID (e.g., "SPEAKER_01") for color consistency
    )
    resolved_speaker_name: str | None = None  # Display name (user label or original ID)

    # Content redaction (text above is already masked at read time when redaction is on).
    # `redactions` carries the spans that were applied so the UI can render blur/tooltips.
    redactions: list[dict] | None = None
    toxicity: dict | None = None  # Segment-level toxicity scores (for badge/flag UI)


class GroupedTranscriptSegment(BaseModel):
    """A display group of transcript segments, referencing them by UUID.

    Consecutive segments sharing an ``overlap_group_id`` (with more than one member)
    are collapsed into a single overlap group; every other segment is its own
    single-member group. This is the authoritative grouping — the SPA renders it
    directly rather than recomputing it.

    Groups carry **UUID references**, never copies. ``transcript_segments`` is the
    single representation of segment data on the wire and on the client; embedding
    copies here previously gave the SPA two objects per segment, and every optimistic
    update patched only one of them — the transcript then rendered stale names and
    text until a full page reload (issue #352).

    Attributes:
        is_overlap_group: True when this group represents an overlapping-speech cluster.
        overlap_group_id: The shared overlap group id. Set on every group that has one,
            including single-member groups, so a group split across a pagination
            boundary can be stitched back together by the client.
        start_time: Minimum start time across the group's segments.
        end_time: Maximum end time across the group's segments.
        start_segment_index: Index of the first segment of this group in the flat
            transcript list, **global** across pagination (used by the frontend for
            reading-progress tracking).
        segment_uuids: UUIDs of the segments in this group, in order. Resolve them
            against ``transcript_segments``.
    """

    is_overlap_group: bool = False
    overlap_group_id: UUID | None = None
    start_time: float
    end_time: float
    start_segment_index: int
    segment_uuids: list[UUID] = []


class TranscriptSegmentsPage(BaseModel):
    """One page of transcript segments, as served by ``GET /files/{uuid}/segments``.

    Mirrors the transcript half of ``MediaFileDetail``: the flat segment list plus the
    grouping that references it. ``grouped_segments`` is always present (empty when the
    transcript is withheld) so the SPA can concatenate pages without a null guard.

    Attributes:
        transcript_segments: This page's segments, in order.
        grouped_segments: Display grouping for this page, with **global**
            ``start_segment_index`` values.
        total_segments: Total segment count for the file, across all pages.
        redaction_pending: True when the transcript is withheld because redaction
            detection has not finished.
        redaction_status: Redaction pipeline status, when withheld.
    """

    transcript_segments: list[TranscriptSegment] = []
    grouped_segments: list[GroupedTranscriptSegment] = []
    total_segments: int = 0
    redaction_pending: bool = False
    redaction_status: str | None = None
