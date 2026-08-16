from datetime import datetime
from enum import StrEnum
from typing import Any
from typing import Literal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.core.enums import FileStatus  # noqa: F401 — re-exported for backward compat
from app.schemas.base import UUIDBaseSchema
from app.schemas.user import UserBrief

VALID_LOCAL_WHISPER_MODELS = frozenset(
    {
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large-v1",
        "large-v2",
        "large-v3",
        "large-v3-turbo",
        # CrisperWhisper: only the CTranslate2 build loads in faster-whisper.
        # The PyTorch checkpoint (nyrahealth/CrisperWhisper) is deliberately omitted.
        "nyrahealth/faster_CrisperWhisper",
    }
)


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ReprocessRequest(BaseModel):
    """Request schema for reprocessing a file with optional speaker diarization settings.

    Attributes:
        stages: Pipeline stages to re-run. Empty list = full reprocess (backward compatible).
        min_speakers: Optional minimum number of speakers for diarization
        max_speakers: Optional maximum number of speakers for diarization
        num_speakers: Optional fixed number of speakers for diarization (overrides min/max)
    """

    stages: list[
        Literal[
            "transcription",
            "rediarize",
            "search_indexing",
            "analytics",
            "speaker_llm",
            "summarization",
            "topic_extraction",
        ]
    ] = Field(
        default_factory=list,
        description="Pipeline stages to re-run. Empty list = full reprocess (backward compatible)",
    )

    min_speakers: int | None = Field(
        None, description="Minimum number of speakers for diarization (positive integer)"
    )
    max_speakers: int | None = Field(
        None, description="Maximum number of speakers for diarization (positive integer)"
    )
    num_speakers: int | None = Field(
        None, description="Fixed number of speakers for diarization (overrides min/max when set)"
    )
    disable_diarization: bool | None = Field(
        None,
        description="Skip speaker diarization entirely",
    )
    whisper_model: str | None = Field(
        None,
        description="Whisper model to use for reprocessing. "
        "None = use admin-configured default. "
        "Only applies to local ASR provider.",
        examples=["tiny", "medium", "large-v2", "large-v3", "large-v3-turbo"],
    )

    @field_validator("min_speakers", "max_speakers", "num_speakers")
    @classmethod
    def validate_speaker_count_positive(cls, v: int | None) -> int | None:
        """Validate that speaker counts are positive integers (>= 1) if provided."""
        if v is not None and v < 1:
            raise ValueError("Speaker count must be at least 1")
        return v

    @field_validator("whisper_model")
    @classmethod
    def validate_whisper_model(cls, v: str | None) -> str | None:
        """Validate that whisper_model is a known local model name."""
        if v is None:
            return v
        v = v.strip()
        if v and v not in VALID_LOCAL_WHISPER_MODELS:
            raise ValueError(
                f"Unknown Whisper model '{v}'. Valid models: {sorted(VALID_LOCAL_WHISPER_MODELS)}"
            )
        return v or None

    @model_validator(mode="after")
    def validate_min_max_speakers(self) -> "ReprocessRequest":
        """Validate that min_speakers <= max_speakers if both are provided."""
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError(
                f"min_speakers ({self.min_speakers}) must be less than or equal to "
                f"max_speakers ({self.max_speakers})"
            )
        return self


