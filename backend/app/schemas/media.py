from datetime import datetime
from typing import Any
from typing import Optional
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from app.core.enums import FileStatus
from app.schemas.base import UUIDBaseSchema
from app.schemas.collection import Collection
from app.schemas.content_hold import HoldSummary
from app.schemas.processing_provenance import ProcessingStepOut
from app.schemas.speaker import Speaker
from app.schemas.tag import Tag
from app.schemas.transcript import GroupedTranscriptSegment
from app.schemas.transcript import TranscriptSegment


class MediaFileBase(BaseModel):
    filename: str


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

    #: Authoritative takedown flag (issue #570/#576 wire contract). The gallery loads
    #: this schema whole for every page, so this is the one field cheap enough to put
    #: here; the reason/timestamp/hold detail live on `MediaFileDetail` only. A
    #: non-admin can never SEE a quarantined row at all (`takedown_service.is_hidden_for`),
    #: so this can only ever be `False` in a non-admin response — no admin-only
    #: serialization branch is needed or added.
    is_quarantined: bool = False
    # Whether the caller may permanently delete this file: its owner, an org admin
    # of its organization, or a platform admin (issue #1103). Set by the gallery
    # list endpoint; None where a response does not compute it.
    can_delete: bool | None = None

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
    # `UserErrorReason` (app.services.error_categorization_service) — the user-facing
    # vocabulary, NOT `media_file.error_category` (app.utils.error_classification.ErrorCategory,
    # retry policy). The rename off "error_category" is what makes the retry-policy column
    # physically unable to reach the wire under this field's old name (issue #843).
    error_reason: str | None = None  # User-friendly error reason for display
    error_suggestions: list[str] | None = None  # User-friendly error suggestions
    user_message: str | None = None  # User-friendly error description (issue #842)
    is_retryable: bool | None = None  # Whether the error is retryable


class MediaFileDetail(MediaFile):
    #: Who holds the file against deletion and reprocessing (issue #1276); never a reason.
    hold: HoldSummary = Field(default_factory=HoldSummary)

    #: Detail-only takedown fields — kept off the list schema (§B.3): a `Text` reason
    #: is not free on a ~70-column row loaded for every gallery page, and nothing in
    #: the gallery renders it. `is_quarantined` itself is inherited from `MediaFile`.
    quarantine_reason: str | None = None
    quarantined_at: datetime | None = None
    legal_hold: bool = False

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

    # How each step was processed (issue #1229), built by services/file_provenance_view.
    processing_steps: list[ProcessingStepOut] = Field(
        default_factory=list,
        description="One display-ready row per processing step, in processing order",
    )

    # Content redaction state. When redaction is enabled and detection hasn't finished,
    # transcript segments are withheld and `redaction_pending` is true so the UI shows a
    # "redaction in progress" state instead of un-redacted text.
    redaction_status: str | None = None  # pending | processing | done | failed | None
    redaction_pending: bool = False
    # Whether the viewer's effective policy would mask anything (switch on AND a category
    # enabled). The UI offers "Run redaction" only when true; the rescan action refuses
    # (409) otherwise.
    redaction_enabled: bool = False

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


class Analytics(AnalyticsBase, UUIDBaseSchema):
    """Analytics with UUID as public identifier"""

    media_file_id: UUID
    computed_at: datetime | None = None
    version: str | None = None


class CollectionResponse(Collection):
    media_files: list[MediaFile] | None = []


class SubtitleValidationResult(BaseModel):
    """Result of subtitle validation."""

    is_valid: bool = Field(..., description="Whether subtitles are valid")
    issues: list[str] = Field(default_factory=list, description="List of validation issues found")
    total_segments: int = Field(..., description="Total number of subtitle segments")
    total_duration: float = Field(..., description="Total duration of subtitles in seconds")
