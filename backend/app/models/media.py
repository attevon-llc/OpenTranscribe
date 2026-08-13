import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text

# ``TranscriptSegment`` declares a column literally named ``text``, which shadows
# ``sqlalchemy.text`` inside that class body. The alias is the only way to reach
# the SQL construct from there.
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.enums import FileStatus  # noqa: F401 — re-exported for backward compat
from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.file_facts import FileFacts
    from app.models.prompt import SummaryPrompt
    from app.models.sharing import CollectionShare
    from app.models.topic import TopicSuggestion
    from app.models.upload_batch import UploadBatch
    from app.models.user import User


class MediaFile(Base):
    __tablename__ = "media_file"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    # Cloud-edition seam: tenant scope (NULL = personal). Written by the cloud layer.
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    filename: Mapped[str | None] = mapped_column(String, index=True)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)  # Path in MinIO/S3
    upload_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # When processing completed
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)  # Duration in seconds
    # BigInteger, not Integer: the column is bigint in Postgres and the app
    # advertises 15 GB uploads (MAX_UPLOAD_BYTES), well past Integer's 2.1 GB
    # ceiling. The model said Integer while the database said bigint — reads
    # still worked (Python ints are arbitrary precision), but the model was
    # wrong, DDL generated from it would have been too narrow, and the sibling
    # models (pipeline_timing.file_size_bytes, watch_source) already use
    # BigInteger. Found by scripts/check-schema-drift.py.
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)  # Size in bytes
    content_type: Mapped[str] = mapped_column(String, nullable=False)  # MIME type
    is_public: Mapped[bool | None] = mapped_column(
        Boolean, default=False
    )  # Whether file is publicly accessible
    language: Mapped[str | None] = mapped_column(String, nullable=True)  # Detected language code
    # Legacy column had no explicit nullable= → DDL is nullable. Keep nullable=True for
    # schema-equivalence, but type the attribute as non-Optional: a default=PENDING is
    # always applied so it is never None in practice, and call sites read `.value`
    # unconditionally. (The Mapped[] annotation and the explicit nullable= kwarg are
    # allowed to disagree; the kwarg drives DDL.)
    status: Mapped[FileStatus] = mapped_column(
        SAEnum(
            FileStatus,
            native_enum=False,
            create_constraint=False,
            values_callable=lambda e: [s.value for s in e],
        ),
        nullable=True,
        default=FileStatus.PENDING,
        index=True,
    )
    summary_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # Complete structured AI summary (flexible format)
    summary_opensearch_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # OpenSearch document ID for summary
    summary_status: Mapped[str | None] = mapped_column(
        String, default="pending", nullable=True
    )  # pending, processing, completed, failed, not_configured, disabled
    summary_schema_version: Mapped[int | None] = mapped_column(
        Integer, default=1
    )  # Track summary schema evolution
    translated_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # For non-English transcripts

    # Content redaction lifecycle (detection runs once per transcript, cached on segments)
    redaction_status: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # pending | processing | done | failed (None = not yet run)
    redaction_model_version: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Detector model version that produced the cached spans (for upgrade re-index)
    # Client-declared content fingerprint of the source the user selected — the
    # file itself for a plain upload, the SOURCE VIDEO for client-extracted audio.
    # imohash (32 hex) since issue #342; SHA-256 (64 hex) on rows predating it.
    # Compared by exact equality only, so the two vintages never collide.
    file_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Constant-time content fingerprint (first/middle/last byte samples + size).
    # Complements file_hash for server-side dedup + artifact cache keys. Not
    # collision-resistant; do NOT use for security-sensitive equality checks.
    imohash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    thumbnail_path: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Path to video thumbnail in storage

    # Detailed metadata fields
    metadata_raw: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # Complete raw metadata from extraction
    metadata_important: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # Important metadata for display

    # Waveform visualization data
    waveform_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # Cached waveform data for visualization

    # Media technical specs
    media_format: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Container format (MP4, MOV, etc.)
    codec: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Codec used (H.264, AAC, etc.)
    frame_rate: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # Frames per second for video
    frame_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Total frames for video
    resolution_width: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Video width in pixels
    resolution_height: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Video height in pixels
    aspect_ratio: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Aspect ratio (16:9, etc.)

    # Audio specs
    audio_channels: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Number of audio channels
    audio_sample_rate: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Audio sample rate (Hz)
    audio_bit_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Audio bit depth

    # Creation information
    #
    # ⚠️ ``creation_date`` is the CONTAINER's claim and nothing else. It used to be
    # the end of a silent fallback chain (container metadata → filesystem mtime →
    # ``upload_time``) that recorded no provenance, so a value copied from
    # ``upload_time`` was indistinguishable from one read out of the file. That chain
    # is gone: the fallbacks are now explicit ``recorded_date_source`` values below.
    # Rows written before v390 may still hold a laundered value, which is exactly why
    # v390 does not backfill ``recorded_date`` from this column.
    creation_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Original creation date, as the container states it (NULL = the container did not)
    last_modified_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Last modified date

    # --- When the recording actually happened, and how we know (v390, #403 R7) ---
    #
    # Distinct from ``upload_time`` (when the bytes arrived) and from ``creation_date``
    # (what the container claims). This is the resolved answer to "when did this
    # meeting happen", and it never travels without its source — see
    # ``app.core.enums.RecordedDateSource``.
    # No ``index=True``: that would declare a FULL index under the same name the
    # migration gives a PARTIAL one, which is precisely the ORM/DDL disagreement
    # ``test_orm_ddl_divergence`` exists to catch. The partial index is declared in
    # ``__table_args__`` instead, with its predicate.
    recorded_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_date_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recorded_date_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Every source's observation, winner and losers alike, so a disagreement is
    #: inspectable instead of buried in whichever branch of the resolver ran first.
    recorded_date_candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    #: The user set the date by hand. No re-derivation may overwrite a locked row —
    #: this is what makes "you can correct it" permanent rather than true until the
    #: next reindex.
    recorded_date_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    # Device information
    device_make: Mapped[str | None] = mapped_column(String, nullable=True)  # Device manufacturer
    device_model: Mapped[str | None] = mapped_column(String, nullable=True)  # Device model

    # Content information
    title: Mapped[str | None] = mapped_column(String, nullable=True)  # Content title from metadata
    author: Mapped[str | None] = mapped_column(String, nullable=True)  # Content author/artist
    description: Mapped[str | None] = mapped_column(Text, nullable=True)  # Content description
    source_url: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )  # Original source URL (e.g., YouTube URL)

    # Task tracking and error handling fields
    active_task_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )  # Current Celery task ID
    task_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # When current task started
    task_last_update: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Last task progress update
    cancellation_requested: Mapped[bool | None] = mapped_column(
        Boolean, default=False
    )  # User requested cancellation
    retry_count: Mapped[int | None] = mapped_column(Integer, default=0)  # Number of retry attempts
    max_retries: Mapped[int | None] = mapped_column(
        Integer, default=3
    )  # Maximum retry attempts allowed
    last_error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Last error encountered
    error_category: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )  # Classified error type
    force_delete_eligible: Mapped[bool | None] = mapped_column(
        Boolean, default=False
    )  # Can be force deleted if orphaned
    recovery_attempts: Mapped[int | None] = mapped_column(
        Integer, default=0
    )  # Number of recovery attempts
    last_recovery_attempt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Last recovery attempt time

    # Processing model tracking
    whisper_model: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # e.g., "large-v3-turbo", "large-v3"
    requested_whisper_model: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Model user asked for at upload
    diarization_model: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # e.g., "pyannote/speaker-diarization-3.1"
    embedding_mode: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # "v3" (512d) or "v4" (256d)

    # ASR provider tracking
    asr_provider: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Provider used (local/deepgram/etc.)
    asr_model: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Model used for transcription
    diarization_provider: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Provider used for diarization
    diarization_disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )  # True when user explicitly skipped diarization

    # Upload batch tracking
    upload_batch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("upload_batch.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Abuse / DMCA / safe-harbor takedown (issue: abuse intake). Independent of
    # the processing ``status`` so a COMPLETED file can be taken down and later
    # restored to exactly its prior state. While quarantined the file is excluded
    # from every read surface (gallery list, detail, search, streaming, download,
    # thumbnail) for non-admins; admins still see it for review. The original
    # transcript/media are NEVER deleted by a takedown — masking/hiding is a
    # read-time transform, the row stays for the audit + appeal trail.
    is_quarantined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    quarantine_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Free-text takedown reason (DMCA notice ref, AUP clause, etc.)
    quarantined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # When the takedown was applied
    quarantined_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True
    )  # Admin who applied the takedown
    pre_quarantine_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # Processing status at takedown time, restored verbatim on release (v371)
    # Source-of-truth legal hold. When set, the storage object is protected from
    # deletion (best-effort S3/MinIO object legal-hold mirrors this flag) so the
    # evidence can't be destroyed while a dispute/notice is open.
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Declared here, not only in v390, because a constraint the database enforces and
    # Python never states is invisible until it fires at runtime — see
    # ``tests/unit/test_orm_ddl_divergence.py``, whose allowlist is empty by
    # measurement. The two that carry design rather than hygiene:
    #
    #   * ``ck_media_file_recorded_date_provenance`` makes a date with no recorded
    #     origin a state the database will not hold. The rule is not "remember to set
    #     the source" — it is that forgetting is rejected.
    #   * ``ck_media_file_recorded_date_locked_is_manual`` stops a re-derivation from
    #     relabelling a hand-entered date as machine-derived while leaving it locked.
    __table_args__ = (
        CheckConstraint(
            "recorded_date_source IS NULL OR recorded_date_source IN "
            "('container', 'filename', 'transcript', 'llm', 'manual', 'none')",
            name="ck_media_file_recorded_date_source",
        ),
        CheckConstraint(
            "recorded_date IS NULL OR recorded_date_source IS NOT NULL",
            name="ck_media_file_recorded_date_provenance",
        ),
        CheckConstraint(
            "recorded_date_confidence IS NULL OR "
            "(recorded_date_confidence >= 0 AND recorded_date_confidence <= 1)",
            name="ck_media_file_recorded_date_confidence",
        ),
        CheckConstraint(
            "NOT recorded_date_locked OR recorded_date_source = 'manual'",
            name="ck_media_file_recorded_date_locked_is_manual",
        ),
        Index(
            "ix_media_file_recorded_date",
            "recorded_date",
            postgresql_where=text("recorded_date IS NOT NULL"),
        ),
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User", back_populates="media_files", foreign_keys=[user_id]
    )
    upload_batch: Mapped["UploadBatch | None"] = relationship(
        "UploadBatch", back_populates="media_files"
    )
    transcript_segments: Mapped[list["TranscriptSegment"]] = relationship(
        "TranscriptSegment", back_populates="media_file", cascade="all, delete-orphan"
    )
    speakers: Mapped[list["Speaker"]] = relationship(
        "Speaker", back_populates="media_file", cascade="all, delete-orphan"
    )
    comments: Mapped[list["Comment"]] = relationship(
        "Comment", back_populates="media_file", cascade="all, delete-orphan"
    )
    file_tags: Mapped[list["FileTag"]] = relationship(
        "FileTag", back_populates="media_file", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        "Task", back_populates="media_file", cascade="all, delete-orphan"
    )
    analytics: Mapped["Analytics | None"] = relationship(
        "Analytics",
        back_populates="media_file",
        uselist=False,
        cascade="all, delete-orphan",
    )
    collection_memberships: Mapped[list["CollectionMember"]] = relationship(
        "CollectionMember", back_populates="media_file", cascade="all, delete-orphan"
    )
    # Topic extraction and suggestions
    topic_suggestions: Mapped["TopicSuggestion | None"] = relationship(
        "TopicSuggestion", back_populates="media_file", cascade="all, delete-orphan", uselist=False
    )
    # Deterministic ingest artifacts (#383 Phase 2): stats, extractive digest, keyphrases.
    # `passive_deletes` because the FK is ON DELETE CASCADE — without it SQLAlchemy would
    # SELECT the row (and its digest JSONB) just to DELETE it on every file deletion.
    facts_row: Mapped["FileFacts | None"] = relationship(
        "FileFacts",
        back_populates="media_file",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    media_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_file.id"), nullable=False)
    speaker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("speaker.id"), nullable=True)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)  # Start time in seconds
    end_time: Mapped[float] = mapped_column(Float, nullable=False)  # End time in seconds
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_overlap: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # From overlapping speech separation
    overlap_group_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )  # Groups overlapping segments together
    overlap_confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # Confidence of overlap detection
    words: Mapped[list | None] = mapped_column(
        JSONB, nullable=True
    )  # Word-level timestamps: [{"word": "...", "start": 0.1, "end": 0.25, "score": 0.95}]
    confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # ASR confidence score (0.0–1.0)
    # Content redaction: cached detection spans (original text is never modified).
    # [{"char_start": int, "char_end": int, "word_start": int|None, "word_end": int|None,
    #   "category": "pii|toxicity|profanity|custom", "entity_type": "NAME|EMAIL|...",
    #   "detector": "presidio|gliner|toxicity|wordlist|llm", "confidence": float}]
    redactions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Segment-level toxicity scores (no char span): {"toxic": 0.91, "insult": 0.81, ...}
    toxicity: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # UNIQUE (media_file_id, start_time, end_time, md5(text)) — one utterance per
    # (file, span, content). ``v071`` created it as a real UNIQUE constraint;
    # ``v353`` replaced it with this functional unique index because btree's
    # 2704-byte key limit broke on seven-minute monologues. That history is why it
    # is declarable ONLY as an ``Index`` over a ``text()`` expression, never as a
    # ``UniqueConstraint``, and why ``app/db/migrations.py``'s v071/v073 detection
    # arms probe ``pg_constraint`` for it and correctly find nothing.
    #
    # ASR never collides on it — one segment per detected utterance, timings
    # float-distinct. A turn-per-segment corpus collides immediately (two speakers
    # both saying "Yeah ." over each other get the same span from the reference),
    # and because it surfaces as ``IntegrityError`` the whole transaction aborts.
    __table_args__ = (
        Index(
            "uq_transcript_segment_content",
            "media_file_id",
            "start_time",
            "end_time",
            sa_text("md5(text)"),  # sa_text: this class has a column named `text`
            unique=True,
        ),
    )

    # Relationships
    media_file: Mapped["MediaFile"] = relationship(
        "MediaFile", back_populates="transcript_segments"
    )
    speaker: Mapped["Speaker | None"] = relationship(
        "Speaker", back_populates="transcript_segments"
    )


