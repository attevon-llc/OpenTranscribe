"""SQLAlchemy model for user diarization provider settings."""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
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
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.user import User


class UserDiarizationSettings(Base):
    """User-specific diarization provider configuration. Mirrors UserASRSettings pattern."""

    __tablename__ = "user_diarization_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., "pyannote"
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g., "precision-2"
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # AES-256-GCM encrypted
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_tested: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    test_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    test_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship("User", back_populates="diarization_settings")

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="_user_diarization_config_name_unique"),
    )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        return (
            f"<UserDiarizationSettings(user_id={self.user_id}, "
            f"name={self.name!r}, provider={self.provider})>"
        )
