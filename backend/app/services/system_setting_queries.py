"""Row-level lookup of ``SystemSettings`` for the admin endpoints that edit a row in place.

Value reads with a coded default belong to ``system_settings_service``; this is only for
callers that need the ORM row itself (to update it, or to delete the override). It does not
commit: the caller keeps the transaction boundary it had.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.system_settings import SystemSettings


def find_system_setting(db: Session, key: str) -> SystemSettings | None:
    """The deployment-wide setting row stored under ``key``, or ``None``."""
    return db.query(SystemSettings).filter(SystemSettings.key == key).first()
