"""Queries behind the watch-source email configs and the links that subscribe sources to them."""

from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail


def find_email_config(db: Session, config_uuid: str) -> EmailNotificationConfig | None:
    """The email config with this uuid, or ``None``."""
    return (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )


def list_email_configs_with_links(db: Session) -> list[EmailNotificationConfig]:
    """Every config, name-ordered, with its links loaded.

    ``selectinload`` because the response counts ``cfg.links``: lazily that is one extra
    query per config.
    """
    return (
        db.query(EmailNotificationConfig)
        .options(selectinload(EmailNotificationConfig.links))
        .order_by(EmailNotificationConfig.name)
        .all()
    )


def list_all_configs_by_name(db: Session) -> list[EmailNotificationConfig]:
    """Every config, name-ordered, without loading links."""
    return db.query(EmailNotificationConfig).order_by(EmailNotificationConfig.name).all()


def links_of_source(db: Session, source_id: int) -> list[WatchSourceEmail]:
    """The source's email links with each link's config loaded."""
    return (
        db.query(WatchSourceEmail)
        .options(selectinload(WatchSourceEmail.email_config))
        .filter(WatchSourceEmail.watch_source_id == source_id)
        .all()
    )


def linked_config_ids(db: Session, source_id: int) -> set[int]:
    """Ids of the configs this source already notifies."""
    return {
        link.email_config_id
        for link in db.query(WatchSourceEmail)
        .filter(WatchSourceEmail.watch_source_id == source_id)
        .all()
    }


def find_link(db: Session, source_id: int, config_id: int) -> WatchSourceEmail | None:
    """The link between this source and config, or ``None``."""
    return (
        db.query(WatchSourceEmail)
        .filter(
            WatchSourceEmail.watch_source_id == source_id,
            WatchSourceEmail.email_config_id == config_id,
        )
        .first()
    )
