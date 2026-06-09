"""Reusable SQLAlchemy model mixins for common column patterns."""

import uuid as _uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.utils.uuid7 import uuid7


class UUIDMixin:
    """Mixin that adds a native ``uuid`` column defaulting to a UUIDv7.

    Currently unused by any model (each model declares its own ``uuid`` column
    with ``UUID(as_uuid=True)`` + the ``uuid7`` default), but kept correct so it
    can be adopted without surprises: time-ordered UUIDv7 for index locality,
    real ``uuid.UUID`` values available pre-flush.
    """

    uuid: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7
    )


class TimestampMixin:
    """Mixin that adds ``created_at`` and ``updated_at`` columns."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