class PrepareUploadRequest(BaseModel):
    """Request schema for preparing a file upload.

    This schema is used to create a file record before the actual upload starts.

    Attributes:
        filename: Name of the file to be uploaded
        file_size: Size of the file in bytes
        content_type: MIME type of the file
        file_hash: Content fingerprint of the source, for duplicate detection
        extracted_from_video: Optional metadata from original video file (if audio was extracted client-side)
        min_speakers: Optional minimum number of speakers for diarization
        max_speakers: Optional maximum number of speakers for diarization
        num_speakers: Optional fixed number of speakers for diarization (overrides min/max)
        collection_ids: Optional collection UUIDs to add the file to after creation
        tag_names: Optional tag names to apply to the file after creation
    """

    filename: str = Field(..., description="Name of the file to be uploaded")
    file_size: int = Field(..., description="Size of the file in bytes")
    content_type: str = Field(..., description="MIME type of the file")
    file_hash: str | None = Field(
        None,
        description=(
            "Client-computed content fingerprint used for pre-upload duplicate "
            "detection. The browser sends an imohash (32-char hex) — the same "
            "constant-time fingerprint the server computes into MediaFile.imohash, "
            "so a 15 GB file costs the same to fingerprint as a 1 MB one. For "
            "client-extracted audio this is the fingerprint of the SOURCE VIDEO. "
            "A legacy SHA-256 from an older client still matches historical rows."
        ),
    )
    extracted_from_video: dict[str, Any] | None = Field(
        None, description="Metadata from original video file if audio was extracted client-side"
    )
    min_speakers: int | None = Field(
        None, description="Minimum number of speakers for diarization (positive integer)"
    )
    max_speakers: int | None = Field(
        None, description="Maximum number of speakers for diarization (positive integer)"
    )
    num_speakers: int | None = Field(
        None, description="Fixed number of speakers for diarization (overrides min/max when set)"
    )
    disable_diarization: bool | None = Field(
        None,
        description=(
            "Skip speaker diarization entirely; all segments assigned to a single speaker"
        ),
    )
    collection_ids: list[UUID] | None = Field(
        None, description="Collection UUIDs to add the file to after creation"
    )
    tag_names: list[str] | None = Field(
        None, description="Tag names to apply to the file after creation"
    )
    upload_batch_id: UUID | None = Field(
        None,
        description="Client-generated UUID to group files uploaded together into a batch. "
        "All files sharing the same upload_batch_id will be linked to the same UploadBatch record.",
    )
    whisper_model: str | None = Field(
        None,
        description="Whisper model to use for this file. "
        "None = use admin-configured default. "
        "Only applies to local ASR provider.",
        examples=["tiny", "medium", "large-v2", "large-v3", "large-v3-turbo"],
    )
    use_presigned: bool | None = Field(
        False,
        description=(
            "When true, the prepare response includes a presigned PUT URL so "
            "the browser can upload bytes directly to MinIO, bypassing the "
            "API container. Requires a follow-up call to /files/complete. "
            "Defaults to the legacy multipart-form upload flow."
        ),
    )

    @field_validator("min_speakers", "max_speakers", "num_speakers")
    @classmethod
    def validate_speaker_count_positive(cls, v: int | None) -> int | None:
        """Validate that speaker counts are positive integers (>= 1) if provided."""
        if v is not None and v < 1:
            raise ValueError("Speaker count must be at least 1")
        return v

    @field_validator("whisper_model")
    @classmethod
    def validate_whisper_model(cls, v: str | None) -> str | None:
        """Validate that whisper_model is a known local model name."""
        if v is None:
            return v
        v = v.strip()
        if v and v not in VALID_LOCAL_WHISPER_MODELS:
            raise ValueError(
                f"Unknown Whisper model '{v}'. Valid models: {sorted(VALID_LOCAL_WHISPER_MODELS)}"
            )
        return v or None

    @model_validator(mode="after")
    def validate_min_max_speakers(self) -> "PrepareUploadRequest":
        """Validate that min_speakers <= max_speakers if both are provided."""
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError(
                f"min_speakers ({self.min_speakers}) must be less than or equal to "
                f"max_speakers ({self.max_speakers})"
            )
        return self


class SpeakerBase(BaseModel):
    name: str
    display_name: str | None = None
    suggested_name: str | None = None
    verified: bool = False


class SpeakerCreate(SpeakerBase):
    embedding_vector: list[float] | None = None


