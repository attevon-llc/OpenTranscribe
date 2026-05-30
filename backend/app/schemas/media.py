from datetime import datetime
from enum import Enum
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


class TaskStatus(str, Enum):
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

    min_speakers: Optional[int] = Field(
        None, description="Minimum number of speakers for diarization (positive integer)"
    )
    max_speakers: Optional[int] = Field(
        None, description="Maximum number of speakers for diarization (positive integer)"
    )
    num_speakers: Optional[int] = Field(
        None, description="Fixed number of speakers for diarization (overrides min/max when set)"
    )
    disable_diarization: Optional[bool] = Field(
        None,
        description="Skip speaker diarization entirely",
    )
    whisper_model: Optional[str] = Field(
        None,
        description="Whisper model to use for reprocessing. "
        "None = use admin-configured default. "
        "Only applies to local ASR provider.",
        examples=["tiny", "medium", "large-v2", "large-v3", "large-v3-turbo"],
    )

    @field_validator("min_speakers", "max_speakers", "num_speakers")
    @classmethod
    def validate_speaker_count_positive(cls, v: Optional[int]) -> Optional[int]:
        """Validate that speaker counts are positive integers (>= 1) if provided."""
        if v is not None and v < 1:
            raise ValueError("Speaker count must be at least 1")
        return v

    @field_validator("whisper_model")
    @classmethod
    def validate_whisper_model(cls, v: Optional[str]) -> Optional[str]:
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
    file_hash: Optional[str] = Field(
        None, description="SHA-256 hash of the file for duplicate detection"
    )
    extracted_from_video: Optional[dict[str, Any]] = Field(
        None, description="Metadata from original video file if audio was extracted client-side"
    )
    min_speakers: Optional[int] = Field(
        None, description="Minimum number of speakers for diarization (positive integer)"
    )
    max_speakers: Optional[int] = Field(
        None, description="Maximum number of speakers for diarization (positive integer)"
    )
    num_speakers: Optional[int] = Field(
        None, description="Fixed number of speakers for diarization (overrides min/max when set)"
    )
    disable_diarization: Optional[bool] = Field(
        None,
        description=(
            "Skip speaker diarization entirely; all segments assigned to a single speaker"
        ),
    )
    collection_ids: Optional[list[UUID]] = Field(
        None, description="Collection UUIDs to add the file to after creation"
    )
    tag_names: Optional[list[str]] = Field(
        None, description="Tag names to apply to the file after creation"
    )
    upload_batch_id: Optional[UUID] = Field(
        None,
        description="Client-generated UUID to group files uploaded together into a batch. "
        "All files sharing the same upload_batch_id will be linked to the same UploadBatch record.",
    )
    whisper_model: Optional[str] = Field(
        None,
        description="Whisper model to use for this file. "
        "None = use admin-configured default. "
        "Only applies to local ASR provider.",
        examples=["tiny", "medium", "large-v2", "large-v3", "large-v3-turbo"],
    )
    use_presigned: Optional[bool] = Field(
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
    def validate_speaker_count_positive(cls, v: Optional[int]) -> Optional[int]:
        """Validate that speaker counts are positive integers (>= 1) if provided."""
        if v is not None and v < 1:
            raise ValueError("Speaker count must be at least 1")
        return v

    @field_validator("whisper_model")
    @classmethod
    def validate_whisper_model(cls, v: Optional[str]) -> Optional[str]:
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
    display_name: Optional[str] = None
    suggested_name: Optional[str] = None
    verified: bool = False


class SpeakerCreate(SpeakerBase):
    embedding_vector: Optional[list[float]] = None


class SpeakerUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    suggested_name: Optional[str] = None
    verified: Optional[bool] = None
    embedding_vector: Optional[list[float]] = None
    profile_action: Optional[str] = None  # 'update_profile' or 'create_new_profile'


class Speaker(SpeakerBase, UUIDBaseSchema):
    """Speaker with UUID as public identifier"""

    user_id: UUID
    media_file_id: UUID
    profile_id: Optional[UUID] = None
    confidence: Optional[float] = None
    created_at: datetime

    # Computed status fields from SpeakerStatusService
    computed_status: Optional[str] = None  # "verified", "suggested", "unverified"
    status_text: Optional[str] = None  # Human-readable status text
    status_color: Optional[str] = None  # CSS color for status display
    resolved_display_name: Optional[str] = None  # Best available display name

    # AI-predicted voice attributes
    predicted_gender: Optional[str] = None
    predicted_age_range: Optional[str] = None
    attribute_confidence: Optional[dict[str, float]] = None
    attributes_predicted_at: Optional[datetime] = None


# Speaker Profile schemas
class SpeakerProfileBase(BaseModel):
    name: str
    description: Optional[str] = None


class SpeakerProfileCreate(SpeakerProfileBase):
    pass


class SpeakerProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class SpeakerProfile(SpeakerProfileBase, UUIDBaseSchema):
    """Speaker profile with UUID as public identifier"""

    user_id: UUID
    created_at: datetime
    updated_at: datetime

    # AI-predicted attributes (consensus from linked speakers)
    predicted_gender: Optional[str] = None
    predicted_age_range: Optional[str] = None


# Speaker Collection schemas
class SpeakerCollectionBase(BaseModel):
    name: str
    description: Optional[str] = None
    is_public: bool = False


class SpeakerCollectionCreate(SpeakerCollectionBase):
    pass


class SpeakerCollectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None


class SpeakerCollection(SpeakerCollectionBase, UUIDBaseSchema):
    """Speaker collection with UUID as public identifier"""

    user_id: UUID
    created_at: datetime
    updated_at: datetime


class TranscriptSegmentBase(BaseModel):
    start_time: float
    end_time: float
    text: str
    speaker_id: Optional[UUID] = None
    is_overlap: bool = False
    overlap_group_id: Optional[UUID] = None
    overlap_confidence: Optional[float] = None


class TranscriptSegmentCreate(TranscriptSegmentBase):
    pass  # media_file_id will be from URL path


class TranscriptSegmentUpdate(BaseModel):
    id: Optional[int] = None  # Optional since segment is identified by UUID in URL
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    text: Optional[str] = None
    speaker_id: Optional[UUID] = None


class TranscriptSegment(TranscriptSegmentBase, UUIDBaseSchema):
    """Transcript segment with UUID as public identifier"""

    media_file_id: UUID
    speaker: Optional[Speaker] = None
    confidence: Optional[float] = None  # ASR confidence score (0.0–1.0)

    # Formatted fields for frontend display
    formatted_timestamp: Optional[str] = None  # e.g., "0:45.2"
    display_timestamp: Optional[str] = None  # e.g., "0:45.2" for transcript UI
    speaker_label: Optional[str] = (
        None  # ALWAYS original speaker ID (e.g., "SPEAKER_01") for color consistency
    )
    resolved_speaker_name: Optional[str] = None  # Display name (user label or original ID)


class MediaFileBase(BaseModel):
    filename: str


class MediaFileCreate(MediaFileBase):
    storage_path: str
    duration: Optional[float] = None
    language: Optional[str] = None
    file_hash: Optional[str] = None
    thumbnail_path: Optional[str] = None


class MediaFileUpdate(BaseModel):
    filename: Optional[str] = None
    title: Optional[str] = None
    status: Optional[FileStatus] = None
    summary_data: Optional[dict[str, Any]] = None
    translated_text: Optional[str] = None
    duration: Optional[float] = None
    language: Optional[str] = None
    file_hash: Optional[str] = None
    thumbnail_path: Optional[str] = None


class MediaFile(MediaFileBase, UUIDBaseSchema):
    """Media file with UUID as public identifier"""

    user_id: UUID
    storage_path: str
    upload_time: datetime
    file_size: Optional[int] = None
    content_type: Optional[str] = None
    duration: Optional[float] = None
    language: Optional[str] = None
    status: FileStatus
    summary_data: Optional[dict[str, Any]] = None
    translated_text: Optional[str] = None
    download_url: Optional[str] = None
    preview_url: Optional[str] = None
    file_hash: Optional[str] = None
    thumbnail_path: Optional[str] = None
    thumbnail_url: Optional[str] = None

    # Technical metadata
    media_format: Optional[str] = None
    codec: Optional[str] = None
    resolution_width: Optional[int] = None
    resolution_height: Optional[int] = None
    frame_rate: Optional[float] = None
    frame_count: Optional[int] = None
    aspect_ratio: Optional[str] = None

    # Audio specs
    audio_channels: Optional[int] = None
    audio_sample_rate: Optional[int] = None
    audio_bit_depth: Optional[int] = None

    # Creation and device information
    creation_date: Optional[datetime] = None
    last_modified_date: Optional[datetime] = None
    device_make: Optional[str] = None
    device_model: Optional[str] = None

    # Content information
    title: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    source_url: Optional[str] = None

    # Formatted fields for frontend display
    formatted_duration: Optional[str] = None  # e.g., "5:23"
    formatted_upload_date: Optional[str] = None  # e.g., "Oct 15, 2024"
    formatted_file_age: Optional[str] = None  # e.g., "2 hours ago"
    formatted_file_size: Optional[str] = None  # e.g., "2.5 MB"
    display_status: Optional[str] = None  # User-friendly status text
    status_badge_class: Optional[str] = None  # CSS class for status styling
    speaker_summary: Optional[dict[str, Any]] = None  # Speaker count and primary speakers

    # Processing model tracking
    whisper_model: Optional[str] = None
    diarization_model: Optional[str] = None
    embedding_mode: Optional[str] = None

    # ASR provider tracking
    asr_provider: Optional[str] = None  # Provider used (local/deepgram/etc.)
    asr_model: Optional[str] = None  # Model used for transcription
    diarization_provider: Optional[str] = None  # Provider used for diarization
    diarization_disabled: bool = Field(default=False)

    # Error handling fields
    error_category: Optional[str] = None  # Error category for user-friendly handling
    error_suggestions: Optional[list[str]] = None  # User-friendly error suggestions
    is_retryable: Optional[bool] = None  # Whether the error is retryable


class MediaFileDetail(MediaFile):
    transcript_segments: list[TranscriptSegment] = []
    tags: list[str] = []
    collections: list["Collection"] = []
    analytics: Optional["Analytics"] = None
    speakers: list[Speaker] = []

    # Lightweight summary indicator (full summary fetched via /summary endpoint)
    has_summary: bool = False
    summary_data: Optional[dict[str, Any]] = None  # Excluded from detail response

    # Additional formatted fields for detail view
    speaker_summary: Optional[dict[str, Any]] = None  # Speaker count and primary speakers

    # Caller's effective permission on this file (null = owner)
    my_permission: Optional[str] = None

    # Transcript pagination metadata
    total_segments: Optional[int] = None  # Total number of transcript segments
    total_speaker_segments: Optional[int] = (
        None  # Total after merging adjacent same-speaker segments
    )
    segment_limit: Optional[int] = None  # Max segments returned (None = all)
    segment_offset: Optional[int] = None  # Offset for pagination


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

    source: Optional[str] = None


class TagWithCount(Tag):
    """Tag with usage count for filtering UI"""

    usage_count: int = 0


class CommentBase(BaseModel):
    text: str
    timestamp: Optional[float] = None


class CommentCreate(CommentBase):
    pass  # media_file_id will be from URL path


class CommentUpdate(BaseModel):
    text: Optional[str] = None
    timestamp: Optional[float] = None


class CommentUser(BaseModel):
    """Nested user info for comments"""

    uuid: UUID
    email: Optional[str] = None
    full_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class Comment(CommentBase, UUIDBaseSchema):
    """Comment with UUID as public identifier"""

    media_file_id: UUID
    user_id: UUID
    user: Optional[CommentUser] = None
    created_at: datetime


class MediaFileInfo(BaseModel):
    """Schema for simplified media file information that gets included in tasks"""

    uuid: UUID  # Public UUID identifier
    filename: str
    file_size: Optional[int] = None
    content_type: Optional[str] = None
    duration: Optional[float] = None
    language: Optional[str] = None
    format: Optional[str] = None
    media_format: Optional[str] = None
    codec: Optional[str] = None
    upload_time: Optional[datetime] = None


class MediaFilePublicInfo(BaseModel):
    """
    Lightweight file metadata for the /info endpoint.

    Returns core identity and status fields without transcript or summary data.
    """

    model_config = ConfigDict(from_attributes=True)

    uuid: UUID
    filename: str
    title: Optional[str] = None
    user_id: UUID
    storage_path: str
    upload_time: Optional[datetime] = None
    file_size: Optional[int] = None
    content_type: Optional[str] = None
    duration: Optional[float] = None
    language: Optional[str] = None
    status: FileStatus


class TaskBase(BaseModel):
    task_type: str
    status: str
    media_file_id: Optional[UUID] = None


class TaskCreate(TaskBase):
    id: str  # Celery task ID (string, not UUID)
    user_id: UUID


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    progress: Optional[float] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None


class Task(TaskBase):
    """Task schema - uses Celery task ID (string), not UUID"""

    id: str  # Celery task ID
    user_id: UUID
    progress: float
    created_at: datetime
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    media_file: Optional[MediaFileInfo] = None

    # Computed fields for frontend display
    age_category: Optional[str] = None  # "today", "week", "month", "older"
    formatted_duration: Optional[str] = None  # e.g., "5m", "1h 23m"
    status_display: Optional[str] = None  # Human-readable status

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
    speaking_pace: Optional[float] = None  # words per minute
    silence_ratio: Optional[float] = None  # ratio of silence


class AnalyticsBase(BaseModel):
    overall_analytics: Optional[OverallAnalytics] = None


class AnalyticsCreate(AnalyticsBase):
    pass  # media_file_id will be from context


class Analytics(AnalyticsBase, UUIDBaseSchema):
    """Analytics with UUID as public identifier"""

    media_file_id: UUID
    computed_at: Optional[datetime] = None
    version: Optional[str] = None


# Collection schemas
class CollectionBase(BaseModel):
    name: str
    description: Optional[str] = None
    is_public: bool = False


class CollectionCreate(CollectionBase):
    default_prompt_id: Optional[UUID] = None


class CollectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None
    default_prompt_id: Optional[UUID] = None


class Collection(CollectionBase, UUIDBaseSchema):
    """Collection with UUID as public identifier"""

    user_id: UUID
    default_prompt_id: Optional[UUID] = None
    default_prompt_name: Optional[str] = None
    source: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CollectionWithCount(Collection):
    media_count: int = 0
    is_shared: bool = False  # True if shared with (not owned by) the caller
    my_permission: str = "owner"  # caller's effective permission
    shared_by: Optional[UserBrief] = None  # who shared it (for shared collections)
    share_count: int = 0  # Number of shares on this collection


class CollectionResponse(Collection):
    media_files: Optional[list[MediaFile]] = []


class CollectionMemberAdd(BaseModel):
    media_file_ids: list[UUID]  # Changed from int to UUID


class CollectionMemberRemove(BaseModel):
    media_file_ids: list[UUID]  # Changed from int to UUID


# Subtitle-related schemas
class SubtitleFormat(str, Enum):
    SRT = "srt"
    WEBVTT = "webvtt"
    MOV_TEXT = "mov_text"


class VideoFormat(str, Enum):
    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"


class SubtitleRequest(BaseModel):
    """Request schema for generating subtitles."""

    include_speakers: bool = Field(True, description="Include speaker labels in subtitles")
    format: SubtitleFormat = Field(SubtitleFormat.SRT, description="Subtitle format")


class VideoWithSubtitlesRequest(BaseModel):
    """Request schema for video with embedded subtitles."""

    output_format: Optional[VideoFormat] = Field(
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
    file_size: Optional[int] = Field(None, description="Size of the processed video file")


class SubtitleValidationResult(BaseModel):
    """Result of subtitle validation."""

    is_valid: bool = Field(..., description="Whether subtitles are valid")
    issues: list[str] = Field(default_factory=list, description="List of validation issues found")
    total_segments: int = Field(..., description="Total number of subtitle segments")
    total_duration: float = Field(..., description="Total duration of subtitles in seconds")
