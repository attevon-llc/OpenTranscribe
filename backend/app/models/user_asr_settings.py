"""SQLAlchemy model for user ASR provider settings."""

import uuid as uuid_pkg

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class UserASRSettings(Base):
    """User-specific ASR provider configuration. Mirrors UserLLMSettings pattern."""

    __tablename__ = "user_asr_settings"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    user_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)

    provider = Column(String(50), nullable=False, index=True)
    model_name = Column(String(100), nullable=False)
    api_key = Column(Text, nullable=True)  # AES-256-GCM encrypted (AWS: the secret access key)
    access_key_id = Column(String(200), nullable=True)  # AES-256-GCM encrypted (AWS access key ID)
    base_url = Column(String(500), nullable=True)
    region = Column(String(50), nullable=True)  # For Azure / AWS

    is_active = Column(Boolean, default=True, nullable=False)
    last_tested = Column(DateTime(timezone=True), nullable=True)
    test_status = Column(String(20), nullable=True)
    test_message = Column(Text, nullable=True)

    # Sharing
    is_shared = Column(Boolean, default=False, nullable=False)
    shared_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="asr_settings")

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="_user_asr_config_name_unique"),
        Index("ix_user_asr_settings_user_prov", "user_id", "provider"),
    )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        return f"<UserASRSettings(user_id={self.user_id}, name={self.name!r}, provider={self.provider})>"