class SpeakerUpdate(BaseModel):
    # Server-side enforcement of the speaker-label length cap (the frontend also
    # validates display_name <= 100 chars; the backend is the system of record).
    name: str | None = Field(default=None, max_length=100)
    display_name: str | None = Field(default=None, max_length=100)
    suggested_name: str | None = Field(default=None, max_length=100)
    verified: bool | None = None
    embedding_vector: list[float] | None = None
    profile_action: str | None = None  # 'update_profile' or 'create_new_profile'


class Speaker(SpeakerBase, UUIDBaseSchema):
    """Speaker with UUID as public identifier"""

    user_id: UUID
    media_file_id: UUID
    profile_id: UUID | None = None
    confidence: float | None = None
    created_at: datetime

    # Computed status fields from SpeakerStatusService
    computed_status: str | None = None  # "verified", "suggested", "unverified"
    status_text: str | None = None  # Human-readable status text
    status_color: str | None = None  # CSS color for status display
    resolved_display_name: str | None = None  # Best available display name

    # Linked SpeakerProfile info (saves the frontend a separate profile fetch).
    # None when the speaker is not linked to a profile.
    profile_name: str | None = None  # Name of the linked SpeakerProfile
    profile_status: str | None = None  # "linked" when a profile is attached, else None

    # AI-predicted voice attributes
    predicted_gender: str | None = None
    predicted_age_range: str | None = None
    attribute_confidence: dict[str, float] | None = None
    attributes_predicted_at: datetime | None = None


# Speaker Profile schemas
class SpeakerProfileBase(BaseModel):
    name: str
    description: str | None = None


class SpeakerProfileCreate(SpeakerProfileBase):
    pass


class SpeakerProfileUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class SpeakerProfile(SpeakerProfileBase, UUIDBaseSchema):
    """Speaker profile with UUID as public identifier"""

    user_id: UUID
    created_at: datetime
    updated_at: datetime

    # AI-predicted attributes (consensus from linked speakers)
    predicted_gender: str | None = None
    predicted_age_range: str | None = None


# Speaker Collection schemas
class SpeakerCollectionBase(BaseModel):
    name: str
    description: str | None = None
    is_public: bool = False


class SpeakerCollectionCreate(SpeakerCollectionBase):
    pass


class SpeakerCollectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_public: bool | None = None


class SpeakerCollection(SpeakerCollectionBase, UUIDBaseSchema):
    """Speaker collection with UUID as public identifier"""

    user_id: UUID
    created_at: datetime
    updated_at: datetime


class TranscriptSegmentBase(BaseModel):
    start_time: float
    end_time: float
    text: str
    speaker_id: UUID | None = None
    is_overlap: bool = False
    overlap_group_id: UUID | None = None
    overlap_confidence: float | None = None


class TranscriptSegmentCreate(TranscriptSegmentBase):
    pass  # media_file_id will be from URL path


class TranscriptSegmentUpdate(BaseModel):
    id: int | None = None  # Optional since segment is identified by UUID in URL
    start_time: float | None = None
    end_time: float | None = None
    text: str | None = None
    speaker_id: UUID | None = None


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


class MediaFileBase(BaseModel):
    filename: str


class MediaFileCreate(MediaFileBase):
    storage_path: str
    duration: float | None = None
    language: str | None = None
    file_hash: str | None = None
    thumbnail_path: str | None = None


class DerivedCandidate(BaseModel):
    """One source's observation, kept even when it lost.

    Surfaced so a disagreement is something the user can see and settle, rather than a
    decision taken silently by whichever branch of the resolver ran first.
    """

    source: str
    date: datetime | None = None
    confidence: float | None = None
    #: What the source actually said, in its own terms — the matched filename substring,
    #: the spoken phrase, the container field. This is what makes a wrong value
    #: *diagnosable* rather than merely wrong.
    evidence: str | None = None


