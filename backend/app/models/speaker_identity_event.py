"""One change to a speaker's name, profile link or suggestion (v462, issue #1336).

Append-only: the database refuses to rewrite or delete the rows (``speaker_identity_event_guard``,
registered in ``app.db.audit_protection``). See
``alembic/versions/v462_add_speaker_identity_event.py`` for the design.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.speaker import Speaker

#: Every change kind (``ck_speaker_identity_event_kind``; v462 holds a frozen copy).
EVENT_KINDS: tuple[str, ...] = (
    "rename",
    "name_cleared",
    "link",
    "unlink",
    "suggested",
    "suggestion_accepted",
    "suggestion_rejected",
    "suggestion_cleared",
    "confirmed",
    "skipped",
    "merge",
    "purge",
)
#: The session setting under which the guard lets a ``voiceprint`` row be deleted (a voiceprint
#: purge sets it LOCAL); v462 holds a frozen copy.
PURGE_SWITCH = "opentranscribe.voiceprint_purge"


class SpeakerIdentityEvent(Base):
    """A recorded change: who, when, which model, before and after."""

    __tablename__ = "speaker_identity_event"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in EVENT_KINDS) + ")",
            name="ck_speaker_identity_event_kind",
        ),
        CheckConstraint(
            "actor_kind IN ('user', 'system') AND (actor_kind = 'user' OR actor_user_id IS NULL)",
            name="ck_speaker_identity_event_actor",
        ),
        CheckConstraint(
            "(before_value IS NULL OR jsonb_typeof(before_value) = 'object') "
            "AND (after_value IS NULL OR jsonb_typeof(after_value) = 'object') "
            "AND (detail IS NULL OR jsonb_typeof(detail) = 'object')",
            name="ck_speaker_identity_event_json",
        ),
        Index("ix_speaker_identity_event_speaker_created", "speaker_id", "created_at"),
        Index("ix_speaker_identity_event_organization_id", "organization_id"),
        Index(
            "ix_speaker_identity_event_actor_user_id",
            "actor_user_id",
            postgresql_where=text("actor_user_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid_pkg.uuid4
    )
    speaker_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="CASCADE"), nullable=True
    )
    #: One of ``speaker_identity_history.EVENT_KINDS`` (``ck_speaker_identity_event_kind``).
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    origin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    voiceprint: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    before_value: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    after_value: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # No backref: the speaker never loads or cascades its history (the database deletes it).
    speaker: Mapped[Speaker] = relationship("Speaker")
