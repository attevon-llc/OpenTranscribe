"""``auth_config`` row reads for the auth-config and login-gate routers. None commit."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.orm import Session

from app.models.auth_config import AuthConfig


def list_configs_in_category(db: Session, category: str) -> list[AuthConfig]:
    """Every config row filed under ``category``."""
    return db.query(AuthConfig).filter(AuthConfig.category == category).all()


def get_configs_by_keys(db: Session, config_keys: Collection[str]) -> dict[str, AuthConfig]:
    """``{config_key: row}`` for the keys that exist."""
    rows = db.query(AuthConfig).filter(AuthConfig.config_key.in_(config_keys)).all()
    return {row.config_key: row for row in rows}