class DerivedFieldProvenance(BaseModel):
    """Where a derived value came from, how sure we are, and whether a human fixed it.

    **Deliberately not specific to dates.** Participants, topics and titles are the same
    shape — a value the system inferred from one of several sources, which may disagree,
    which the user must be able to see the origin of and override. The next one to ship
    reuses this type and the `<ProvenanceField>` component that renders it rather than
    inventing a second vocabulary; that was the explicit design instruction, and a second
    bespoke edit surface is how the first one stops being maintained.
    """

    #: ``container`` | ``filename`` | ``transcript`` | ``llm`` | ``manual`` | ``none``.
    #: ``none`` means every source was consulted and none answered — which is a different
    #: statement from a null provenance, meaning the resolver has not run at all.
    source: str
    #: Ordinal, not a calibrated probability. Ranks forms within a source; it never
    #: decides *between* sources — precedence does that.
    confidence: float | None = None
    #: A human entered this value. It outranks every derived source permanently and no
    #: re-derivation overwrites it.
    locked: bool = False
    #: Two or more sources named different days. Not an error — a recording made on the
    #: 14th about the 15th's meeting is ordinary — but the user is the right person to
    #: settle it, so it is shown rather than resolved silently.
    conflict: bool = False
    candidates: list[DerivedCandidate] = []


class MediaFileUpdate(BaseModel):
    filename: str | None = None
    title: str | None = None
    status: FileStatus | None = None
    summary_data: dict[str, Any] | None = None
    translated_text: str | None = None
    duration: float | None = None
    language: str | None = None
    file_hash: str | None = None
    thumbnail_path: str | None = None
    #: The user's own correction. Sending a value sets ``source='manual'`` and **locks**
    #: it; sending an explicit ``null`` clears the correction and returns the file to
    #: automatic resolution — a user who set a date by mistake has to be able to take it
    #: back, and a lock at NULL would disable the resolver for that file forever.
    #:
    #: ``crud.update_media_file`` intercepts this field rather than letting its generic
    #: ``setattr`` loop assign it: a bare assignment would write a date with no source and
    #: the database would reject the whole update with an IntegrityError.
    recorded_date: datetime | None = None


