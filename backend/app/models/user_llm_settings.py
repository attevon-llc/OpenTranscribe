"""
SQLAlchemy models for user LLM provider settings
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class UserLLMSettings(Base):
    """
    Model for storing user-specific LLM provider configurations

    Each user can have multiple LLM provider configurations. The active configuration
    is tracked via the UserSetting table with key 'active_llm_config_id'.
    API keys are stored encrypted for security.
    """

    __tablename__ = "user_llm_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # User-friendly name for the configuration

    # Provider configuration
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # openai, vllm, ollama, claude, custom
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # Encrypted API key
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Custom endpoint URL

    # Optional settings
    max_tokens: Mapped[int] = mapped_column(
        Integer, default=8192, nullable=False
    )  # Model's context window in tokens (user-configured)
    temperature: Mapped[str] = mapped_column(
        String(10), default="0.3", nullable=False
    )  # Store as string to avoid float precision issues

    # Status tracking
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_tested: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    test_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # success, failed, pending
    test_message: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Error message or success details

    # Sharing
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="llm_settings")

    # Table constraints
    __table_args__ = (
        # Ensure unique configuration names per user
        UniqueConstraint("user_id", "name", name="_user_llm_config_name_unique"),
        Index("ix_user_llm_settings_user_provider", "user_id", "provider"),
    )

    @property
    def has_api_key(self) -> bool:
        """Indicates whether an API key is stored (computed property for schema compatibility)"""
        return bool(self.api_key)

    def __repr__(self):
        return f"<UserLLMSettings(user_id={self.user_id}, name={self.name}, provider={self.provider}, model={self.model_name})>"
