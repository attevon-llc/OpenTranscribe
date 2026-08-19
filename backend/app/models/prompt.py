"""
SQLAlchemy models for AI summarization prompt management
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
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.user import User


class SummaryPrompt(Base):
    """
    Model for storing AI summarization prompts

    Supports both system-provided default prompts and user-created custom prompts.
    Allows for multiple prompts per content type with proper management.
    """

    __tablename__ = "summary_prompt"

    # PARTIAL unique index: at most one system default per content type, and no
    # constraint at all on the many non-default rows. The predicate is part of the
    # object's identity — a plain UniqueConstraint("content_type") would forbid a
    # second custom prompt for the same content type, which is the feature.
    # Same shape as ``uq_tag_user_name`` in ``app/models/media.py``.
    __table_args__ = (
        Index(
            "unique_system_default_per_content_type",
            "content_type",
            unique=True,
            postgresql_where=text("is_system_default = true"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_system_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Cloud-edition seam: tenant scope (NULL = personal). Written by the cloud layer.
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    content_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )  # 'meeting', 'interview', 'podcast', 'documentary', 'general', 'qa_panel'

    # Sharing
    is_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Admin/owner who flipped sharing on (may differ from creator)
    shared_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("user.id"), nullable=True)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    # Two FKs to user (creator + sharer) require explicit foreign_keys to disambiguate.
    user: Mapped["User | None"] = relationship(
        "User", back_populates="summary_prompts", foreign_keys=[user_id]
    )
    shared_by_user: Mapped["User | None"] = relationship("User", foreign_keys=[shared_by])


class UserSetting(Base):
    """
    Model for storing user preferences and settings

    Key-value store for user preferences including active summary prompt selection.
    """

    __tablename__ = "user_setting"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    setting_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    setting_value: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Can store JSON or simple values
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="settings")

    # The comment "Ensure unique setting keys per user" used to sit here above
    # nothing but the dict below — the declaration was intended and never written,
    # while the database enforced it all along (v010 baseline).
    __table_args__ = (
        UniqueConstraint("user_id", "setting_key", name="user_setting_user_id_setting_key_key"),
        {"extend_existing": True},
    )