class MediaFile(MediaFileBase, UUIDBaseSchema):
    """Media file with UUID as public identifier"""

    user_id: UUID
    storage_path: str
    upload_time: datetime
    file_size: int | None = None
    content_type: str | None = None
    duration: float | None = None
    language: str | None = None
    status: FileStatus
    summary_data: dict[str, Any] | None = None
    translated_text: str | None = None
    download_url: str | None = None
    preview_url: str | None = None
    file_hash: str | None = None
    imohash: str | None = None  # Server-side constant-time content fingerprint (dedup)
    thumbnail_path: str | None = None
    thumbnail_url: str | None = None

    # Technical metadata
    media_format: str | None = None
    codec: str | None = None
    resolution_width: int | None = None
    resolution_height: int | None = None
    frame_rate: float | None = None
    frame_count: int | None = None
    aspect_ratio: str | None = None

    # Audio specs
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    audio_bit_depth: int | None = None

    # Creation and device information
    creation_date: datetime | None = None
    last_modified_date: datetime | None = None
    device_make: str | None = None
    device_model: str | None = None

    # When the recording actually happened — distinct from `upload_time` (when the
    # bytes arrived) and from `creation_date` (what the container claims).
    recorded_date: datetime | None = None
    #: **Always sent alongside `recorded_date`, and never omitted when it is set.** A
    #: derived date whose origin the client cannot show, and which the user cannot
    #: correct, is worse than no date: the UI would render an inference as a fact. The
    #: database refuses a date with no source; this is the wire half of the same rule.
    #:
    #: Derived by the validator below rather than assigned at each response builder.
    #: That is not tidiness — it is the fix for a bug this file shipped for one commit:
    #: there are THREE paths that serialise a MediaFile (the detail builder, the gallery
    #: formatter, and `PUT /files/{uuid}` returning the ORM row straight through
    #: `response_model`), the first two were wired by hand, and the third silently
    #: returned a date with no provenance. Deriving it here makes "wire one and forget
    #: another" unrepresentable instead of merely discouraged.
    recorded_date_provenance: Optional["DerivedFieldProvenance"] = None

    # The four raw columns, carried so the validator below can see them and
    # `exclude=True` so they never reach the wire — the client gets the assembled
    # `recorded_date_provenance` object, not four loose fields it would have to
    # reassemble (and could reassemble differently).
    recorded_date_source: str | None = Field(default=None, exclude=True)
    recorded_date_confidence: float | None = Field(default=None, exclude=True)
    recorded_date_locked: bool | None = Field(default=None, exclude=True)
    recorded_date_candidates: list[dict[str, Any]] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _attach_recorded_date_provenance(self) -> "MediaFile":
        """Assemble the provenance from the four columns, on every serialisation path.

        Done here rather than in each response builder because there are **three**
        paths that serialise a MediaFile and hand-wiring covered two of them; the third
        (`PUT /files/{uuid}`, which returns the ORM row straight through
        `response_model`) shipped a date with no provenance until a test caught it.
        """
        if self.recorded_date_provenance is None:
            from app.services.ingest_artifacts.recorded_date_service import provenance_from_columns

            payload = provenance_from_columns(
                source=self.recorded_date_source,
                confidence=self.recorded_date_confidence,
                locked=self.recorded_date_locked,
                candidates=self.recorded_date_candidates,
            )
            if payload is not None:
                self.recorded_date_provenance = DerivedFieldProvenance.model_validate(payload)
        return self

    # Content information
    title: str | None = None
    author: str | None = None
    description: str | None = None
    source_url: str | None = None

    # Formatted fields for frontend display
    formatted_duration: str | None = None  # e.g., "5:23"
    formatted_upload_date: str | None = None  # e.g., "Oct 15, 2024"
    formatted_file_age: str | None = None  # e.g., "2 hours ago"
    formatted_file_size: str | None = None  # e.g., "2.5 MB"
    display_status: str | None = None  # User-friendly status text
    status_badge_class: str | None = None  # CSS class for status styling
    speaker_summary: dict[str, Any] | None = None  # Speaker count and primary speakers

    # Processing model tracking
    whisper_model: str | None = None
    diarization_model: str | None = None
    embedding_mode: str | None = None

    # ASR provider tracking
    asr_provider: str | None = None  # Provider used (local/deepgram/etc.)
    asr_model: str | None = None  # Model used for transcription
    diarization_provider: str | None = None  # Provider used for diarization
    diarization_disabled: bool = Field(default=False)

    # Error handling fields
    error_category: str | None = None  # Error category for user-friendly handling
    error_suggestions: list[str] | None = None  # User-friendly error suggestions
    is_retryable: bool | None = None  # Whether the error is retryable


class MediaFileDetail(MediaFile):
    transcript_segments: list[TranscriptSegment] = []
    # Pre-grouped view of transcript_segments (overlap groups kept together).
    # Mirrors the frontend grouping so it can render groups without recomputing.
    grouped_segments: list[GroupedTranscriptSegment] = []
    # Full tag objects (uuid + name + source), the same shape `/api/tags` returns.
    # Names alone lost `source`, so the detail page could not badge AI-applied tags
    # without a second lookup. BREAKING wire change (issue #326) — see CHANGELOG.
    tags: list["Tag"] = Field(
        default=[],
        description=(
            "Tags attached to this file, as full tag objects — the same shape "
            "`GET /api/tags` returns. BREAKING (issue #326): this was an array of tag "
            "*name strings*; clients reading `tags` as strings must now read `tag.name`. "
            "Note `GET /api/files` (list) carries no `tags` field at all."
        ),
        json_schema_extra={
            "example": [
                {
                    "uuid": "019ec90a-3f41-7aaa-8000-0000000000a1",
                    "name": "Important",
                    "source": "manual",
                }
            ]
        },
    )
    collections: list["Collection"] = []
    analytics: Optional["Analytics"] = None
    speakers: list[Speaker] = []

    # Lightweight summary indicator (full summary fetched via /summary endpoint)
    has_summary: bool = False
    summary_data: dict[str, Any] | None = None  # Excluded from detail response

    # Additional formatted fields for detail view
    speaker_summary: dict[str, Any] | None = None  # Speaker count and primary speakers

    # Caller's effective permission on this file (null = owner)
    my_permission: str | None = None

    # Content redaction state. When redaction is enabled and detection hasn't finished,
    # transcript segments are withheld and `redaction_pending` is true so the UI shows a
    # "redaction in progress" state instead of un-redacted text.
    redaction_status: str | None = None  # pending | processing | done | failed | None
    redaction_pending: bool = False

    # Transcript pagination metadata
    total_segments: int | None = None  # Total number of transcript segments
    total_speaker_segments: int | None = None  # Total after merging adjacent same-speaker segments
    segment_limit: int | None = None  # Max segments returned (None = all)
    segment_offset: int | None = None  # Offset for pagination


