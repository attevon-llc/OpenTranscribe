"""Reads over ``refresh_token`` rows (a session IS one) for the auth and admin routers.

None of these commit; the sign-in path commits the row it later inserts.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.refresh_token import RefreshToken


def list_active_sessions(db: Session, user_id: int) -> list[RefreshToken]:
    """The account's unrevoked, unexpired sessions."""
    return (
        db.query(RefreshToken)
        .filter(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        .all()
    )


def lock_active_sessions(db: Session, user_id: int) -> list[RefreshToken]:
    """The account's active sessions, row-locked (``FOR UPDATE``) until the transaction ends.

    Locks rows rather than counting, because ``FOR UPDATE`` cannot be applied to an aggregate.
    """
    stmt = (
        select(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        .with_for_update()
    )
    return list(db.execute(stmt).scalars().all())
