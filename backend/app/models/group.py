"""SQLAlchemy models for user groups and group membership."""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.sharing import CollectionShare
    from app.models.user import User


class UserGroup(Base):
    """User-created group for sharing collections."""

    __tablename__ = "user_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("owner_id", "name", name="_user_group_owner_name_uc"),)

    # Relationships
    owner: Mapped["User"] = relationship("User", back_populates="owned_groups")
    members: Mapped[list["UserGroupMember"]] = relationship(
        "UserGroupMember", back_populates="group", cascade="all, delete-orphan"
    )
    collection_shares: Mapped[list["CollectionShare"]] = relationship(
        "CollectionShare",
        back_populates="target_group",
        foreign_keys="CollectionShare.target_group_id",
        cascade="all, delete-orphan",
    )


class UserGroupMember(Base):
    """Membership record linking users to groups with roles."""

    __tablename__ = "user_group_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_group.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="member"
    )  # "owner", "admin", "member"
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="_group_member_uc"),)

    # Relationships
    group: Mapped["UserGroup"] = relationship("UserGroup", back_populates="members")
    user: Mapped["User"] = relationship("User", back_populates="group_memberships")
