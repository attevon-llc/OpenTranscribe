"""Invitation listing for the admin invitation route. Does not commit."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.invitation import UserInvitation

INVITATION_LIST_LIMIT = 500


def list_invitations(db: Session, include_inactive: bool) -> list[UserInvitation]:
    """Newest invitations first; pending only unless ``include_inactive``."""
    query = db.query(UserInvitation)
    if not include_inactive:
        query = query.filter(UserInvitation.used_at.is_(None), UserInvitation.revoked_at.is_(None))
    return query.order_by(UserInvitation.created_at.desc()).limit(INVITATION_LIST_LIMIT).all()
