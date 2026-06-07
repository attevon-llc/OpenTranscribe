"""
SQLAlchemy models for AI-powered tag and collection suggestions

Simplified model for LLM-powered tag and collection suggestions from transcripts (Issue #79).
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.media import MediaFile
    from app.models.user import User


class TopicSuggestion(Base):
    """
    AI-generated tag and collection suggestions for a media file

    Stores LLM-suggested tags and collections for user review and approval.
    Suggestions are stored as JSONB arrays for simplicity.

    Attributes:
        id: Primary key (internal use only)
        uuid: Public identifier for API exposure
        media_file_id: Reference to the media file
        user_id: Reference to the user
        suggested_tags: JSONB array of tag suggestions [{name, confidence, rationale}, ...]
        suggested_collections: JSONB array of collection suggestions [{name, confidence, rationale}, ...]
        status: User interaction status (pending, reviewed, accepted, rejected)
        user_decisions: JSONB tracking user's accepts {accepted_collections: [], accepted_tags: []}
    """

    __tablename__ = "topic_suggestion"

    # Primary keys and identifiers
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )

    # Foreign keys
    media_file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # AI-generated suggestions (JSONB arrays)
    suggested_tags: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    suggested_collections: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)

    # User interaction tracking
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending", index=True)
    user_decisions: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # {accepted_collections: [], accepted_tags: []}

    # Auto-apply tracking
    auto_applied_tags: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    auto_applied_collections: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, default=list
    )
    auto_apply_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="topic_suggestions")
    user: Mapped["User"] = relationship("User", back_populates="topic_suggestions")
