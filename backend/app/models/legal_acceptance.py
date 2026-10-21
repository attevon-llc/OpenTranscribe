"""A user's acceptance of one exact version of a legal document (migration v441)."""

import uuid as uuid_pkg
from datetime import datetime

from sqlalchemy import CHAR
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7


class LegalAcceptance(Base):
    """Which user accepted which document text.

    ``sha256`` is the hash of the exact bytes served at acceptance time; a new version or
    a changed text therefore needs a new row. Rows are removed with the user (CASCADE).
    """

    __tablename__ = "legal_acceptance"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "document_key", "sha256", name="uq_legal_acceptance_user_doc_sha"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
