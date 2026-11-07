"""One change to a speaker's name, profile link or suggestion (v462, issue #1336).

Append-only: rows are written in the same transaction as the change by
``app/services/speaker_identity_history.py`` (the ORM flush listener, or
``record_bulk_identity_events`` for a bulk ``UPDATE``) and the database refuses to rewrite or
delete them (``speaker_identity_event_guard``, registered in ``app.db.audit_protection``). See
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
from sqlalchemy import event
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import InstanceState
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import Session
from sqlalchemy.orm import UOWTransaction
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.speaker_identity_history import TRACKED_FIELDS
from app.utils.speaker_identity_history import EventDraft
from app.utils.speaker_identity_history import derive_event
from app.utils.speaker_identity_source import HISTORY_NOTES_KEY

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

    # Set by the flush listener so a speaker inserted in the same flush is written first. No
    # backref: the speaker never loads or cascades its history (the database deletes it).
    speaker: Mapped[Speaker] = relationship("Speaker")


def _changes(state: InstanceState[Any]) -> dict[str, tuple[Any, Any]]:
    """Tracked field -> ``(old, new)`` for each tracked field this flush changes."""
    found: dict[str, tuple[Any, Any]] = {}
    for field in TRACKED_FIELDS:
        history = state.attrs[field].history
        if not history.added:
            continue
        old = history.deleted[0] if history.deleted else None
        new = history.added[0]
        if old != new:
            found[field] = (old, new)
    return found


def _draft_for(state: InstanceState[Any]) -> EventDraft | None:
    speaker = state.obj()
    restamped = any(v is not None for v in state.attrs["identity_source_at"].history.added)
    row = {
        name: getattr(speaker, name)
        for name in (
            "identity_source",
            "identity_source_user_id",
            "identity_source_detail",
            "suggestion_source",
            "suggestion_detail",
        )
    }
    return derive_event(
        _changes(state), row, state.info.pop(HISTORY_NOTES_KEY, ()), restamped=restamped
    )


@event.listens_for(Session, "before_flush")
def _record_speaker_identity_history(
    session: Session, _flush_context: UOWTransaction, _instances: object
) -> None:
    """Append one history row per speaker whose name, link or suggestion this flush changes.

    Every ORM writer of those fields goes through the helpers of
    ``app/utils/speaker_identity_source`` (``tests/unit/test_speaker_identity_source_discipline``),
    and every one of their writes reaches the database through a flush, so this is the one place
    that cannot be forgotten (the shape of ``content_hold``'s membership listener). The row is
    added to the same flush, so it commits or rolls back with the change. Bulk ``UPDATE``
    statements bypass the flush and record through
    ``app.services.speaker_identity_history.record_bulk_identity_events``.
    """
    from app.models.speaker import Speaker

    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Speaker):
            continue
        state = inspect(obj)
        if state.deleted or obj in session.deleted:
            continue
        draft = _draft_for(state)
        if draft is None:
            continue
        session.add(
            SpeakerIdentityEvent(
                speaker=obj,
                organization_id=obj.organization_id,
                kind=draft.kind,
                actor_kind=draft.actor_kind,
                actor_user_id=draft.actor_user_id,
                origin=draft.origin,
                model=draft.model,
                confidence=draft.confidence,
                voiceprint=draft.voiceprint,
                before_value=draft.before,
                after_value=draft.after,
                detail=draft.detail,
            )
        )


@event.listens_for(Session, "after_soft_rollback")
def _forget_unflushed_notes(session: Session, _previous_transaction: object) -> None:
    """A rolled-back change must not lend its attribution to the next write of the same row."""
    from app.models.speaker import Speaker

    for obj in list(session.identity_map.values()):
        if isinstance(obj, Speaker):
            inspect(obj).info.pop(HISTORY_NOTES_KEY, None)
