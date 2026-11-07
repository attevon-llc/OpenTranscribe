"""A privacy purge's unfinished cluster-centroid work (v461, issue #1334 item 2).

A voiceprint purge deletes cluster ROWS before their ``cluster_<uuid>`` OpenSearch documents, so
the ids of those documents exist nowhere else once the relational half commits. The relational half
writes this row in that same transaction; the final pass deletes it once the documents are measured
gone, and ``privacy.purge_reverify`` finishes whatever a dead worker left. See
``alembic/versions/v461_add_privacy_purge_pending.py`` for the design.

No foreign keys: the row must outlive the task row (and a user or organization deleted meanwhile)
until its documents are gone. It holds surrogate ids only, and lives minutes unless a store could not
be purged.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class PrivacyPurgePending(Base):
    """One purge whose cluster-centroid documents are not yet confirmed deleted."""

    __tablename__ = "privacy_purge_pending"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(cluster_uuids) = 'array' AND jsonb_typeof(scope) = 'object'",
            name="ck_privacy_purge_pending_array",
        ),
        Index("ix_privacy_purge_pending_created_at", "created_at"),
    )

    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: ``PurgeScope.to_payload()``: where the re-verify's audit event belongs.
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cluster_uuids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reverify_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_reverify_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
