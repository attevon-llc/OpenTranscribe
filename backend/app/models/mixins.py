"""Reusable SQLAlchemy model mixins for common column patterns."""

import uuid as _uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import String
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column


class UUIDMixin:
    """Mixin that adds a ``uuid`` column with auto-generated UUID4."""

    uuid: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=lambda: str(_uuid.uuid4())
    )


class TimestampMixin:
    """Mixin that adds ``created_at`` and ``updated_at`` columns."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
