"""Usage event spine — cloud-edition seam, also useful for self-host analytics.

One row per countable thing that happened (transcription hours, uploads,
summaries, searches, exports, ...). Serves BOTH billing/quota (cloud) and
product analytics. Events never contain transcript content — IDs/counts only.

``idempotency_key`` (unique when present) makes writers safe against Celery
retries and webhook replays.
"""

import uuid as uuid_pkg

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.base import Base


class UsageEvent(Base):
    __tablename__ = "usage_event"
    __table_args__ = (
        Index("idx_usage_event_org_type_time", "organization_id", "event_type", "created_at"),
        Index("idx_usage_event_user_time", "user_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid_pkg.uuid4)
    user_id = Column(Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    organization_id = Column(
        Integer, ForeignKey("organization.id", ondelete="SET NULL"), nullable=True
    )
    event_type = Column(String(50), nullable=False)  # e.g. "transcription.hours"
    quantity = Column(Numeric(12, 3), nullable=False, default=1)
    unit = Column(String(16), nullable=True)  # e.g. "hours", "count", "tokens"
    file_id = Column(Integer, ForeignKey("media_file.id", ondelete="SET NULL"), nullable=True)
    idempotency_key = Column(String(128), nullable=True, unique=True)
    event_metadata = Column(JSONB, nullable=True)  # IDs/counts only — never content
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
