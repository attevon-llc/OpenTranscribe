"""TOTP enrolment row lookups for the auth and admin routers. None of these commit."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.user_mfa import UserMFA


def get_user_mfa(db: Session, user_id: int) -> UserMFA | None:
    """The account's MFA enrolment row, or ``None`` if it never started one."""
    return db.query(UserMFA).filter(UserMFA.user_id == user_id).first()


def count_totp_enabled(db: Session) -> int:
    """How many accounts have TOTP switched on."""
    return int(db.query(func.count(UserMFA.id)).filter(UserMFA.totp_enabled.is_(True)).scalar())
