"""SQLAlchemy model for user ASR provider settings."""

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


class UserASRSettings(Base):
    """User-specific ASR provider configuration. Mirrors UserLLMSettings pattern."""

    __tablename__ = "user_asr_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # AES-256-GCM encrypted (AWS: the secret access key)
    access_key_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )  # AES-256-GCM encrypted (AWS access key ID)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    region: Mapped[str | None] = mapped_column(String(50), nullable=True)  # For Azure / AWS

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_tested: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    test_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    test_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Sharing
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship("User", back_populates="asr_settings")

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="_user_asr_config_name_unique"),
        Index("ix_user_asr_settings_user_prov", "user_id", "provider"),
    )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        return f"<UserASRSettings(user_id={self.user_id}, name={self.name!r}, provider={self.provider})>"
