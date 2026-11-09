"""Reads of per-user media sources (``UserMediaSource`` rows).

Every function takes the request's ``Session`` and plain arguments, and none commits.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.user_media_source import UserMediaSource
from app.utils.tenant_sharing import owner_in_tenant


def list_own_media_sources(db: Session, user_id: int) -> list[UserMediaSource]:
    """The user's sources, newest first."""
    return (
        db.query(UserMediaSource)
        .filter(UserMediaSource.user_id == user_id)
        .order_by(UserMediaSource.created_at.desc())
        .all()
    )


def list_shared_media_sources(
    db: Session, user_id: int, org_id: int | None
) -> list[UserMediaSource]:
    """Active, shared sources of other active users inside tenant ``org_id``, newest share first."""
    return (
        db.query(UserMediaSource)
        .join(User, User.id == UserMediaSource.user_id)
        .filter(
            UserMediaSource.is_shared.is_(True),
            UserMediaSource.is_active.is_(True),
            UserMediaSource.user_id != user_id,
            User.is_active.is_(True),
            owner_in_tenant(UserMediaSource.user_id, org_id),
        )
        .order_by(UserMediaSource.shared_at.desc().nullslast())
        .all()
    )


def count_media_sources(db: Session, user_id: int) -> int:
    """How many sources the user owns."""
    return db.query(UserMediaSource).filter(UserMediaSource.user_id == user_id).count()


def find_own_media_source(db: Session, source_uuid: str, user_id: int) -> UserMediaSource | None:
    """The user's source with this public UUID, or ``None``."""
    return (
        db.query(UserMediaSource)
        .filter(UserMediaSource.uuid == source_uuid, UserMediaSource.user_id == user_id)
        .first()
    )