class SpeakerProfile(Base):
    """Global speaker profile that can be identified across multiple media files"""

    __tablename__ = "speaker_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(
        String, nullable=False
    )  # User-assigned name (e.g., "John Doe")
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Optional description or notes

    # Note: embedding_vector stored in OpenSearch for optimal vector similarity performance
    embedding_count: Mapped[int | None] = mapped_column(
        Integer, default=0
    )  # Number of speakers contributing to this embedding
    last_embedding_update: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Avatar image path in MinIO
    avatar_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # AI-predicted attributes (consensus from linked speakers)
    predicted_gender: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # "male", "female", "unknown"
    predicted_age_range: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )  # "child", "teen", "young_adult", "adult", "senior"

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Cluster origin
    source_cluster_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("speaker_cluster.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_speaker_profile_user_name"),)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="speaker_profiles")
    speaker_instances: Mapped[list["Speaker"]] = relationship(
        "Speaker", back_populates="profile", cascade="save-update, merge"
    )
    speaker_collections: Mapped[list["SpeakerCollectionMember"]] = relationship(
        "SpeakerCollectionMember",
        back_populates="speaker_profile",
        cascade="all, delete-orphan",
    )
    source_cluster: Mapped["SpeakerCluster | None"] = relationship(
        "SpeakerCluster", foreign_keys=[source_cluster_id]
    )


