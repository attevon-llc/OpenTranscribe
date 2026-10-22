"""Takedown notice records: the durable history of why a file was taken down (issue #1254).

Quarantine state lives on ``media_file`` and release wipes it, and the audit trail is a
log with a retention setting. Neither answers "which notice did we receive three years
ago, about which file, and what became of it" once the file is purged. These tables do.

* ``takedown_notice`` - one row per legal notice, or per operator action that had none.
* ``takedown_notice_file`` - one row per file a notice covers, carrying that file's
  outcome. A letter can cover ten files and end differently for each, so the outcome
  lives here and a notice's overall state is *derived* from its links, never stored.
* ``takedown_notification`` (v444) - who was told, by which channel, with what result.

**The records outlive what they describe, on purpose.** The file, its owner and its
organization are held as *snapshot uuids* beside ``ON DELETE SET NULL`` keys, never as
bare ``CASCADE`` or ``NO ACTION`` keys: ``CASCADE`` would delete the evidence with the
file, ``NO ACTION`` would make purge and erasure fail. The uuids are meaningless once the
row they named is gone, which is what makes them safe to keep (same doctrine as
``models/erasure.py``). There is no column for a requester's name, address or contact
detail: ``requester_reference`` is an opaque case number or ticket id, and the operator
keeps the complainant's details in their own ticketing system.
"""

import uuid as uuid_pkg
from datetime import datetime

from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.enums import TakedownLinkStatus
from app.core.enums import TakedownNoticeType
from app.core.enums import TakedownNotifyEvent
from app.core.enums import TakedownNotifyStatus
from app.db.base import Base
from app.utils.uuid7 import uuid7

#: Who recorded the notice. ``organization`` is the organization-administrator route.
RECORDED_BY_AUTHORITIES = ("platform", "organization")
NOTIFY_RECIPIENT_KINDS = ("owner", "org_admin")
NOTIFY_CHANNELS = ("in_app", "email")


def sql_in(column: str, values: tuple[str, ...]) -> str:
    """Render ``col IN ('a','b')`` so the ORM CHECK and the migration share one list."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


NOTICE_TYPES = tuple(t.value for t in TakedownNoticeType)
LINK_STATUSES = tuple(s.value for s in TakedownLinkStatus)
NOTIFY_EVENTS = tuple(e.value for e in TakedownNotifyEvent)
NOTIFY_STATUSES = tuple(s.value for s in TakedownNotifyStatus)

#: A real notice carries a reference; an operator action cannot pretend to have one.
OPERATOR_ACTION_NO_REFERENCE_SQL = (
    "(notice_type = 'operator_action') = (requester_reference IS NULL)"
)
COUNTER_NOTICE_TIMESTAMP_SQL = (
    "status <> 'counter_notice' OR counter_notice_received_at IS NOT NULL"
)
OUTCOME_TIMESTAMP_SQL = "status NOT IN ('upheld', 'released') OR outcome_at IS NOT NULL"


class TakedownNotice(Base):
    """One legal notice received by the operator, or one operator action with no notice."""

    __tablename__ = "takedown_notice"
    __table_args__ = (
        CheckConstraint(sql_in("notice_type", NOTICE_TYPES), name="ck_takedown_notice_type_valid"),
        CheckConstraint(
            sql_in("recorded_by_authority", RECORDED_BY_AUTHORITIES),
            name="ck_takedown_notice_authority_valid",
        ),
        CheckConstraint(
            OPERATOR_ACTION_NO_REFERENCE_SQL,
            name="ck_takedown_notice_operator_action_has_no_requester",
        ),
        Index("ix_takedown_notice_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    notice_type: Mapped[str] = mapped_column(String(24), nullable=False)
    #: When the notice reached the operator. Entered by the administrator, so it may
    #: predate the row by years.
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: A case number, ticket id or the complainant's organization. NEVER contact details.
    requester_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The internal legal basis. For ``operator_action`` it is the explicit "no notice"
    #: reason. Never shown to the file owner.
    basis: Mapped[str] = mapped_column(String(2000), nullable=False)
    recorded_by_authority: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'platform'")
    )
    handled_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    #: Snapshot of the handler's uuid; survives the account being deleted.
    handled_by_user_uuid: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    files: Mapped[list["TakedownNoticeFile"]] = relationship(
        "TakedownNoticeFile",
        back_populates="notice",
        order_by="TakedownNoticeFile.id",
        passive_deletes=True,
    )


class TakedownNoticeFile(Base):
    """One file covered by one notice, and what became of it."""

    __tablename__ = "takedown_notice_file"
    __table_args__ = (
        UniqueConstraint("notice_id", "file_uuid", name="uq_takedown_notice_file_notice_file"),
        CheckConstraint(
            sql_in("status", LINK_STATUSES), name="ck_takedown_notice_file_status_valid"
        ),
        CheckConstraint(
            COUNTER_NOTICE_TIMESTAMP_SQL, name="ck_takedown_notice_file_counter_notice_at"
        ),
        CheckConstraint(OUTCOME_TIMESTAMP_SQL, name="ck_takedown_notice_file_outcome_at"),
        Index(
            "ix_takedown_notice_file_live",
            "file_id",
            postgresql_where=text("status IN ('open', 'counter_notice', 'upheld')"),
        ),
        Index("ix_takedown_notice_file_org_quarantined", "organization_id", "quarantined_at"),
        Index("ix_takedown_notice_file_owner", "owner_user_uuid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    #: The app never deletes a notice. CASCADE only serves a deliberate manual delete,
    #: because a link without its notice means nothing.
    notice_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("takedown_notice.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL once the file is purged; ``file_uuid`` then still names it.
    file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="SET NULL"), nullable=True
    )
    file_uuid: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_user_uuid: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="SET NULL"), nullable=True
    )
    organization_uuid: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    #: The exact text the owner was shown. Copied here because release wipes the live
    #: ``media_file.quarantine_reason``.
    owner_reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    quarantined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    counter_notice_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    legal_hold_placed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    notice: Mapped[TakedownNotice] = relationship("TakedownNotice", back_populates="files")


class TakedownNotification(Base):
    """Who was told about one takedown event, by which channel, with what result (v444).

    Every textual column is a CHECK-bounded enum: no address, no error message, no body.
    Failure detail stays in the application log.
    """

    __tablename__ = "takedown_notification"
    __table_args__ = (
        UniqueConstraint(
            "link_id",
            "event",
            "recipient_user_uuid",
            "channel",
            name="uq_takedown_notification_delivery",
        ),
        CheckConstraint(sql_in("event", NOTIFY_EVENTS), name="ck_takedown_notification_event"),
        CheckConstraint(
            sql_in("recipient_kind", NOTIFY_RECIPIENT_KINDS),
            name="ck_takedown_notification_recipient_kind",
        ),
        CheckConstraint(
            sql_in("channel", NOTIFY_CHANNELS), name="ck_takedown_notification_channel"
        ),
        CheckConstraint(sql_in("status", NOTIFY_STATUSES), name="ck_takedown_notification_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("takedown_notice_file.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    recipient_user_uuid: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
