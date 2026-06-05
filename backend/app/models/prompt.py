"""
SQLAlchemy models for AI summarization prompt management
"""

import uuid as uuid_pkg

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class SummaryPrompt(Base):
    """
    Model for storing AI summarization prompts

    Supports both system-provided default prompts and user-created custom prompts.
    Allows for multiple prompts per content type with proper management.
    """

    __tablename__ = "summary_prompt"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    prompt_text = Column(Text, nullable=False)
    is_system_default = Column(Boolean, nullable=False, default=False)
    user_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True)
    # Cloud-edition seam: tenant scope (NULL = personal). Written by the cloud layer.
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    content_type = Column(
        String(50), nullable=True, index=True
    )  # 'meeting', 'interview', 'podcast', 'documentary', 'general'

    # Sharing
    is_shared = Column(Boolean, nullable=False, default=False)
    shared_at = Column(DateTime(timezone=True), nullable=True)
    # Admin/owner who flipped sharing on (may differ from creator)
    shared_by = Column(Integer, ForeignKey("user.id"), nullable=True)
    tags = Column(JSONB, nullable=False, default=list)
    usage_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    # Two FKs to user (creator + sharer) require explicit foreign_keys to disambiguate.
    user = relationship("User", back_populates="summary_prompts", foreign_keys=[user_id])
    shared_by_user = relationship("User", foreign_keys=[shared_by])


class UserSetting(Base):
    """
    Model for storing user preferences and settings

    Key-value store for user preferences including active summary prompt selection.
    """

    __tablename__ = "user_setting"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    user_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True, index=True)
    setting_key = Column(String(100), nullable=False, index=True)
    setting_value = Column(Text, nullable=True)  # Can store JSON or simple values
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="settings")

    __table_args__ = (
        # Ensure unique setting keys per user
        {"extend_existing": True}
    )