class Speaker(Base):
    """Speaker instance within a specific media file"""

    __tablename__ = "speaker"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    media_file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("speaker_profile.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(
        String, nullable=False
    )  # Original name from diarization (e.g., "SPEAKER_01")
    display_name: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # User-assigned display name
    suggested_name: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # AI-suggested name from LLM or embedding match
    suggestion_source: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Source of suggestion: "llm_analysis", "voice_match", "profile_match"
    verified: Mapped[bool | None] = mapped_column(
        Boolean, default=False
    )  # Flag to indicate if the speaker has been verified
    confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # Confidence score if auto-matched
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Computed status fields (calculated by SpeakerStatusService)
    computed_status: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # "verified", "suggested", "unverified"
    status_text: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Human-readable status text
    status_color: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # CSS color for status display
    resolved_display_name: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Best available display name

    # AI-predicted voice attributes
    predicted_gender: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # "male", "female", "unknown"
    predicted_age_range: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )  # "child", "teen", "young_adult", "adult", "senior"
    attribute_confidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # {"gender": 0.92, "age_range": 0.75}
    attributes_predicted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    gender_confirmed_by_user: Mapped[bool | None] = mapped_column(Boolean, default=False)

    # Cluster assignment
    cluster_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("speaker_cluster.id", ondelete="SET NULL"), nullable=True
    )

    # v010 baseline. A diarization label is unique per file per owner — every
    # writer in the diarization path relies on it and none of them could see it.
    __table_args__ = (
        UniqueConstraint(
            "user_id", "media_file_id", "name", name="speaker_user_id_media_file_id_name_key"
        ),
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="speakers")
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="speakers")
    profile: Mapped["SpeakerProfile | None"] = relationship(
        "SpeakerProfile", back_populates="speaker_instances"
    )
    transcript_segments: Mapped[list["TranscriptSegment"]] = relationship(
        "TranscriptSegment", back_populates="speaker"
    )
    cluster: Mapped["SpeakerCluster | None"] = relationship(
        "SpeakerCluster", back_populates="speakers"
    )


