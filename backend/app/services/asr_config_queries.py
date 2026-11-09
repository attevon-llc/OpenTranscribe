"""Reads and writes of a user's ASR provider configurations (``UserASRSettings`` rows).

Every function takes the request's ``Session`` and plain arguments, and none commits: the
caller keeps the transaction boundary it had when these queries lived in the router.
"""

from __future__ import annotations

from collections.abc import Collection
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.user_asr_settings import UserASRSettings
from app.utils.tenant_sharing import owner_in_tenant
from app.utils.tenant_sharing import shared_visible_in_tenant


def list_own_asr_configs(db: Session, user_id: int) -> list[UserASRSettings]:
    """The user's configurations, oldest first."""
    return (
        db.query(UserASRSettings)
        .filter(UserASRSettings.user_id == user_id)
        .order_by(UserASRSettings.created_at)
        .all()
    )


def user_has_asr_config(db: Session, user_id: int) -> bool:
    """Whether the user owns at least one configuration."""
    return (
        db.query(UserASRSettings.id).filter(UserASRSettings.user_id == user_id).limit(1).scalar()
        is not None
    )


def find_shared_asr_config_in_tenant(
    db: Session, config_id: int, org_id: int | None
) -> UserASRSettings | None:
    """The configuration with this id when it is shared inside tenant ``org_id``."""
    return (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.id == config_id,
            UserASRSettings.is_shared.is_(True),
            owner_in_tenant(UserASRSettings.user_id, org_id),
        )
        .first()
    )


def list_shared_asr_configs(db: Session, user_id: int, org_id: int | None) -> list[UserASRSettings]:
    """Configurations other users share inside tenant ``org_id``, oldest first."""
    return (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.is_shared.is_(True),
            UserASRSettings.user_id != user_id,
            owner_in_tenant(UserASRSettings.user_id, org_id),
        )
        .order_by(UserASRSettings.created_at)
        .all()
    )


def list_admin_shared_asr_configs(
    db: Session, org_id: int | None, elevated_roles: Collection[str]
) -> list[tuple[UserASRSettings, User]]:
    """``(config, owner)`` for shared configurations whose owner holds one of ``elevated_roles``."""
    return [
        (config, owner)
        for config, owner in (
            db.query(UserASRSettings, User)
            .join(User, User.id == UserASRSettings.user_id)
            .filter(
                UserASRSettings.is_shared.is_(True),
                User.role.in_(elevated_roles),
                owner_in_tenant(UserASRSettings.user_id, org_id),
            )
            .order_by(UserASRSettings.created_at)
            .all()
        )
    ]


def find_asr_config_by_uuid(
    db: Session,
    config_uuid: UUID,
    user_id: int,
    *,
    shared_org_id: int | None = None,
    include_shared: bool = False,
) -> UserASRSettings | None:
    """The configuration with this UUID that the user owns.

    With ``include_shared`` a configuration shared inside tenant ``shared_org_id`` also
    qualifies; another tenant's shared configuration answers like a missing one.
    """
    query = db.query(UserASRSettings).filter(UserASRSettings.uuid == config_uuid)
    if include_shared:
        query = query.filter(
            shared_visible_in_tenant(
                UserASRSettings.user_id, UserASRSettings.is_shared, user_id, shared_org_id
            )
        )
    else:
        query = query.filter(UserASRSettings.user_id == user_id)
    return query.first()


def find_own_asr_config_by_name(
    db: Session, user_id: int, name: str | None, *, exclude_id: int | None = None
) -> UserASRSettings | None:
    """The user's configuration called ``name``, ignoring ``exclude_id``."""
    query = db.query(UserASRSettings).filter(
        UserASRSettings.user_id == user_id, UserASRSettings.name == name
    )
    if exclude_id is not None:
        query = query.filter(UserASRSettings.id != exclude_id)
    return query.first()


def list_promotable_asr_configs(
    db: Session, user_id: int, excluding_id: int
) -> list[UserASRSettings]:
    """The user's other active configurations in id order; an inactive one is never promoted."""
    return (
        db.query(UserASRSettings)
        .filter(
            UserASRSettings.user_id == user_id,
            UserASRSettings.id != excluding_id,
            UserASRSettings.is_active.is_(True),
        )
        .order_by(UserASRSettings.id)
        .all()
    )


def asr_config_uuid_by_id(db: Session, config_id: int) -> UUID | None:
    """The public UUID of the configuration with this primary key, or ``None``."""
    value: UUID | None = (
        db.query(UserASRSettings.uuid).filter(UserASRSettings.id == config_id).scalar()
    )
    return value


def delete_all_own_asr_configs(db: Session, user_id: int) -> None:
    """Bulk-delete the user's configurations."""
    db.query(UserASRSettings).filter(UserASRSettings.user_id == user_id).delete()
