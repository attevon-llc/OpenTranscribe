import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.custom_vocabulary import CustomVocabulary
    from app.models.group import UserGroup
    from app.models.group import UserGroupMember
    from app.models.media import Collection
    from app.models.media import Comment
    from app.models.media import MediaFile
    from app.models.media import Speaker
    from app.models.media import SpeakerCollection
    from app.models.media import SpeakerProfile
    from app.models.organization import OrganizationMembership
    from app.models.password_history import PasswordHistory
    from app.models.prompt import SummaryPrompt
    from app.models.prompt import UserSetting
    from app.models.refresh_token import RefreshToken
    from app.models.sharing import CollectionShare
    from app.models.topic import TopicSuggestion
    from app.models.user_asr_settings import UserASRSettings
    from app.models.user_diarization_settings import UserDiarizationSettings
    from app.models.user_llm_settings import UserLLMSettings
    from app.models.user_media_source import UserMediaSource
    from app.models.user_mfa import UserMFA


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Derived mirror of (role == "super_admin"). Kept in sync at every write and
    # enforced by a DB CHECK constraint (migration v369). Never set independently
    # of role — see app.auth.roles.role_implies_superuser. role is the source of
    # truth for authorization.
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role: Mapped[str] = mapped_column(
        String, default="user", nullable=False
    )  # "user", "admin", or "super_admin" (authorization source of truth)
    auth_type: Mapped[str] = mapped_column(
        String, default="local", nullable=False
    )  # "local", "ldap", "keycloak", "pki"
    allow_local_fallback: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )  # When True: user can authenticate via password even if auth_type != 'local'
    ldap_uid: Mapped[str | None] = mapped_column(
        String, nullable=True, unique=True, index=True
    )  # sAMAccountName from AD
    keycloak_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )  # Keycloak subject ID
    external_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )  # External IdP subject id
    external_org_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Last-seen external org — convenience only, never authorization authority
    keycloak_refresh_token: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Encrypted KC refresh token for federated logout
    pki_subject_dn: Mapped[str | None] = mapped_column(
        String(512), unique=True, nullable=True, index=True
    )  # X.509 certificate DN

    # PKI certificate metadata fields
    pki_serial_number: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )  # Certificate serial number
    pki_issuer_dn: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )  # Certificate issuer DN
    pki_organization: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )  # Organization from cert
    pki_organizational_unit: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )  # Organizational unit from cert
    pki_common_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )  # Common name from cert
    pki_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Cert valid from
    pki_not_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Cert valid until
    pki_fingerprint_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )  # SHA256 fingerprint for cert tracking

    # FedRAMP compliance fields
    password_hash_version: Mapped[str | None] = mapped_column(
        String(20), default="bcrypt", nullable=True
    )  # bcrypt, pbkdf2
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # For password expiration
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )  # Force password change
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # For account inactivity
    account_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Account expiration date
    banner_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Login banner ack

    # Proof that THIS deployment sent mail to `email` and someone holding it came
    # back. Gates local login when the `require_email_verification` auth-config
    # key is on (app/auth/email_verification.py) — that key had no reader at all
    # before v375. Not to be confused with ExternalIdentity.email_verified, which
    # records an IdP's assertion about an address (app/auth/external_sync.py).
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def is_admin(self) -> bool:
        """Check if user has admin or super_admin role."""
        return self.role in ("admin", "super_admin")

    @property
    def is_super_admin(self) -> bool:
        """Check if user is a platform super_admin (authorization source of truth)."""
        return self.role == "super_admin"

    # Relationships
    # MediaFile has two FKs to user.id (owner user_id + takedown admin
    # quarantined_by), so the owner relationship must name its join column.
    media_files: Mapped[list["MediaFile"]] = relationship(
        "MediaFile", back_populates="user", foreign_keys="MediaFile.user_id"
    )
    comments: Mapped[list["Comment"]] = relationship("Comment", back_populates="user")
    speakers: Mapped[list["Speaker"]] = relationship("Speaker", back_populates="user")
    speaker_profiles: Mapped[list["SpeakerProfile"]] = relationship(
        "SpeakerProfile", back_populates="user"
    )
    speaker_collections: Mapped[list["SpeakerCollection"]] = relationship(
        "SpeakerCollection", back_populates="user"
    )
    collections: Mapped[list["Collection"]] = relationship("Collection", back_populates="user")
    # SummaryPrompt has two FKs to user (creator + sharer); disambiguate on the creator FK.
    summary_prompts: Mapped[list["SummaryPrompt"]] = relationship(
        "SummaryPrompt", back_populates="user", foreign_keys="SummaryPrompt.user_id"
    )
    settings: Mapped[list["UserSetting"]] = relationship("UserSetting", back_populates="user")
    llm_settings: Mapped[list["UserLLMSettings"]] = relationship(
        "UserLLMSettings", back_populates="user"
    )
    asr_settings: Mapped[list["UserASRSettings"]] = relationship(
        "UserASRSettings", back_populates="user", cascade="all, delete-orphan"
    )
    diarization_settings: Mapped[list["UserDiarizationSettings"]] = relationship(
        "UserDiarizationSettings", back_populates="user", cascade="all, delete-orphan"
    )
    media_sources: Mapped[list["UserMediaSource"]] = relationship(
        "UserMediaSource", back_populates="user", cascade="all, delete-orphan"
    )
    custom_vocabulary: Mapped[list["CustomVocabulary"]] = relationship(
        "CustomVocabulary", back_populates="user", cascade="all, delete-orphan"
    )
    # Topic extraction relationships
    topic_suggestions: Mapped[list["TopicSuggestion"]] = relationship(
        "TopicSuggestion", back_populates="user"
    )
    # Refresh tokens for session management (FedRAMP AC-12)
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    # MFA configuration (FedRAMP IA-2)
    mfa: Mapped["UserMFA | None"] = relationship(
        "UserMFA", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    # Password history for reuse prevention (FedRAMP IA-5)
    password_history: Mapped[list["PasswordHistory"]] = relationship(
        "PasswordHistory",
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="desc(PasswordHistory.created_at)",
    )
    # Organization (tenant) memberships — cloud-edition seam, empty for self-host
    org_memberships: Mapped[list["OrganizationMembership"]] = relationship(
        "OrganizationMembership", back_populates="user", cascade="all, delete-orphan"
    )
    # Groups and sharing relationships
    owned_groups: Mapped[list["UserGroup"]] = relationship(
        "UserGroup", back_populates="owner", cascade="all, delete-orphan"
    )
    group_memberships: Mapped[list["UserGroupMember"]] = relationship(
        "UserGroupMember", back_populates="user", cascade="all, delete-orphan"
    )
    shared_by_me: Mapped[list["CollectionShare"]] = relationship(
        "CollectionShare",
        foreign_keys="CollectionShare.shared_by_id",
        back_populates="shared_by",
        passive_deletes=True,
    )
    shared_with_me: Mapped[list["CollectionShare"]] = relationship(
        "CollectionShare",
        foreign_keys="CollectionShare.target_user_id",
        back_populates="target_user",
        passive_deletes=True,
    )