class Comment(Base):
    __tablename__ = "comment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    media_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_file.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # Timestamp in seconds, null for general comments
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="comments")
    user: Mapped["User"] = relationship("User", back_populates="comments")


class Tag(Base):
    """A tag, owned by one user or shared as system vocabulary.

    ``user_id`` is NULL for **system tags** (the seeded ``Important`` /
    ``Meeting`` / ``Interview`` / ``Personal`` set, visible to everyone) and set
    for a user's private tags. Uniqueness is therefore per owner, not global —
    ``name`` alone can match several rows, so never look a tag up by name
    without an owner predicate or a join through ``file_tag`` (migration
    ``v374_add_tag_user_id``).
    """

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True, index=True
    )
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "manual" | "auto_ai" | "ai_accepted"
    normalized_name: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Declared to match the column v230_add_auto_labeling created. The column has
    # existed in every database since then; only the model was missing it, so it
    # showed up as model-vs-schema drift (issue #398). Adopting it is the
    # non-destructive fix — dropping a populated timestamptz to satisfy a
    # comparison would be the wrong direction entirely.
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    # Two partial unique indexes rather than one composite UNIQUE: Postgres
    # treats NULLs as distinct, so UNIQUE(user_id, name) alone would allow
    # duplicate system tags and break the idempotent seeder.
    __table_args__ = (
        Index(
            "uq_tag_user_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_tag_system_name",
            "name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
    )


class FileTag(Base):
    __tablename__ = "file_tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    # Both FKs are ``ON DELETE CASCADE`` in the DDL (v010:193-194) and were the
    # only two of the schema's 103 foreign keys whose ``ondelete`` the ORM did not
    # mirror: deleting a MediaFile or a Tag already removes these rows in the
    # database while the ORM believed it had to do the work itself.
    media_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE")
    )
    tag_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tag.id", ondelete="CASCADE"))
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "manual" | "auto_ai" | "ai_accepted"
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="file_tags")
    tag: Mapped["Tag"] = relationship("Tag")

    # Present in the DDL since ``v010_baseline`` but omitted here until now, so
    # the ORM understated the schema and ``merge_tags`` met the violation at
    # runtime instead of planning for it. One file carries a given tag once.
    #
    # It cannot express the *stronger* invariant — one file never carries two
    # differently-owned rows of the same normalized name — because the name
    # lives on ``tag``, not here. That one is held at the single choke point
    # every attach path goes through (``tag_service.resolve_or_create_tag``
    # consults ``lookup_tag_on_file`` first) and repaired for legacy rows by
    # the collision/merge pass.
    __table_args__ = (UniqueConstraint("media_file_id", "tag_id"),)


