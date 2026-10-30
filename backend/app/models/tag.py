"""SQLAlchemy models for tags and the file-to-tag association."""

import uuid as uuid_pkg
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy import Float
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

from app.db.base import Base
from app.utils.uuid7 import uuid7

if TYPE_CHECKING:
    from app.models.media import MediaFile


class Tag(Base):
    """A tag, belonging to one tenant or shared as system vocabulary.

    Three kinds (migrations ``v374_add_tag_user_id`` and
    ``v420_add_tag_organization_id``):

    * **system** — ``user_id IS NULL AND organization_id IS NULL``: the seeded
      ``Important`` / ``Meeting`` / ``Interview`` / ``Personal`` set, visible in
      every tenant.
    * **personal** — ``organization_id IS NULL``, ``user_id`` = the owner.
    * **organization** — ``organization_id`` set; shared by every member of that
      org. ``user_id`` is the creator (attribution only) and may be NULL once
      their account is gone, which is why ``user_id IS NULL`` alone never means
      "system".

    Uniqueness is per tenant, so ``name`` alone can match several rows — never
    look a tag up by name without a tenant predicate
    (``tag_service.owned_or_system``) or a join through ``file_tag``.
    """

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user.id"), nullable=True, index=True
    )
    # No ON DELETE, like every other org stamp: tenant erasure deletes the org's
    # tags explicitly (gdpr_erasure_service.erase_organization).
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True
    )
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "manual" | "auto_ai" | "ai_accepted"
    normalized_name: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Declared to match the column v230_add_auto_labeling created. The column has
    # existed in every database since then; only the model was missing it, so it
    # showed up as model-vs-schema drift (issue #398). Adopting it is the
    # non-destructive fix — dropping a populated timestamptz to satisfy a
    # comparison would be the wrong direction entirely.
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    # One partial unique index per tenant kind rather than one composite
    # UNIQUE: Postgres treats NULLs as distinct, so a plain composite would allow
    # duplicate system tags and break the idempotent seeder.
    __table_args__ = (
        Index(
            "uq_tag_user_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL AND organization_id IS NULL"),
        ),
        Index(
            "uq_tag_system_name",
            "name",
            unique=True,
            postgresql_where=text("user_id IS NULL AND organization_id IS NULL"),
        ),
        Index(
            "uq_tag_org_name",
            "organization_id",
            "name",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
        Index(
            "ix_tag_organization_id",
            "organization_id",
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
    )


class FileTag(Base):
    __tablename__ = "file_tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    # Both FKs are ``ON DELETE CASCADE`` in the DDL (v010:193-194) and were the
    # only two of the schema's 103 foreign keys whose ``ondelete`` the ORM did not
    # mirror: deleting a MediaFile or a Tag already removes these rows in the
    # database while the ORM believed it had to do the work itself.
    media_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE")
    )
    tag_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tag.id", ondelete="CASCADE"))
    source: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # "manual" | "auto_ai" | "ai_accepted"
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="file_tags")
    tag: Mapped["Tag"] = relationship("Tag")

    # Present in the DDL since ``v010_baseline`` but omitted here until now, so
    # the ORM understated the schema and ``merge_tags`` met the violation at
    # runtime instead of planning for it. One file carries a given tag once.
    #
    # It cannot express the *stronger* invariant — one file never carries two
    # differently-owned rows of the same normalized name — because the name
    # lives on ``tag``, not here. That one is held at the single choke point
    # every attach path goes through (``tag_service.resolve_or_create_tag``
    # consults ``lookup_tag_on_file`` first) and repaired for legacy rows by
    # the collision/merge pass.
    __table_args__ = (UniqueConstraint("media_file_id", "tag_id"),)
