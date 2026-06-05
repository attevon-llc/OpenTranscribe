import uuid as uuid_pkg

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.enums import FileStatus  # noqa: F401 — re-exported for backward compat
from app.db.base import Base


class MediaFile(Base):
    __tablename__ = "media_file"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    # Cloud-edition seam: tenant scope (NULL = personal). Written by the cloud layer.
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True, index=True)
    filename = Column(String, index=True)
    storage_path = Column(String, nullable=False)  # Path in MinIO/S3
    upload_time = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)  # When processing completed
    duration = Column(Float, nullable=True)  # Duration in seconds
    file_size = Column(Integer, nullable=False)  # Size in bytes
    content_type = Column(String, nullable=False)  # MIME type
    is_public = Column(Boolean, default=False)  # Whether file is publicly accessible
    language = Column(String, nullable=True)  # Detected language code
    status: Column[FileStatus] = Column(
        SAEnum(
            FileStatus,
            native_enum=False,
            create_constraint=False,
            values_callable=lambda e: [s.value for s in e],
        ),
        default=FileStatus.PENDING,
        index=True,
    )
    summary_data = Column(JSONB, nullable=True)  # Complete structured AI summary (flexible format)
    summary_opensearch_id = Column(String, nullable=True)  # OpenSearch document ID for summary
    summary_status = Column(
        String, default="pending", nullable=True
    )  # pending, processing, completed, failed, not_configured, disabled
    summary_schema_version = Column(Integer, default=1)  # Track summary schema evolution
    translated_text = Column(Text, nullable=True)  # For non-English transcripts

    # Content redaction lifecycle (detection runs once per transcript, cached on segments)
    redaction_status = Column(
        String, nullable=True
    )  # pending | processing | done | failed (None = not yet run)
    redaction_model_version = Column(
        String, nullable=True
    )  # Detector model version that produced the cached spans (for upgrade re-index)
    file_hash = Column(String, nullable=True, index=True)  # SHA-256 hash for duplicate detection
    # Constant-time content fingerprint (first/middle/last byte samples + size).
    # Complements file_hash for server-side dedup + artifact cache keys. Not
    # collision-resistant; do NOT use for security-sensitive equality checks.
    imohash = Column(String(64), nullable=True, index=True)
    thumbnail_path = Column(String, nullable=True)  # Path to video thumbnail in storage

    # Detailed metadata fields
    metadata_raw = Column(JSONB, nullable=True)  # Complete raw metadata from extraction
    metadata_important = Column(JSONB, nullable=True)  # Important metadata for display

    # Waveform visualization data
    waveform_data = Column(JSONB, nullable=True)  # Cached waveform data for visualization

    # Media technical specs
    media_format = Column(String, nullable=True)  # Container format (MP4, MOV, etc.)
    codec = Column(String, nullable=True)  # Codec used (H.264, AAC, etc.)
    frame_rate = Column(Float, nullable=True)  # Frames per second for video
    frame_count = Column(Integer, nullable=True)  # Total frames for video
    resolution_width = Column(Integer, nullable=True)  # Video width in pixels
    resolution_height = Column(Integer, nullable=True)  # Video height in pixels
    aspect_ratio = Column(String, nullable=True)  # Aspect ratio (16:9, etc.)

    # Audio specs
    audio_channels = Column(Integer, nullable=True)  # Number of audio channels
    audio_sample_rate = Column(Integer, nullable=True)  # Audio sample rate (Hz)
    audio_bit_depth = Column(Integer, nullable=True)  # Audio bit depth

    # Creation information
    creation_date = Column(DateTime(timezone=True), nullable=True)  # Original creation date
    last_modified_date = Column(DateTime(timezone=True), nullable=True)  # Last modified date

    # Device information
    device_make = Column(String, nullable=True)  # Device manufacturer
    device_model = Column(String, nullable=True)  # Device model

    # Content information
    title = Column(String, nullable=True)  # Content title from metadata
    author = Column(String, nullable=True)  # Content author/artist
    description = Column(Text, nullable=True)  # Content description
    source_url = Column(String(2048), nullable=True)  # Original source URL (e.g., YouTube URL)

    # Task tracking and error handling fields
    active_task_id = Column(String, nullable=True, index=True)  # Current Celery task ID
    task_started_at = Column(DateTime(timezone=True), nullable=True)  # When current task started
    task_last_update = Column(DateTime(timezone=True), nullable=True)  # Last task progress update
    cancellation_requested = Column(Boolean, default=False)  # User requested cancellation
    retry_count = Column(Integer, default=0)  # Number of retry attempts
    max_retries = Column(Integer, default=3)  # Maximum retry attempts allowed
    last_error_message = Column(Text, nullable=True)  # Last error encountered
    error_category = Column(String(50), nullable=True, index=True)  # Classified error type
    force_delete_eligible = Column(Boolean, default=False)  # Can be force deleted if orphaned
    recovery_attempts = Column(Integer, default=0)  # Number of recovery attempts
    last_recovery_attempt = Column(
        DateTime(timezone=True), nullable=True
    )  # Last recovery attempt time

    # Processing model tracking
    whisper_model = Column(String, nullable=True)  # e.g., "large-v3-turbo", "large-v3"
    requested_whisper_model = Column(String, nullable=True)  # Model user asked for at upload
    diarization_model = Column(String, nullable=True)  # e.g., "pyannote/speaker-diarization-3.1"
    embedding_mode = Column(String, nullable=True)  # "v3" (512d) or "v4" (256d)

    # ASR provider tracking
    asr_provider = Column(String, nullable=True)  # Provider used (local/deepgram/etc.)
    asr_model = Column(String, nullable=True)  # Model used for transcription
    diarization_provider = Column(String, nullable=True)  # Provider used for diarization
    diarization_disabled = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )  # True when user explicitly skipped diarization

    # Upload batch tracking
    upload_batch_id = Column(
        Integer, ForeignKey("upload_batch.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Relationships
    user = relationship("User", back_populates="media_files")
    upload_batch = relationship("UploadBatch", back_populates="media_files")
    transcript_segments = relationship(
        "TranscriptSegment", back_populates="media_file", cascade="all, delete-orphan"
    )
    speakers = relationship("Speaker", back_populates="media_file", cascade="all, delete-orphan")
    comments = relationship("Comment", back_populates="media_file", cascade="all, delete-orphan")
    file_tags = relationship("FileTag", back_populates="media_file", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="media_file", cascade="all, delete-orphan")
    analytics = relationship(
        "Analytics",
        back_populates="media_file",
        uselist=False,
        cascade="all, delete-orphan",
    )
    collection_memberships = relationship(
        "CollectionMember", back_populates="media_file", cascade="all, delete-orphan"
    )
    # Topic extraction and suggestions
    topic_suggestions = relationship(
        "TopicSuggestion", back_populates="media_file", cascade="all, delete-orphan", uselist=False
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segment"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    media_file_id = Column(Integer, ForeignKey("media_file.id"), nullable=False)
    speaker_id = Column(Integer, ForeignKey("speaker.id"), nullable=True)
    start_time = Column(Float, nullable=False)  # Start time in seconds
    end_time = Column(Float, nullable=False)  # End time in seconds
    text = Column(Text, nullable=False)
    is_overlap = Column(
        Boolean, nullable=False, default=False
    )  # From overlapping speech separation
    overlap_group_id = Column(
        UUID(as_uuid=True), nullable=True, index=True
    )  # Groups overlapping segments together
    overlap_confidence = Column(Float, nullable=True)  # Confidence of overlap detection
    words = Column(
        JSONB, nullable=True
    )  # Word-level timestamps: [{"word": "...", "start": 0.1, "end": 0.25, "score": 0.95}]
    confidence = Column(Float, nullable=True)  # ASR confidence score (0.0–1.0)
    # Content redaction: cached detection spans (original text is never modified).
    # [{"char_start": int, "char_end": int, "word_start": int|None, "word_end": int|None,
    #   "category": "pii|toxicity|profanity|custom", "entity_type": "NAME|EMAIL|...",
    #   "detector": "presidio|gliner|toxicity|wordlist|llm", "confidence": float}]
    redactions = Column(JSONB, nullable=True)
    # Segment-level toxicity scores (no char span): {"toxic": 0.91, "insult": 0.81, ...}
    toxicity = Column(JSONB, nullable=True)

    # Relationships
    media_file = relationship("MediaFile", back_populates="transcript_segments")
    speaker = relationship("Speaker", back_populates="transcript_segments")


class SpeakerProfile(Base):
    """Global speaker profile that can be identified across multiple media files"""

    __tablename__ = "speaker_profile"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True, index=True)
    name = Column(String, nullable=False)  # User-assigned name (e.g., "John Doe")
    description = Column(Text, nullable=True)  # Optional description or notes

    # Note: embedding_vector stored in OpenSearch for optimal vector similarity performance
    embedding_count = Column(
        Integer, default=0
    )  # Number of speakers contributing to this embedding
    last_embedding_update = Column(DateTime(timezone=True), nullable=True)

    # Avatar image path in MinIO
    avatar_path = Column(String(512), nullable=True)

    # AI-predicted attributes (consensus from linked speakers)
    predicted_gender = Column(String(20), nullable=True)  # "male", "female", "unknown"
    predicted_age_range = Column(
        String(30), nullable=True
    )  # "child", "teen", "young_adult", "adult", "senior"

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Cluster origin
    source_cluster_id = Column(
        Integer, ForeignKey("speaker_cluster.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_speaker_profile_user_name"),)

    # Relationships
    user = relationship("User", back_populates="speaker_profiles")
    speaker_instances = relationship(
        "Speaker", back_populates="profile", cascade="save-update, merge"
    )
    speaker_collections = relationship(
        "SpeakerCollectionMember",
        back_populates="speaker_profile",
        cascade="all, delete-orphan",
    )
    source_cluster = relationship("SpeakerCluster", foreign_keys=[source_cluster_id])


class Speaker(Base):
    """Speaker instance within a specific media file"""

    __tablename__ = "speaker"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True, index=True)
    media_file_id = Column(Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False)
    profile_id = Column(
        Integer, ForeignKey("speaker_profile.id", ondelete="SET NULL"), nullable=True
    )
    name = Column(String, nullable=False)  # Original name from diarization (e.g., "SPEAKER_01")
    display_name = Column(String, nullable=True)  # User-assigned display name
    suggested_name = Column(String, nullable=True)  # AI-suggested name from LLM or embedding match
    suggestion_source = Column(
        String, nullable=True
    )  # Source of suggestion: "llm_analysis", "voice_match", "profile_match"
    verified = Column(Boolean, default=False)  # Flag to indicate if the speaker has been verified
    confidence = Column(Float, nullable=True)  # Confidence score if auto-matched
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Computed status fields (calculated by SpeakerStatusService)
    computed_status = Column(String, nullable=True)  # "verified", "suggested", "unverified"
    status_text = Column(String, nullable=True)  # Human-readable status text
    status_color = Column(String, nullable=True)  # CSS color for status display
    resolved_display_name = Column(String, nullable=True)  # Best available display name

    # AI-predicted voice attributes
    predicted_gender = Column(String(20), nullable=True)  # "male", "female", "unknown"
    predicted_age_range = Column(
        String(30), nullable=True
    )  # "child", "teen", "young_adult", "adult", "senior"
    attribute_confidence = Column(JSONB, nullable=True)  # {"gender": 0.92, "age_range": 0.75}
    attributes_predicted_at = Column(DateTime(timezone=True), nullable=True)
    gender_confirmed_by_user = Column(Boolean, default=False)

    # Cluster assignment
    cluster_id = Column(
        Integer, ForeignKey("speaker_cluster.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    user = relationship("User", back_populates="speakers")
    media_file = relationship("MediaFile", back_populates="speakers")
    profile = relationship("SpeakerProfile", back_populates="speaker_instances")
    transcript_segments = relationship("TranscriptSegment", back_populates="speaker")
    cluster = relationship("SpeakerCluster", back_populates="speakers")


class Comment(Base):
    __tablename__ = "comment"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    media_file_id = Column(Integer, ForeignKey("media_file.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    text = Column(Text, nullable=False)
    timestamp = Column(Float, nullable=True)  # Timestamp in seconds, null for general comments
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    media_file = relationship("MediaFile", back_populates="comments")
    user = relationship("User", back_populates="comments")


class Tag(Base):
    __tablename__ = "tag"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    name = Column(String, unique=True, nullable=False)
    source = Column(String(50), nullable=True)  # "manual" | "auto_ai" | "ai_accepted"
    normalized_name = Column(String, nullable=True, index=True)


class FileTag(Base):
    __tablename__ = "file_tag"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    media_file_id = Column(Integer, ForeignKey("media_file.id"))
    tag_id = Column(Integer, ForeignKey("tag.id"))
    source = Column(String(50), nullable=True)  # "manual" | "auto_ai" | "ai_accepted"
    ai_confidence = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    media_file = relationship("MediaFile", back_populates="file_tags")
    tag = relationship("Tag")


class Task(Base):
    __tablename__ = "task"

    id = Column(String, primary_key=True)  # Celery task ID
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    media_file_id = Column(Integer, ForeignKey("media_file.id"), nullable=True)
    task_type = Column(String, nullable=False)  # E.g., "transcription", "summarization"
    status = Column(String, nullable=False)  # "pending", "in_progress", "completed", "failed"
    progress = Column(Float, default=0.0)  # Progress as percentage
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)

    # Relationships
    user = relationship("User")
    media_file = relationship("MediaFile", back_populates="tasks")


class Analytics(Base):
    __tablename__ = "analytics"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    media_file_id = Column(Integer, ForeignKey("media_file.id"), unique=True)

    # Overall analytics structure matching frontend expectations
    overall_analytics = Column(JSONB, nullable=True)  # Complete analytics structure

    # Computation metadata
    computed_at = Column(DateTime(timezone=True), nullable=True)
    version = Column(String, nullable=True)  # Analytics computation version

    # Relationships
    media_file = relationship("MediaFile", back_populates="analytics")


class Collection(Base):
    __tablename__ = "collection"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True, index=True)
    is_public = Column(Boolean, default=False)
    default_summary_prompt_id = Column(
        Integer,
        ForeignKey("summary_prompt.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source = Column(String(50), nullable=True)  # "manual" | "auto_ai" | "bulk_group"
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Unique constraint
    __table_args__ = (UniqueConstraint("user_id", "name", name="_user_collection_uc"),)

    # Relationships
    user = relationship("User", back_populates="collections")
    collection_members = relationship(
        "CollectionMember", back_populates="collection", cascade="all, delete-orphan"
    )
    default_summary_prompt = relationship("SummaryPrompt", foreign_keys=[default_summary_prompt_id])
    # Sharing relationships
    shares = relationship(
        "CollectionShare", back_populates="collection", cascade="all, delete-orphan"
    )


class CollectionMember(Base):
    __tablename__ = "collection_member"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    collection_id = Column(Integer, ForeignKey("collection.id", ondelete="CASCADE"), nullable=False)
    media_file_id = Column(Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=True)  # "manual" | "auto_ai" | "bulk_group"
    ai_confidence = Column(Float, nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    # Unique constraint
    __table_args__ = (
        UniqueConstraint("collection_id", "media_file_id", name="_collection_member_uc"),
    )

    # Relationships
    collection = relationship("Collection", back_populates="collection_members")
    media_file = relationship("MediaFile", back_populates="collection_memberships")


class SpeakerCollection(Base):
    """Collection of speaker profiles for organization"""

    __tablename__ = "speaker_collection"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True, index=True)
    is_public = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Unique constraint
    __table_args__ = (UniqueConstraint("user_id", "name", name="_user_speaker_collection_uc"),)

    # Relationships
    user = relationship("User", back_populates="speaker_collections")
    collection_members = relationship(
        "SpeakerCollectionMember",
        back_populates="collection",
        cascade="all, delete-orphan",
    )


class SpeakerCollectionMember(Base):
    """Members of a speaker collection"""

    __tablename__ = "speaker_collection_member"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    collection_id = Column(
        Integer, ForeignKey("speaker_collection.id", ondelete="CASCADE"), nullable=False
    )
    speaker_profile_id = Column(
        Integer, ForeignKey("speaker_profile.id", ondelete="CASCADE"), nullable=False
    )
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    # Unique constraint
    __table_args__ = (
        UniqueConstraint(
            "collection_id", "speaker_profile_id", name="_speaker_collection_member_uc"
        ),
    )

    # Relationships
    collection = relationship("SpeakerCollection", back_populates="collection_members")
    speaker_profile = relationship("SpeakerProfile", back_populates="speaker_collections")


class SpeakerCluster(Base):
    """Auto-discovered cluster of likely-same speakers across files."""

    __tablename__ = "speaker_cluster"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    user_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    member_count = Column(Integer, default=0)
    promoted_to_profile_id = Column(
        Integer, ForeignKey("speaker_profile.id", ondelete="SET NULL"), nullable=True
    )
    representative_speaker_id = Column(Integer, nullable=True)
    quality_score = Column(Float, nullable=True)
    min_similarity = Column(Float, nullable=True)
    separation_score = Column(Float, nullable=True)
    suggested_name = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", backref="speaker_clusters")
    promoted_to_profile = relationship("SpeakerProfile", foreign_keys=[promoted_to_profile_id])
    members = relationship(
        "SpeakerClusterMember", back_populates="cluster", cascade="all, delete-orphan"
    )
    speakers = relationship("Speaker", back_populates="cluster")


class SpeakerClusterMember(Base):
    """Membership of a speaker in a cluster."""

    __tablename__ = "speaker_cluster_member"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    cluster_id = Column(
        Integer, ForeignKey("speaker_cluster.id", ondelete="CASCADE"), nullable=False
    )
    speaker_id = Column(Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False)
    confidence = Column(Float, default=0.0)
    margin = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    cluster = relationship("SpeakerCluster", back_populates="members")
    speaker = relationship("Speaker")

    __table_args__ = (UniqueConstraint("cluster_id", "speaker_id", name="uq_cluster_speaker"),)


class SpeakerMatch(Base):
    """Cross-references between similar speakers across different media files"""

    __tablename__ = "speaker_match"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    speaker1_id = Column(Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False)
    speaker2_id = Column(Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False)
    confidence = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    speaker1 = relationship("Speaker", foreign_keys=[speaker1_id])
    speaker2 = relationship("Speaker", foreign_keys=[speaker2_id])


class SpeakerCannotLink(Base):
    """Pairwise constraint: these two speakers must not be in the same cluster."""

    __tablename__ = "speaker_cannot_link"

    id = Column(Integer, primary_key=True, index=True)
    speaker_id = Column(Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False)
    cannot_link_speaker_id = Column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    reason = Column(String(255), nullable=True)

    speaker = relationship("Speaker", foreign_keys=[speaker_id])
    cannot_link_speaker = relationship("Speaker", foreign_keys=[cannot_link_speaker_id])

    __table_args__ = (
        UniqueConstraint("speaker_id", "cannot_link_speaker_id", name="uq_speaker_cannot_link"),
    )


class SpeakerProfileBlacklist(Base):
    """Blacklist: this speaker must never join any cluster belonging to this profile."""

    __tablename__ = "speaker_profile_blacklist"

    id = Column(Integer, primary_key=True, index=True)
    speaker_id = Column(Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False)
    profile_id = Column(
        Integer, ForeignKey("speaker_profile.id", ondelete="CASCADE"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    reason = Column(String(255), nullable=True)

    speaker = relationship("Speaker", foreign_keys=[speaker_id])
    profile = relationship("SpeakerProfile", foreign_keys=[profile_id])

    __table_args__ = (
        UniqueConstraint("speaker_id", "profile_id", name="uq_speaker_profile_blacklist"),
    )
