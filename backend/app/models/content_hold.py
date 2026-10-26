"""Organization content holds (issue #1276).

A hold keeps files from being destroyed: no delete, no retention sweep, no account or GDPR
purge, no reprocess. It is placed by an organization administrator, on one or more files or on
a whole collection, and released explicitly; the row stays as history. The platform's own hold
is the older ``media_file.legal_hold`` boolean, and ``services/hold_guard`` is the one place
that answers "is this file held by anyone".

``reason`` and ``matter_ref`` are administrator-only text: they are never copied into an audit
event, a socket message or a file response.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.models.media import CollectionMember
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.media import Collection
    from app.models.media import MediaFile

SCOPE_FILE = "file"
SCOPE_COLLECTION = "collection"
HOLD_SCOPES = (SCOPE_FILE, SCOPE_COLLECTION)

#: Bounds on ``ContentHold.reason`` (mirrored by ``ck_content_hold_reason_length``).
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 2000


class ContentHold(Base):
    """One hold an organization administrator placed. Active while ``released_at`` is NULL."""

    __tablename__ = "content_hold"
    __table_args__ = (
        CheckConstraint("scope IN ('file', 'collection')", name="ck_content_hold_scope"),
        CheckConstraint(
            f"char_length(reason) BETWEEN {REASON_MIN_LENGTH} AND {REASON_MAX_LENGTH}",
            name="ck_content_hold_reason_length",
        ),
        CheckConstraint(
            "scope = 'collection' OR collection_id IS NULL", name="ck_content_hold_collection_scope"
        ),
        Index(
            "ix_content_hold_org_active",
            "organization_id",
            postgresql_where=text("released_at IS NULL"),
        ),
        Index(
            "ix_content_hold_collection",
            "collection_id",
            postgresql_where=text("collection_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True, default=uuid7
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    collection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("collection.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    matter_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    placed_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    released_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    files: Mapped[list["ContentHoldFile"]] = relationship(
        "ContentHoldFile", back_populates="hold", cascade="all, delete-orphan", passive_deletes=True
    )
    collection: Mapped["Collection | None"] = relationship("Collection")

    @property
    def active(self) -> bool:
        return self.released_at is None


class ContentHoldFile(Base):
    """A file a hold covers. A collection hold lists its collection's files, now and later."""

    __tablename__ = "content_hold_file"
    __table_args__ = (Index("ix_content_hold_file_media_file", "media_file_id"),)

    hold_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("content_hold.id", ondelete="CASCADE"), primary_key=True
    )
    media_file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    hold: Mapped[ContentHold] = relationship("ContentHold", back_populates="files")
    media_file: Mapped["MediaFile"] = relationship("MediaFile")


@event.listens_for(CollectionMember, "after_insert")
def _cover_new_member_with_collection_holds(mapper, connection, target) -> None:
    """A file that joins a held collection is held from that moment (issue #1276).

    Every path that adds a collection member (the collection routes, an upload that names a
    target collection, the AI auto-labeller, bulk grouping) inserts a ``CollectionMember``, so
    covering it here is the one place that cannot be forgotten. The insert is idempotent
    (``ON CONFLICT DO NOTHING``), runs in the same flush and so the same transaction as the
    membership, and touches nothing when the collection has no active hold.
    """
    connection.execute(
        text(
            "INSERT INTO content_hold_file (hold_id, media_file_id) "
            "SELECT id, :file_id FROM content_hold "
            "WHERE collection_id = :collection_id AND released_at IS NULL "
            "ON CONFLICT DO NOTHING"
        ),
        {"file_id": target.media_file_id, "collection_id": target.collection_id},
    )
