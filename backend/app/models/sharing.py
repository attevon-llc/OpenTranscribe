"""SQLAlchemy model for collection sharing."""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.group import UserGroup
    from app.models.media import Collection
    from app.models.media import Tag
    from app.models.user import User


class CollectionShare(Base):
    """Sharing grant on a collection for a user or group."""

    __tablename__ = "collection_share"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    collection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("collection.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shared_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "user" or "group"
    target_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    target_group_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_group.id", ondelete="CASCADE"), nullable=True, index=True
    )
    permission: Mapped[str] = mapped_column(
        String(20), nullable=False, default="viewer"
    )  # "viewer" or "editor"
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(target_user_id IS NOT NULL AND target_group_id IS NULL) OR "
            "(target_user_id IS NULL AND target_group_id IS NOT NULL)",
            name="_collection_share_target_check",
        ),
        Index(
            "_collection_share_user_uc",
            "collection_id",
            "target_user_id",
            unique=True,
            postgresql_where=text("target_user_id IS NOT NULL"),
        ),
        Index(
            "_collection_share_group_uc",
            "collection_id",
            "target_group_id",
            unique=True,
            postgresql_where=text("target_group_id IS NOT NULL"),
        ),
    )

    # Relationships
    collection: Mapped["Collection"] = relationship("Collection", back_populates="shares")
    shared_by: Mapped["User"] = relationship(
        "User", foreign_keys=[shared_by_id], back_populates="shared_by_me"
    )
    target_user: Mapped["User | None"] = relationship(
        "User", foreign_keys=[target_user_id], back_populates="shared_with_me"
    )
    target_group: Mapped["UserGroup | None"] = relationship(
        "UserGroup", back_populates="collection_shares"
    )


class TagShare(Base):
    """Sharing grant on a tag for a user or group.

    Mirrors :class:`CollectionShare` — same target shape, same CASCADE rules,
    same partial unique indexes — because sharing is already solved in this
    schema and a second, differently-shaped grant table would be a second set
    of rules to keep in step.

    **No permission column, deliberately.** A collection share distinguishes
    viewer from editor because a collection carries files you might be allowed
    to change. A tag share grants *vocabulary*: the recipient can see the tag,
    filter by it and apply it, while rename / merge / delete stay with the owner
    (or an admin, for system tags). A column the authorization code would always
    read as "viewer" would be a field pretending to be a choice.
    """

    __tablename__ = "tag_share"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tag.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shared_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "user" or "group"
    target_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    target_group_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_group.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Two FKs to `user` (shared_by_id and target_user_id) need explicit
    # foreign_keys on BOTH sides, or mapper configuration crashes at import and
    # the whole app fails to start.
    tag: Mapped["Tag"] = relationship("Tag", foreign_keys=[tag_id])
    shared_by_user: Mapped["User"] = relationship("User", foreign_keys=[shared_by_id])
    target_user: Mapped["User | None"] = relationship("User", foreign_keys=[target_user_id])
    target_group: Mapped["UserGroup | None"] = relationship(
        "UserGroup", foreign_keys=[target_group_id]
    )

    __table_args__ = (
        CheckConstraint(
            "(target_user_id IS NOT NULL AND target_group_id IS NULL) OR "
            "(target_user_id IS NULL AND target_group_id IS NOT NULL)",
            name="_tag_share_target_check",
        ),
        Index(
            "_tag_share_user_uc",
            "tag_id",
            "target_user_id",
            unique=True,
            postgresql_where=text("target_user_id IS NOT NULL"),
        ),
        Index(
            "_tag_share_group_uc",
            "tag_id",
            "target_group_id",
            unique=True,
            postgresql_where=text("target_group_id IS NOT NULL"),
        ),
    )
