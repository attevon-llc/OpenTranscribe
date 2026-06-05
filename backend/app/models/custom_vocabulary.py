"""SQLAlchemy model for custom domain vocabulary terms.

Vocabulary terms boost recognition accuracy for domain-specific words across
all supported ASR providers:
  - Cloud providers: Deepgram keywords, AWS custom vocabulary, Speechmatics
    additional_vocab, AssemblyAI word_boost, Gladia custom_vocabulary
  - Local faster-whisper: hotwords parameter (per-word beam-search boost)

Supported domains: medical, legal, corporate, government, technical, general
"""

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

SUPPORTED_DOMAINS = ("medical", "legal", "corporate", "government", "technical", "general")


class CustomVocabulary(Base):
    """Domain-specific vocabulary term for ASR boosting."""

    __tablename__ = "custom_vocabulary"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True)
    # Cloud-edition seam: tenant scope (NULL = personal). Written by the cloud layer.
    organization_id = Column(Integer, ForeignKey("organization.id"), nullable=True, index=True)
    term = Column(String(200), nullable=False)
    domain = Column(String(50), nullable=False, default="general")
    category = Column(String(100), nullable=True)  # Sub-category within domain
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="custom_vocabulary")

    __table_args__ = (
        # NOTE: The uniqueness constraint for this table is a functional unique index on
        # COALESCE(user_id, 0), term, domain — defined in the migration DDL as:
        #   CONSTRAINT _custom_vocab_unique UNIQUE (COALESCE(user_id, 0), term, domain)
        # SQLAlchemy's UniqueConstraint cannot express COALESCE-based functional constraints,
        # so the constraint is intentionally omitted from __table_args__ to prevent
        # autogenerate from emitting the wrong plain UNIQUE (user_id, term, domain) which
        # would NOT deduplicate system terms (user_id IS NULL) because NULL != NULL in SQL.
        # The actual constraint is enforced at the database level via the migration.
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
