"""Per-user hide of media files (v465, issue #1102).

One row per (user, file) a user has hidden from their OWN default views (gallery, dashboard,
collection and tag lists, search, corpus-wide chat). Hide is a view preference, not access
control: it never changes what another user sees, never blocks a direct link, and never drops a
file a person explicitly picked. Read and written only through
``services/hidden_media_service.py``.

Both foreign keys CASCADE: a deleted account leaves no hide rows, and a purged file takes every
user's hide rows with it. A trashed file keeps its rows, so after a restore it is still hidden for
whoever hid it (plan §4.4: trash wins over hide while it lasts).
"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class UserHiddenMedia(Base):
    """A media file one user has hidden from their own views."""

    __tablename__ = "user_hidden_media"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "media_file_id", name="pk_user_hidden_media"),
        Index("ix_user_hidden_media_media_file", "media_file_id"),
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    media_file_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media_file.id", ondelete="CASCADE"), nullable=False
    )
    hidden_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
