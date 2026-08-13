"""Usage event spine — cloud-edition seam, also useful for self-host analytics.

One row per countable thing that happened (transcription hours, uploads,
summaries, searches, exports, ...). Serves BOTH billing/quota (cloud) and
product analytics. Events never contain transcript content — IDs/counts only.

``idempotency_key`` (unique when present) makes writers safe against Celery
retries and webhook replays.
"""

import uuid as uuid_pkg
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7


class UsageEvent(Base):
    __tablename__ = "usage_event"
    __table_args__ = (
        Index("idx_usage_event_org_type_time", "organization_id", "event_type", "created_at"),
        Index("idx_usage_event_user_time", "user_id", "created_at"),
        # PARTIAL, not total. The column previously carried ``unique=True``, which
        # declares a full unique constraint — a different object from what the
        # database has. Behaviourally equivalent today (a total UNIQUE also admits
        # many NULLs), but it made ``Base.metadata`` describe an index the
        # migrations never build.
        Index(
            "uq_usage_event_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # e.g. "transcription.hours"
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=1)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)  # e.g. "hours", "count"
    file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="SET NULL"), nullable=True
    )
    #: Uniqueness is the PARTIAL index in ``__table_args__``, not a column-level
    #: ``unique=`` — see the note there.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )  # IDs/counts only — never content
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
