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
        file_hash: SHA-256 hash of the file for duplicate detection
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
        None, description="SHA-256 hash of the file for duplicate detection"
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
    """A display group of transcript segments, mirroring the frontend's grouping.

    Consecutive segments sharing an ``overlap_group_id`` (with more than one member)
    are collapsed into a single overlap group; every other segment is its own
    single-member group. This replicates the ``groupedTranscriptSegments`` logic in
    ``TranscriptDisplay.svelte`` so the frontend can render groups directly.

    Attributes:
        is_overlap_group: True when this group represents an overlapping-speech cluster.
        overlap_group_id: The shared overlap group id (only set for overlap groups).
        start_time: Minimum start time across the group's segments.
        end_time: Maximum end time across the group's segments.
        start_segment_index: Index of the first segment of this group in the flat
            transcript list (used by the frontend for reading-progress tracking).
        segments: The segments belonging to this group, in order.
    """

    is_overlap_group: bool = False
    overlap_group_id: UUID | None = None
    start_time: float
    end_time: float
    start_segment_index: int
    segments: list[TranscriptSegment] = []


class MediaFileBase(BaseModel):
    filename: str


class MediaFileCreate(MediaFileBase):
    storage_path: str
    duration: float | None = None
    language: str | None = None
    file_hash: str | None = None
    thumbnail_path: str | None = None


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
    tags: list[str] = []
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
    """Tag with UUID as public identifier"""

    source: str | None = None


class TagWithCount(Tag):
    """Tag with usage count for filtering UI"""

    usage_count: int = 0


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
