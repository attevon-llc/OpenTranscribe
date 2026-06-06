"""Upload batch model for tracking multi-file imports."""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.media import MediaFile
    from app.models.user import User


class UploadBatch(Base):
    """Tracks files uploaded together for batch topic grouping."""

    __tablename__ = "upload_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "multi_upload" | "playlist" | "url_batch"
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    grouping_status: Mapped[str | None] = mapped_column(
        String(50), server_default="pending"
    )  # pending | processing | completed | skipped

    # Relationships
    user: Mapped["User"] = relationship("User")
    media_files: Mapped[list["MediaFile"]] = relationship(
        "MediaFile", back_populates="upload_batch"
    )