class Task(Base):
    __tablename__ = "task"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # Celery task ID
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    media_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("media_file.id"), nullable=True
    )
    task_type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # E.g., "transcription", "summarization"
    status: Mapped[str] = mapped_column(
        String, nullable=False
    )  # "pending", "in_progress", "completed", "failed"
    progress: Mapped[float | None] = mapped_column(Float, default=0.0)  # Progress as percentage
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    user: Mapped["User"] = relationship("User")
    media_file: Mapped["MediaFile | None"] = relationship("MediaFile", back_populates="tasks")


class Analytics(Base):
    __tablename__ = "analytics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    media_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("media_file.id"), unique=True
    )

    # Overall analytics structure matching frontend expectations
    overall_analytics: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # Complete analytics structure

    # Computation metadata
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Analytics computation version

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="analytics")


class Collection(Base):
    __tablename__ = "collection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    is_public: Mapped[bool | None] = mapped_column(Boolean, default=False)
    default_summary_prompt_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("summary_prompt.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "manual" | "auto_ai" | "bulk_group"
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Unique constraint
    __table_args__ = (UniqueConstraint("user_id", "name", name="_user_collection_uc"),)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="collections")
    collection_members: Mapped[list["CollectionMember"]] = relationship(
        "CollectionMember", back_populates="collection", cascade="all, delete-orphan"
    )
    default_summary_prompt: Mapped["SummaryPrompt | None"] = relationship(
        "SummaryPrompt", foreign_keys=[default_summary_prompt_id]
    )
    # Sharing relationships
    shares: Mapped[list["CollectionShare"]] = relationship(
        "CollectionShare", back_populates="collection", cascade="all, delete-orphan"
    )


