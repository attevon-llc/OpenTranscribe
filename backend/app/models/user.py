import uuid as uuid_pkg

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

# Import models at module level to avoid circular imports


class User(Base):
    __tablename__ = "user"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    is_superuser = Column(Boolean, default=False, nullable=False)
    role = Column(String, default="user", nullable=False)  # "user", "admin", or "super_admin"
    auth_type = Column(
        String, default="local", nullable=False
    )  # "local", "ldap", "keycloak", "pki"
    allow_local_fallback = Column(
        Boolean, default=False, nullable=False
    )  # When True: user can authenticate via password even if auth_type != 'local'
    ldap_uid = Column(String, nullable=True, unique=True, index=True)  # sAMAccountName from AD
    keycloak_id = Column(String(255), unique=True, nullable=True, index=True)  # Keycloak subject ID
    keycloak_refresh_token = Column(
        Text, nullable=True
    )  # Encrypted KC refresh token for federated logout
    pki_subject_dn = Column(
        String(512), unique=True, nullable=True, index=True
    )  # X.509 certificate DN

    # PKI certificate metadata fields
    pki_serial_number = Column(String(128), nullable=True)  # Certificate serial number
    pki_issuer_dn = Column(String(512), nullable=True)  # Certificate issuer DN
    pki_organization = Column(String(256), nullable=True)  # Organization from cert
    pki_organizational_unit = Column(String(256), nullable=True)  # Organizational unit from cert
    pki_common_name = Column(String(256), nullable=True)  # Common name from cert
    pki_not_before = Column(DateTime(timezone=True), nullable=True)  # Cert valid from
    pki_not_after = Column(DateTime(timezone=True), nullable=True)  # Cert valid until
    pki_fingerprint_sha256 = Column(
        String(64), nullable=True, index=True
    )  # SHA256 fingerprint for cert tracking

    # FedRAMP compliance fields
    password_hash_version = Column(String(20), default="bcrypt", nullable=True)  # bcrypt, pbkdf2
    password_changed_at = Column(DateTime(timezone=True), nullable=True)  # For password expiration
    must_change_password = Column(Boolean, default=False, nullable=False)  # Force password change
    last_login_at = Column(DateTime(timezone=True), nullable=True)  # For account inactivity
    account_expires_at = Column(DateTime(timezone=True), nullable=True)  # Account expiration date
    banner_acknowledged_at = Column(DateTime(timezone=True), nullable=True)  # Login banner ack

    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    @property
    def is_admin(self) -> bool:
        """Check if user has admin or super_admin role."""
        return self.role in ("admin", "super_admin")

    # Relationships
    media_files = relationship("MediaFile", back_populates="user")
    comments = relationship("Comment", back_populates="user")
    speakers = relationship("Speaker", back_populates="user")
    speaker_profiles = relationship("SpeakerProfile", back_populates="user")
    speaker_collections = relationship("SpeakerCollection", back_populates="user")
    collections = relationship("Collection", back_populates="user")
    # SummaryPrompt has two FKs to user (creator + sharer); disambiguate on the creator FK.
    summary_prompts = relationship(
        "SummaryPrompt", back_populates="user", foreign_keys="SummaryPrompt.user_id"
    )
    settings = relationship("UserSetting", back_populates="user")
    llm_settings = relationship("UserLLMSettings", back_populates="user")
    asr_settings = relationship(
        "UserASRSettings", back_populates="user", cascade="all, delete-orphan"
    )
    diarization_settings = relationship(
        "UserDiarizationSettings", back_populates="user", cascade="all, delete-orphan"
    )
    media_sources = relationship(
        "UserMediaSource", back_populates="user", cascade="all, delete-orphan"
    )
    custom_vocabulary = relationship(
        "CustomVocabulary", back_populates="user", cascade="all, delete-orphan"
    )
    # Topic extraction relationships
    topic_suggestions = relationship("TopicSuggestion", back_populates="user")
    # Refresh tokens for session management (FedRAMP AC-12)
    refresh_tokens = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    # MFA configuration (FedRAMP IA-2)
    mfa = relationship(
        "UserMFA", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    # Password history for reuse prevention (FedRAMP IA-5)
    password_history = relationship(
        "PasswordHistory",
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="desc(PasswordHistory.created_at)",
    )
    # Groups and sharing relationships
    owned_groups = relationship("UserGroup", back_populates="owner", cascade="all, delete-orphan")
    group_memberships = relationship(
        "UserGroupMember", back_populates="user", cascade="all, delete-orphan"
    )
    shared_by_me = relationship(
        "CollectionShare",
        foreign_keys="CollectionShare.shared_by_id",
        back_populates="shared_by",
        passive_deletes=True,
    )
    shared_with_me = relationship(
        "CollectionShare",
        foreign_keys="CollectionShare.target_user_id",
        back_populates="target_user",
        passive_deletes=True,
    )
