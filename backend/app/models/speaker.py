"""SQLAlchemy models for speakers, voiceprint profiles, speaker collections, clusters and matches."""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import backref
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.models.user import User


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

    # A name is unique per user per tenant (v430). COALESCE makes the personal
    # workspace one tenant, so two personal rows of one name still collide.
    __table_args__ = (
        Index(
            "uq_speaker_profile_user_tenant_name",
            "user_id",
            text("COALESCE(organization_id, 0)"),
            "name",
            unique=True,
        ),
    )

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

    # Allows the two plain (non-column) annotations below.
    __allow_unmapped__ = True

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
    )  # "llm_analysis" (a guess; never the label), "voice_match"/"profile_match" (voiceprint), "user_skipped"
    verified: Mapped[bool | None] = mapped_column(
        Boolean, default=False
    )  # Flag to indicate if the speaker has been verified
    confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # Confidence score if auto-matched
    # v449 (#1280): who wrote display_name / profile_id -- "voiceprint", "voiceprint_link",
    # "human", or NULL (before v449, or never named; read as human). Written only through
    # app/utils/speaker_identity_source.py; the privacy policy hides and purges by it.
    identity_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # v449 (#1280 follow-up): the rest of the record of how the CURRENT identity and
    # recommendation came to be. Written by the same helpers as identity_source / the suggestion
    # fields. identity_source_user_id is the person who made or confirmed the identity (NULL = a
    # task, or unknown); it is not a relationship because a second user FK would make
    # Speaker.user ambiguous. The full append-only history is issue #1336.
    identity_source_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    identity_source_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    # none_as_null: a plain JSONB stores Python None as the JSON value ``null``, which is NOT a
    # SQL NULL, so a cleared record would still read ``IS NOT NULL`` to the purge's residual check.
    identity_source_detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    suggestion_detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Read-only provenance for the API schema: attached to a loaded row by
    # services/speaker_provenance_view after the privacy redaction, never a column and never
    # flushed. The class default makes an unattached row read as "unknown".
    identity: dict[str, Any] | None = None
    suggestion: dict[str, Any] | None = None

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
        # v449 (#1280); same text as the migration.
        CheckConstraint(
            "identity_source IS NULL OR identity_source IN "
            "('human', 'voiceprint', 'voiceprint_link')",
            name="ck_speaker_identity_source",
        ),
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="speakers", foreign_keys=[user_id])
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

    # Unique per user per tenant (v430), NULL-safe like the profile index.
    __table_args__ = (
        Index(
            "uq_speaker_collection_user_tenant_name",
            "user_id",
            text("COALESCE(organization_id, 0)"),
            "name",
            unique=True,
        ),
    )

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
    # passive_deletes: speaker_cluster.user_id is NOT NULL + ON DELETE CASCADE, so the
    # database removes the clusters with the account. A bare backref made the ORM NULL the
    # FK first, and every account owning a cluster failed to delete.
    user: Mapped["User"] = relationship(
        "User", backref=backref("speaker_clusters", passive_deletes="all")
    )
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


class SpeakerCorrectionCarryover(Base):
    """A user's correction, kept by the AUDIO it judged while a re-run replaces the speaker.

    Re-diarizing, re-transcribing or retrying a file deletes its speakers, and the
    ``ON DELETE CASCADE`` on the two tables above takes every correction with them. One
    row per correction of a deleted speaker: the speaker's time ``spans`` and what it
    was ruled out from (a profile, or a speaker of another file). The re-run's segment
    save hands it to the new speakers whose speech lies in those spans, then deletes it
    (``services/speaker_correction_carryover``; issue #1233).
    """

    __tablename__ = "speaker_correction_carryover"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    media_file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("speaker_profile.id", ondelete="CASCADE"), nullable=True
    )
    cannot_link_speaker_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=True
    )
    spans: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(profile_id IS NULL) <> (cannot_link_speaker_id IS NULL)",
            name="ck_speaker_correction_carryover_one_target",
        ),
    )