class CollectionMember(Base):
    __tablename__ = "collection_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    collection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("collection.id", ondelete="CASCADE"), nullable=False
    )
    media_file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "manual" | "auto_ai" | "bulk_group"
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    added_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Unique constraint
    __table_args__ = (
        UniqueConstraint("collection_id", "media_file_id", name="_collection_member_uc"),
    )

    # Relationships
    collection: Mapped["Collection"] = relationship(
        "Collection", back_populates="collection_members"
    )
    media_file: Mapped["MediaFile"] = relationship(
        "MediaFile", back_populates="collection_memberships"
    )


class SpeakerCollection(Base):
    """Collection of speaker profiles for organization"""

    __tablename__ = "speaker_collection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    is_public: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Unique constraint
    __table_args__ = (UniqueConstraint("user_id", "name", name="_user_speaker_collection_uc"),)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="speaker_collections")
    collection_members: Mapped[list["SpeakerCollectionMember"]] = relationship(
        "SpeakerCollectionMember",
        back_populates="collection",
        cascade="all, delete-orphan",
    )


class SpeakerCollectionMember(Base):
    """Members of a speaker collection"""

    __tablename__ = "speaker_collection_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    collection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker_collection.id", ondelete="CASCADE"), nullable=False
    )
    speaker_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker_profile.id", ondelete="CASCADE"), nullable=False
    )
    added_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Unique constraint
    __table_args__ = (
        UniqueConstraint(
            "collection_id", "speaker_profile_id", name="_speaker_collection_member_uc"
        ),
    )

    # Relationships
    collection: Mapped["SpeakerCollection"] = relationship(
        "SpeakerCollection", back_populates="collection_members"
    )
    speaker_profile: Mapped["SpeakerProfile"] = relationship(
        "SpeakerProfile", back_populates="speaker_collections"
    )


