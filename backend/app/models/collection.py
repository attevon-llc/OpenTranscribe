"""SQLAlchemy models for file collections and their membership."""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.media import MediaFile
    from app.models.prompt import SummaryPrompt
    from app.models.sharing import CollectionShare
    from app.models.user import User


class Collection(Base):
    """A named set of media files, belonging to one tenant.

    Two kinds (migration ``v422_add_collection_tenancy``, issue #1051):

    * **personal** — ``organization_id IS NULL``, ``user_id`` = the owner; seen by
      the owner and whoever they share it with explicitly.
    * **organization** — ``organization_id`` set; shared by every member of that
      org (members are editors; the creator and org admins are owners).
      ``user_id`` is the creator (attribution) and becomes NULL once their account
      is gone — the collection stays with the tenant.

    Names are unique per tenant, so never look a collection up by name without a
    tenant predicate (``PermissionService.collection_tenant_pred``).
    """

    __tablename__ = "collection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("user.id"), nullable=True)
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    is_public: Mapped[bool | None] = mapped_column(Boolean, default=False)
    default_summary_prompt_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("summary_prompt.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "manual" | "auto_ai" | "bulk_group"
    # ``organization`` (every member sees it) or ``restricted`` (creator, org admins and its
    # shares only, v446, issue #1275). Read it through ``services/collection_visibility``.
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default="organization", server_default="organization"
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # One partial unique index per tenant kind (v422): a user may hold one name in
    # several tenants, and an org holds each name once across all its members.
    __table_args__ = (
        CheckConstraint(
            "user_id IS NOT NULL OR organization_id IS NOT NULL",
            name="ck_collection_owner_or_org",
        ),
        CheckConstraint(
            "visibility IN ('organization', 'restricted') "
            "AND (visibility = 'organization' OR organization_id IS NOT NULL)",
            name="ck_collection_visibility",
        ),
        Index("ix_collection_org_visibility", "organization_id", "visibility"),
        Index(
            "uq_collection_user_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
        Index(
            "uq_collection_org_name",
            "organization_id",
            "name",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
    )

    # Relationships
    user: Mapped["User | None"] = relationship("User", back_populates="collections")
    collection_members: Mapped[list["CollectionMember"]] = relationship(
        "CollectionMember", back_populates="collection", cascade="all, delete-orphan"
    )
    default_summary_prompt: Mapped["SummaryPrompt | None"] = relationship(
        "SummaryPrompt", foreign_keys=[default_summary_prompt_id]
    )
    # Sharing relationships
    shares: Mapped[list["CollectionShare"]] = relationship(
        "CollectionShare", back_populates="collection", cascade="all, delete-orphan"
    )


class CollectionMember(Base):
    __tablename__ = "collection_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    collection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("collection.id", ondelete="CASCADE"), nullable=False
    )
    media_file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "manual" | "auto_ai" | "bulk_group"
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    added_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Unique constraint
    __table_args__ = (
        UniqueConstraint("collection_id", "media_file_id", name="_collection_member_uc"),
    )

    # Relationships
    collection: Mapped["Collection"] = relationship(
        "Collection", back_populates="collection_members"
    )
    media_file: Mapped["MediaFile"] = relationship(
        "MediaFile", back_populates="collection_memberships"
    )
