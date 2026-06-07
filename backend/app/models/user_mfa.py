"""
User MFA (Multi-Factor Authentication) SQLAlchemy model.

Stores TOTP secrets and backup codes for users who have enabled MFA.
Compliant with FedRAMP IA-2 multi-factor authentication requirements.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
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
    from app.models.user import User


class UserMFA(Base):
    """
    Model for storing user MFA configuration.

    Each user can have at most one MFA record. The TOTP secret is stored
    encrypted and backup codes are stored as hashed values for security.
    """

    __tablename__ = "user_mfa"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )

    # Foreign key to user table (one MFA record per user)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    # TOTP secret (base32 encoded, should be encrypted at rest)
    # Max 64 chars for base32-encoded 160-bit secret with padding
    totp_secret: Mapped[str] = mapped_column(String(64), nullable=False)

    # Whether MFA is fully enabled (true after successful verification)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Hashed backup codes stored as JSON array
    # Each code is SHA-256 hashed before storage
    # Format: ["hash1", "hash2", ...]
    backup_codes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Optional: Track last successful MFA verification
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationship back to user
    user: Mapped["User"] = relationship("User", back_populates="mfa")

    def __repr__(self) -> str:
        return f"<UserMFA(id={self.id}, user_id={self.user_id}, enabled={self.totp_enabled})>"
