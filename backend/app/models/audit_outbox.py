"""Governance audit events waiting for (or already given to) the audit sink (v459, issue #1329).

A row is inserted in the same transaction as the change it describes and relayed to the sink by
``app.services.audit_outbox``. See ``alembic/versions/v459_add_audit_outbox.py`` for the design.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
#: The sink refused the document itself; retrying cannot succeed.
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_SENT, STATUS_REJECTED)

LAST_ERROR_MAX_LENGTH = 500


class AuditOutbox(Base):
    """One governance audit event and its delivery state."""

    __tablename__ = "audit_outbox"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in STATUSES)})", name="ck_audit_outbox_status"
        ),
        CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name="ck_audit_outbox_sent_at"
        ),
        Index("uq_audit_outbox_event_uuid", "event_uuid", unique=True),
        Index(
            "ix_audit_outbox_due",
            "next_attempt_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_audit_outbox_sent_at", "sent_at", postgresql_where=text("status = 'sent'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_uuid: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_PENDING, server_default=STATUS_PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(LAST_ERROR_MAX_LENGTH), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