class PaginatedMediaFileResponse(BaseModel):
    """Paginated response for media file listings."""

    items: list[MediaFile]
    total: int  # Total files matching filters
    page: int  # Current page (1-indexed)
    page_size: int  # Items per page
    total_pages: int  # Total pages
    has_more: bool  # Convenience for infinite scroll


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
        explicitly with ``tag_service.tag_ownership(tag, user_id)`` and passes it
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


class CommentBase(BaseModel):
    text: str
    timestamp: float | None = None


class CommentCreate(CommentBase):
    pass  # media_file_id will be from URL path


class CommentCreateStandalone(CommentBase):
    """Comment creation where the file reference travels in the body.

    Used by ``POST /api/comments`` (the non-nested fallback route); the
    frontend sends the file's public UUID as ``media_file_id``.
    """

    media_file_id: UUID


class CommentUpdate(BaseModel):
    text: str | None = None
    timestamp: float | None = None


class CommentUser(BaseModel):
    """Nested user info for comments"""

    uuid: UUID
    email: str | None = None
    full_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class Comment(CommentBase, UUIDBaseSchema):
    """Comment with UUID as public identifier"""

    media_file_id: UUID
    user_id: UUID
    user: CommentUser | None = None
    created_at: datetime


class MediaFileInfo(BaseModel):
    """Schema for simplified media file information that gets included in tasks"""

    uuid: UUID  # Public UUID identifier
    filename: str
    file_size: int | None = None
    content_type: str | None = None
    duration: float | None = None
    language: str | None = None
    format: str | None = None
    media_format: str | None = None
    codec: str | None = None
    upload_time: datetime | None = None


