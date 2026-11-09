"""Reads and writes of per-user key/value settings (``UserSetting`` rows).

Every function takes the request's ``Session`` and plain arguments, and none commits: the
caller keeps the transaction boundary it had when these queries lived in the router.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.models.prompt import UserSetting
from app.models.user import User
from app.utils.tenant_sharing import owner_in_tenant


def find_user_setting(db: Session, user_id: int, setting_key: str) -> UserSetting | None:
    """The user's row for ``setting_key``, or ``None``."""
    return (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user_id, UserSetting.setting_key == setting_key)
        .first()
    )


def list_user_settings(
    db: Session, user_id: int, setting_keys: Sequence[str] | None = None
) -> list[UserSetting]:
    """The user's rows, restricted to ``setting_keys`` when given."""
    query = db.query(UserSetting).filter(UserSetting.user_id == user_id)
    if setting_keys is not None:
        query = query.filter(UserSetting.setting_key.in_(setting_keys))
    return query.all()


def user_setting_values(db: Session, user_id: int, setting_keys: Sequence[str]) -> dict[str, str]:
    """``key -> value`` for the user's rows among ``setting_keys``, skipping empty values."""
    rows = (
        db.query(UserSetting.setting_key, UserSetting.setting_value)
        .filter(UserSetting.user_id == user_id, UserSetting.setting_key.in_(setting_keys))
        .all()
    )
    return {str(key): str(value) for key, value in rows if value}


def upsert_user_setting(db: Session, user_id: int, setting_key: str, setting_value: Any) -> None:
    """Insert or update one setting; a bool is stored as ``"true"``/``"false"``."""
    if isinstance(setting_value, bool):
        db_value = "true" if setting_value else "false"
    else:
        db_value = str(setting_value)

    existing = find_user_setting(db, user_id, setting_key)
    if existing:
        existing.setting_value = db_value
        db.add(existing)
    else:
        db.add(UserSetting(user_id=user_id, setting_key=setting_key, setting_value=db_value))


def delete_user_settings(db: Session, user_id: int, setting_keys: Sequence[str]) -> int:
    """Bulk-delete the user's rows among ``setting_keys``; returns how many went."""
    return int(
        db.query(UserSetting)
        .filter(UserSetting.user_id == user_id, UserSetting.setting_key.in_(setting_keys))
        .delete(synchronize_session=False)
    )


def list_settings_holding_value(
    db: Session, setting_key: str, setting_values: Collection[str]
) -> list[UserSetting]:
    """Every user's row for ``setting_key`` whose value is one of ``setting_values``."""
    return (
        db.query(UserSetting)
        .filter(
            UserSetting.setting_key == setting_key, UserSetting.setting_value.in_(setting_values)
        )
        .all()
    )


def list_user_settings_with_value(
    db: Session, user_ids: Collection[int], setting_key: str, setting_value: str
) -> list[UserSetting]:
    """Rows of ``setting_key`` equal to ``setting_value`` among the given users."""
    return (
        db.query(UserSetting)
        .filter(
            UserSetting.user_id.in_(user_ids),
            UserSetting.setting_key == setting_key,
            UserSetting.setting_value == setting_value,
        )
        .all()
    )


def delete_settings_holding_value(
    db: Session, setting_key: str, setting_value: str, *, exclude_user_id: int | None = None
) -> None:
    """Bulk-delete every user's ``setting_key`` row equal to ``setting_value``.

    ``exclude_user_id`` spares that one user's row.
    """
    query = db.query(UserSetting).filter(
        UserSetting.setting_key == setting_key,
        UserSetting.setting_value == setting_value,
    )
    if exclude_user_id is not None:
        query = query.filter(UserSetting.user_id != exclude_user_id)
    query.delete(synchronize_session=False)


def list_user_ids_holding_value(
    db: Session, setting_key: str, setting_value: str, *, exclude_user_id: int | None = None
) -> list[int]:
    """Ids of the users whose ``setting_key`` equals ``setting_value``, sparing ``exclude_user_id``."""
    query = db.query(UserSetting.user_id).filter(
        UserSetting.setting_key == setting_key, UserSetting.setting_value == setting_value
    )
    if exclude_user_id is not None:
        query = query.filter(UserSetting.user_id != exclude_user_id)
    return [int(row[0]) for row in query.all()]


def users_by_id(db: Session, user_ids: Collection[int]) -> dict[int, User]:
    """``id -> User`` for the given ids; an empty collection issues no query."""
    if not user_ids:
        return {}
    return {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}


def list_user_settings_for_users(
    db: Session, user_ids: Collection[int], setting_key: str
) -> list[UserSetting]:
    """The ``setting_key`` rows belonging to any of ``user_ids``."""
    return (
        db.query(UserSetting)
        .filter(UserSetting.setting_key == setting_key, UserSetting.user_id.in_(user_ids))
        .all()
    )


def list_shared_org_context_flags(
    db: Session, excluding_user_id: int, org_id: int | None
) -> list[UserSetting]:
    """Shared-organization-context flags of other users inside the tenant ``org_id``."""
    return (
        db.query(UserSetting)
        .filter(
            UserSetting.setting_key == "org_context_is_shared",
            UserSetting.setting_value == "true",
            UserSetting.user_id != excluding_user_id,
            owner_in_tenant(UserSetting.user_id, org_id),
        )
        .all()
    )


def find_shared_org_context_flag(
    db: Session, owner_user_id: int, org_id: int | None
) -> UserSetting | None:
    """``owner_user_id``'s shared-context flag when that user is inside tenant ``org_id``."""
    return (
        db.query(UserSetting)
        .filter(
            UserSetting.user_id == owner_user_id,
            UserSetting.setting_key == "org_context_is_shared",
            UserSetting.setting_value == "true",
            owner_in_tenant(UserSetting.user_id, org_id),
        )
        .first()
    )
