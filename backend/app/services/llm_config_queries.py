"""Reads and writes of a user's LLM configurations (``UserLLMSettings`` rows).

Every function takes the request's ``Session`` and plain arguments, and none commits: the
caller keeps the transaction boundary it had when these queries lived in the router.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.user_llm_settings import UserLLMSettings
from app.utils.tenant_sharing import owner_in_tenant
from app.utils.tenant_sharing import shared_visible_in_tenant


def list_own_llm_configs(db: Session, user_id: int) -> list[UserLLMSettings]:
    """The user's configurations, newest first."""
    return (
        db.query(UserLLMSettings)
        .filter(UserLLMSettings.user_id == user_id)
        .order_by(UserLLMSettings.created_at.desc())
        .all()
    )


def count_own_llm_configs(db: Session, user_id: int) -> int:
    """How many configurations the user owns."""
    return db.query(UserLLMSettings).filter(UserLLMSettings.user_id == user_id).count()


def list_shared_llm_configs(db: Session, user_id: int, org_id: int | None) -> list[UserLLMSettings]:
    """Configurations other users share inside tenant ``org_id``, newest share first."""
    return (
        db.query(UserLLMSettings)
        .filter(
            UserLLMSettings.is_shared.is_(True),
            UserLLMSettings.user_id != user_id,
            owner_in_tenant(UserLLMSettings.user_id, org_id),
        )
        .order_by(UserLLMSettings.shared_at.desc())
        .all()
    )


def find_visible_llm_config(
    db: Session, config_id: int, user_id: int, org_id: int | None
) -> UserLLMSettings | None:
    """The configuration with this id when the user owns it or it is shared in tenant ``org_id``."""
    return (
        db.query(UserLLMSettings)
        .filter(
            UserLLMSettings.id == config_id,
            shared_visible_in_tenant(
                UserLLMSettings.user_id, UserLLMSettings.is_shared, user_id, org_id
            ),
        )
        .first()
    )


def llm_config_is_visible(db: Session, config_id: int, user_id: int, org_id: int | None) -> bool:
    """Whether the user may see configuration ``config_id`` (own, or shared in the tenant)."""
    return (
        db.query(UserLLMSettings.id)
        .filter(
            UserLLMSettings.id == config_id,
            shared_visible_in_tenant(
                UserLLMSettings.user_id, UserLLMSettings.is_shared, user_id, org_id
            ),
        )
        .first()
        is not None
    )


def find_own_llm_config_by_uuid(
    db: Session, config_uuid: object, user_id: int
) -> UserLLMSettings | None:
    """The user's own configuration with this public UUID, or ``None``."""
    return (
        db.query(UserLLMSettings)
        .filter(UserLLMSettings.uuid == config_uuid, UserLLMSettings.user_id == user_id)
        .first()
    )


def find_own_llm_config_by_name(
    db: Session, user_id: int, name: str, *, exclude_id: int | None = None
) -> UserLLMSettings | None:
    """The user's configuration called ``name``, ignoring ``exclude_id``."""
    query = db.query(UserLLMSettings).filter(
        UserLLMSettings.user_id == user_id, UserLLMSettings.name == name
    )
    if exclude_id is not None:
        query = query.filter(UserLLMSettings.id != exclude_id)
    return query.first()


def find_other_own_llm_config(db: Session, user_id: int, config_id: int) -> UserLLMSettings | None:
    """Any configuration of the user's other than ``config_id``."""
    return (
        db.query(UserLLMSettings)
        .filter(UserLLMSettings.user_id == user_id, UserLLMSettings.id != config_id)
        .first()
    )


def find_own_llm_config(db: Session, user_id: int, config_id: int) -> UserLLMSettings | None:
    """The user's configuration with this primary key."""
    return (
        db.query(UserLLMSettings)
        .filter(UserLLMSettings.user_id == user_id, UserLLMSettings.id == config_id)
        .first()
    )


def deactivate_other_llm_configs(db: Session, user_id: int, keep_id: int) -> None:
    """Bulk-clear ``is_active`` on every active configuration of the user's but ``keep_id``."""
    db.query(UserLLMSettings).filter(
        UserLLMSettings.user_id == user_id,
        UserLLMSettings.id != keep_id,
        UserLLMSettings.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session=False)


def delete_all_own_llm_configs(db: Session, user_id: int) -> int:
    """Bulk-delete the user's configurations; returns how many went."""
    return int(db.query(UserLLMSettings).filter(UserLLMSettings.user_id == user_id).delete())