class MediaFilePublicInfo(BaseModel):
    """
    Lightweight file metadata for the /info endpoint.

    Returns core identity and status fields without transcript or summary data.
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: UUID
    filename: str
    title: str | None = None
    user_id: UUID
    storage_path: str
    upload_time: datetime | None = None
    file_size: int | None = None
    content_type: str | None = None
    duration: float | None = None
    language: str | None = None
    status: FileStatus


class TaskBase(BaseModel):
    task_type: str
    status: str
    media_file_id: UUID | None = None


class TaskCreate(TaskBase):
    id: str  # Celery task ID (string, not UUID)
    user_id: UUID


class TaskUpdate(BaseModel):
    status: str | None = None
    progress: float | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


class Task(TaskBase):
    """Task schema - uses Celery task ID (string), not UUID"""

    id: str  # Celery task ID
    user_id: UUID
    progress: float
    created_at: datetime
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    media_file: MediaFileInfo | None = None

    # Computed fields for frontend display
    age_category: str | None = None  # "today", "week", "month", "older"
    formatted_duration: str | None = None  # e.g., "5m", "1h 23m"
    status_display: str | None = None  # Human-readable status

    model_config = {"from_attributes": True}


class PaginatedTaskResponse(BaseModel):
    """Paginated response for task listings."""

    items: list[Task]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_more: bool


# Analytics-related schemas
class SpeakerTimeStats(BaseModel):
    by_speaker: dict[str, float] = {}
    total: float = 0.0


class InterruptionStats(BaseModel):
    by_speaker: dict[str, int] = {}
    total: int = 0


class TurnTakingStats(BaseModel):
    by_speaker: dict[str, int] = {}
    total_turns: int = 0


class QuestionStats(BaseModel):
    by_speaker: dict[str, int] = {}
    total: int = 0


class OverallAnalytics(BaseModel):
    word_count: int = 0
    duration_seconds: float = 0.0
    talk_time: SpeakerTimeStats = SpeakerTimeStats()
    interruptions: InterruptionStats = InterruptionStats()
    turn_taking: TurnTakingStats = TurnTakingStats()
    questions: QuestionStats = QuestionStats()
    speaking_pace: float | None = None  # words per minute
    silence_ratio: float | None = None  # ratio of silence


class AnalyticsBase(BaseModel):
    overall_analytics: OverallAnalytics | None = None


class AnalyticsCreate(AnalyticsBase):
    pass  # media_file_id will be from context


class Analytics(AnalyticsBase, UUIDBaseSchema):
    """Analytics with UUID as public identifier"""

    media_file_id: UUID
    computed_at: datetime | None = None
    version: str | None = None


# Collection schemas
class CollectionBase(BaseModel):
    name: str
    description: str | None = None
    is_public: bool = False


class CollectionCreate(CollectionBase):
    default_prompt_id: UUID | None = None


class CollectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_public: bool | None = None
    default_prompt_id: UUID | None = None


class Collection(CollectionBase, UUIDBaseSchema):
    """Collection with UUID as public identifier"""

    user_id: UUID
    default_prompt_id: UUID | None = None
    default_prompt_name: str | None = None
    source: str | None = None
    created_at: datetime
    updated_at: datetime


class CollectionWithCount(Collection):
    media_count: int = 0
    is_shared: bool = False  # True if shared with (not owned by) the caller
    my_permission: str = "owner"  # caller's effective permission
    shared_by: UserBrief | None = None  # who shared it (for shared collections)
    share_count: int = 0  # Number of shares on this collection


class CollectionResponse(Collection):
    media_files: list[MediaFile] | None = []


class CollectionMemberAdd(BaseModel):
    media_file_ids: list[UUID]  # Changed from int to UUID


class CollectionMemberRemove(BaseModel):
    media_file_ids: list[UUID]  # Changed from int to UUID


# Subtitle-related schemas
class SubtitleFormat(StrEnum):
    SRT = "srt"
    WEBVTT = "webvtt"
    MOV_TEXT = "mov_text"


class VideoFormat(StrEnum):
    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"


class SubtitleRequest(BaseModel):
    """Request schema for generating subtitles."""

    include_speakers: bool = Field(True, description="Include speaker labels in subtitles")
    format: SubtitleFormat = Field(SubtitleFormat.SRT, description="Subtitle format")


class VideoWithSubtitlesRequest(BaseModel):
    """Request schema for video with embedded subtitles."""

    output_format: VideoFormat | None = Field(
        None, description="Output video format (auto-detect if not specified)"
    )
    include_speakers: bool = Field(True, description="Include speaker labels in subtitles")
    force_regenerate: bool = Field(
        False, description="Force regeneration even if cached version exists"
    )


class VideoWithSubtitlesResponse(BaseModel):
    """Response schema for video with embedded subtitles."""

    download_url: str = Field(..., description="URL to download the video with embedded subtitles")
    format: str = Field(..., description="Video format")
    cache_key: str = Field(..., description="Cache key for the processed video")
    expires_at: datetime = Field(..., description="When the download URL expires")
    file_size: int | None = Field(None, description="Size of the processed video file")


class SubtitleValidationResult(BaseModel):
    """Result of subtitle validation."""

    is_valid: bool = Field(..., description="Whether subtitles are valid")
    issues: list[str] = Field(default_factory=list, description="List of validation issues found")
    total_segments: int = Field(..., description="Total number of subtitle segments")
    total_duration: float = Field(..., description="Total duration of subtitles in seconds")