class SpeakerCluster(Base):
    """Auto-discovered cluster of likely-same speakers across files."""

    __tablename__ = "speaker_cluster"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    # Cloud-edition seam: tenant scope (NULL = personal). Stamped from the
    # member speakers' file org at cluster creation (see v373).
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    member_count: Mapped[int | None] = mapped_column(Integer, default=0)
    promoted_to_profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("speaker_profile.id", ondelete="SET NULL"), nullable=True
    )
    representative_speaker_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    separation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    suggested_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped["User"] = relationship("User", backref="speaker_clusters")
    promoted_to_profile: Mapped["SpeakerProfile | None"] = relationship(
        "SpeakerProfile", foreign_keys=[promoted_to_profile_id]
    )
    members: Mapped[list["SpeakerClusterMember"]] = relationship(
        "SpeakerClusterMember", back_populates="cluster", cascade="all, delete-orphan"
    )
    speakers: Mapped[list["Speaker"]] = relationship("Speaker", back_populates="cluster")


class SpeakerClusterMember(Base):
    """Membership of a speaker in a cluster."""

    __tablename__ = "speaker_cluster_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    cluster_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker_cluster.id", ondelete="CASCADE"), nullable=False
    )
    speaker_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Float, default=0.0)
    margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    cluster: Mapped["SpeakerCluster"] = relationship("SpeakerCluster", back_populates="members")
    speaker: Mapped["Speaker"] = relationship("Speaker")

    __table_args__ = (UniqueConstraint("cluster_id", "speaker_id", name="uq_cluster_speaker"),)


class SpeakerMatch(Base):
    """Cross-references between similar speakers across different media files"""

    __tablename__ = "speaker_match"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    speaker1_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False
    )
    speaker2_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # v010 baseline. Together these two say a match is stored exactly once, in
    # canonical order. ``speaker_match_check`` is the auto-named CHECK Postgres
    # gave the inline ``CHECK (speaker1_id < speaker2_id)`` — a rule every writer
    # must obey and which was previously discoverable only by triggering it.
    __table_args__ = (
        UniqueConstraint(
            "speaker1_id", "speaker2_id", name="speaker_match_speaker1_id_speaker2_id_key"
        ),
        CheckConstraint("speaker1_id < speaker2_id", name="speaker_match_check"),
    )

    # Relationships
    speaker1: Mapped["Speaker"] = relationship("Speaker", foreign_keys=[speaker1_id])
    speaker2: Mapped["Speaker"] = relationship("Speaker", foreign_keys=[speaker2_id])


class SpeakerCannotLink(Base):
    """Pairwise constraint: these two speakers must not be in the same cluster."""

    __tablename__ = "speaker_cannot_link"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    speaker_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False
    )
    cannot_link_speaker_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    speaker: Mapped["Speaker"] = relationship("Speaker", foreign_keys=[speaker_id])
    cannot_link_speaker: Mapped["Speaker"] = relationship(
        "Speaker", foreign_keys=[cannot_link_speaker_id]
    )

    __table_args__ = (
        UniqueConstraint("speaker_id", "cannot_link_speaker_id", name="uq_speaker_cannot_link"),
    )


class SpeakerProfileBlacklist(Base):
    """Blacklist: this speaker must never join any cluster belonging to this profile."""

    __tablename__ = "speaker_profile_blacklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    speaker_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker_profile.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    speaker: Mapped["Speaker"] = relationship("Speaker", foreign_keys=[speaker_id])
    profile: Mapped["SpeakerProfile"] = relationship("SpeakerProfile", foreign_keys=[profile_id])

    __table_args__ = (
        UniqueConstraint("speaker_id", "profile_id", name="uq_speaker_profile_blacklist"),
    )
