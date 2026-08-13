"""SQLAlchemy model for custom domain vocabulary terms.

Vocabulary terms boost recognition accuracy for domain-specific words across
all supported ASR providers:
  - Cloud providers: Deepgram keywords, AWS custom vocabulary, Speechmatics
    additional_vocab, AssemblyAI word_boost, Gladia custom_vocabulary
  - Local faster-whisper: hotwords parameter (per-word beam-search boost)

Supported domains: medical, legal, corporate, government, technical, general
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User

SUPPORTED_DOMAINS = ("medical", "legal", "corporate", "government", "technical", "general")


class CustomVocabulary(Base):
    """Domain-specific vocabulary term for ASR boosting."""

    __tablename__ = "custom_vocabulary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Cloud-edition seam: tenant scope (NULL = personal). Written by the cloud layer.
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str] = mapped_column(String(50), nullable=False, default="general")
    category: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # Sub-category within domain
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User | None"] = relationship("User", back_populates="custom_vocabulary")

    __table_args__ = (
        # UNIQUE (COALESCE(user_id, 0), term, domain). The COALESCE is the whole
        # point: a plain UniqueConstraint("user_id", "term", "domain") would be
        # WRONG, because NULL != NULL in SQL and duplicate *system* terms
        # (user_id IS NULL) would slip through. That argument is against the wrong
        # spelling, not against declaring it at all — an Index over a text()
        # expression states the real rule exactly, which is what is written below.
        Index(
            "_custom_vocab_unique",
            text("COALESCE(user_id, 0)"),
            "term",
            "domain",
            unique=True,
        ),
        Index("ix_custom_vocabulary_domain", "domain"),
        # Composite index for the hot query in _run_cloud_asr_pipeline:
        #   WHERE (user_id = :uid OR user_id IS NULL) AND is_active = TRUE
        Index("ix_custom_vocabulary_user_active", "user_id", "is_active"),
    )

    @property
    def is_system(self) -> bool:
        """System-wide terms have no user_id."""
        return self.user_id is None

    def __repr__(self) -> str:
        return (
            f"<CustomVocabulary(term={self.term!r}, domain={self.domain}, user_id={self.user_id})>"
        )
