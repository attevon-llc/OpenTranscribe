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
from sqlalchemy import text

# ``TranscriptSegment`` declares a column literally named ``text``, which shadows
# ``sqlalchemy.text`` inside that class body. The alias is the only way to reach
# the SQL construct from there.
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY
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
    from app.models.collection import CollectionMember
    from app.models.file_facts import FileFacts
    from app.models.speaker import Speaker
    from app.models.tag import FileTag
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
    # Where ``duration`` came from (v431, issue #969) — mirrors ``recorded_date_source``.
    # NULL means "written before #969 was fixed; provenance unknown" — the pre-fix
    # pipeline unconditionally overwrote this column with the transcript's speech
    # extent, so an un-backfilled NULL row's duration is presumptively wrong. See
    # ``app.core.enums.DurationSource``.
    duration_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
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
    # Vestigial (#67): a document id in the retired ``transcript_summaries`` index.
    # Nothing sets it any more — ``summary_data`` above is the only copy of a
    # summary — but existing rows on an upgraded deployment still carry one, so
    # readers tolerate it and the clear paths (regenerate / reprocess / delete)
    # drain it. Kept rather than dropped because a migration that removes it is a
    # destructive change to user rows for no functional gain.
    summary_opensearch_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
    # Which detectors the cached spans actually REFLECT. `redaction_status = done` says
    # the scan finished, not that every detector ran: an unavailable one is reported as
    # skipped and still reaches `done` (see detectors/DetectorUnavailableError). Without
    # this, a read path could mask nothing, report success, and send unexamined PII to a
    # provider. NULL = scanned before v392, coverage unknown — see
    # `services/redaction/coverage.py` for why that is trusted rather than refused.
    redaction_coverage: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
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
    # A browser-playable AAC/M4A copy, for originals no browser decodes (AIFF, WMA, AVI,
    # ...). NULL means the original plays as-is. Written by media.create_playback_rendition,
    # served by /stream-url, deleted and quarantine-tagged together with the original.
    playback_path: Mapped[str | None] = mapped_column(String, nullable=True)

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
    # Rows written before v391 may still hold a laundered value, which is exactly why
    # v391 does not backfill ``recorded_date`` from this column.
    creation_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Original creation date, as the container states it (NULL = the container did not)
    last_modified_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Last modified date

    # --- When the recording actually happened, and how we know (v391, #403 R7) ---
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
    # The rest of the file's per-file request, so a retry or recovery re-dispatches what the
    # user chose rather than falling back to defaults. NULL = "not asked" (saved setting wins).
    requested_min_speakers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_max_speakers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_num_speakers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_disable_diarization: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
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
    # Per-step provider/model/where record (issue #1229), {"v": 1, "steps": {...}}. Written
    # only by services/file_provenance.record_stage (one atomic jsonb_set); NULL = processed
    # before recording began. Deferred: the gallery loads this row whole and never reads it.
    processing_provenance: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, deferred=True
    )

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
    # Which authority holds the file (issue #1253, v442): ``TakedownOrigin`` value,
    # 'platform' or 'organization'. "Platform wins": an organization administrator can
    # release only what an organization placed. Set only by ``takedown_service``; a
    # database trigger keeps it consistent with the two flags above (held with no
    # origin reads as 'platform', nothing held means no origin), so a writer that sets
    # ``is_quarantined`` directly can never leave a file the organization could release.
    takedown_origin: Mapped[str | None] = mapped_column(String(16), nullable=True)

    if TYPE_CHECKING:
        # Non-persisted, TYPE_CHECKING-only: `takedown_service.quarantine_file`/
        # `release_file` stamp these plain (non-`Mapped`) attributes onto the
        # instance they return, to report the presigned-URL-revocation storage-plane
        # outcome to the admin quarantine/release endpoint (issue #907) without a DB
        # column or a second return type. `TYPE_CHECKING` keeps this invisible to
        # SQLAlchemy's declarative scanner at import time — it exists purely so mypy
        # accepts `file.presign_revoked = ...` — and nothing here creates a "true"
        # unmapped ORM attribute name collision.
        presign_revoked: bool
        presign_tag_cleared: bool

    # Declared here, not only in v391, because a constraint the database enforces and
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
            "takedown_origin IS NULL OR takedown_origin IN ('platform', 'organization')",
            name="ck_media_file_takedown_origin_valid",
        ),
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
        # v431 (#969). No provenance-required companion CHECK like
        # ``ck_media_file_recorded_date_provenance``: every pre-existing row already
        # has a duration and a NULL source, and the migration deliberately does not
        # backfill (see v431's docstring) — that pairing would refuse to insert on
        # a live production schema.
        CheckConstraint(
            "duration_source IS NULL OR duration_source IN "
            "('container', 'transcript_extent', 'none')",
            name="ck_media_file_duration_source",
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
    # ``UniqueConstraint``, and why ``app/db/schema_detect/legacy.py``'s v071/v073
    # detection arms probe ``pg_constraint`` for it and correctly find nothing.
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


#: The partial UNIQUE index behind "one active task row per file and step" (``v448``, issue
#: #1185). Named here so code that must tell its violation apart from any other integrity error
#: (``task_utils.is_active_row_conflict``) matches on one definition.
TASK_ACTIVE_INDEX = "uq_task_active_per_file_type"


class Task(Base):
    __tablename__ = "task"

    # A file has at most one pending/in_progress row per task_type (corpus-wide rows with no
    # file are exempt: NULLs are distinct). Mirrors ``v448``; the migration resolves existing
    # duplicates first and the writers (``create_task_record``, the reopen paths) take the
    # conflict deliberately rather than swallowing it.
    __table_args__ = (
        Index(
            TASK_ACTIVE_INDEX,
            "media_file_id",
            "task_type",
            unique=True,
            postgresql_where=text("status IN ('pending', 'in_progress')"),
        ),
    )

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

    # ⚠️ Do NOT add a non-mapped attribute here to carry a computed value out of a
    # helper. A `duration_seconds` transient briefly lived on this class for issue
    # #753's notification chip and was wrong twice over: as a bare `float | None` it
    # raised `MappedAnnotationError` at class-definition time (SQLAlchemy 2.0
    # Annotated Declarative scans every annotated class attribute and rejects anything
    # that is neither `Mapped[...]` nor `ClassVar[...]`), so `import app.models.media`
    # failed and the whole app refused to start; and as a `ClassVar[...]` it satisfied
    # SQLAlchemy but made every write a mypy error, because assigning to a ClassVar
    # through an instance is a type error even though Python permits it.
    #
    # The value is now simply RETURNED — `task_utils.update_task_status_with_duration`
    # hands back `(task, duration_seconds)`. Smuggling a computed value out on an ORM
    # object was fighting both tools because it was the wrong shape.


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


# Issue #1162: clear a file's infrastructure-requeue count on every terminal status write.
# Registered here so every process that can write a MediaFile status has it.
from app.core.infra_requeue_reset import register as _register_infra_requeue_reset  # noqa: E402

_register_infra_requeue_reset()
