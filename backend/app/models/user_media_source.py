"""SQLAlchemy model for user media source settings."""

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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.user import User


class UserMediaSource(Base):
    """User-specific media source configuration for authenticated downloads."""

    __tablename__ = "user_media_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False, default="mediacms")
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    password: Mapped[str | None] = mapped_column(Text, nullable=True)  # AES-256-GCM encrypted
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Sharing
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship("User", back_populates="media_sources")

    __table_args__ = (
        UniqueConstraint("user_id", "hostname", name="_user_media_source_host_unique"),
        Index(
            "ix_user_media_source_shared",
            "is_shared",
            postgresql_where=text("is_shared = TRUE"),
        ),
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    def __repr__(self) -> str:
        return (
            f"<UserMediaSource(user_id={self.user_id}, hostname={self.hostname!r}, "
            f"provider={self.provider_type})>"
        )
