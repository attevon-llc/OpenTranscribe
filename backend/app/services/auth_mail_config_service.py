"""Designate which ``EmailNotificationConfig`` carries transactional auth mail.

The read half of this decision lives in
:func:`app.services.email_service.load_auth_mail_config`, which resolves the
``SystemSettings`` key ``email.auth_config_uuid`` on every send and degrades to
env SMTP (with an ERROR log) when the designated row is gone or disabled. This
module owns the **write** half, plus the description the admin UI renders.

Two properties matter here and neither belongs in an endpoint body:

* **A broken designation is never saved.** The read path degrades quietly enough
  that a typo'd or stale UUID would only surface as undelivered password resets,
  so a UUID that does not exist — or names a disabled config — is rejected at
  write time with a message naming the fix.
* **The designation is reported with its resolution.** ``config_uuid`` alone
  cannot tell the UI whether auth mail actually works today: the row may have
  been deleted or disabled since. :class:`DesignationStatus` carries both, so a
  dangling designation is visible rather than silent.

Clearing the designation (empty string) is legitimate and means "use the
``SMTP_*`` env transport".
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.constants import AUTH_EMAIL_CONFIG_SETTING_KEY
from app.core.constants import DEFAULT_AUTH_EMAIL_CONFIG_UUID
from app.models.email_notification_config import EmailNotificationConfig
from app.services.system_settings_service import get_setting
from app.services.system_settings_service import set_setting

logger = logging.getLogger(__name__)

#: No row is designated; auth mail uses the env SMTP transport.
STATUS_NOT_DESIGNATED = "not_designated"
#: Designated and resolvable — auth mail goes through this provider.
STATUS_ACTIVE = "active"
#: Designated, but no config with that UUID exists any more.
STATUS_MISSING = "missing"
#: Designated, but the config is disabled, so the sender skips it.
STATUS_DISABLED = "disabled"

#: Stored on the ``SystemSettings`` row so the key explains itself in psql.
SETTING_DESCRIPTION = "EmailNotificationConfig that carries transactional auth mail"

#: Refusal shown when an admin deletes or disables the designated config. Both
#: operations would take auth mail down to the env transport — which is unset in
#: every stock deployment — so they are refused rather than warned about, and the
#: remedy is one control away in the same panel.
IN_USE_MESSAGE = (
    "This email configuration is designated to carry authentication email "
    "(password resets, invitations, verification links). Designate another "
    "configuration, or clear the designation, before changing it."
)


@dataclass(frozen=True)
class DesignationStatus:
    """What is designated, and whether it still resolves.

    Attributes:
        config_uuid: The designated UUID, or ``None`` when nothing is designated.
        config_name: Name of the designated row, ``None`` when it no longer exists.
        provider: ``smtp`` / ``m365`` / ``exchange`` of the designated row.
        is_enabled: Whether that row is enabled; ``None`` when it is missing.
        resolves: True when a send would actually use the designated config.
        status: One of the ``STATUS_*`` constants above.
        env_smtp_configured: Whether ``SMTP_HOST`` is set. With no designation
            and no env SMTP, credential-bearing mail fails outright — the UI needs
            to say so rather than showing an innocuous "not configured".
    """

    config_uuid: str | None
    config_name: str | None
    provider: str | None
    is_enabled: bool | None
    resolves: bool
    status: str
    env_smtp_configured: bool


def _find(db: Session, config_uuid: str) -> EmailNotificationConfig | None:
    """Look up an email config by UUID string, tolerating a malformed value."""
    try:
        parsed = uuid_pkg.UUID(config_uuid)
    except ValueError:
        return None
    return db.query(EmailNotificationConfig).filter(EmailNotificationConfig.uuid == parsed).first()


def designated_uuid(db: Session) -> str | None:
    """Return the raw designated UUID string, or ``None`` when unset/cleared."""
    raw = get_setting(db, AUTH_EMAIL_CONFIG_SETTING_KEY, DEFAULT_AUTH_EMAIL_CONFIG_UUID)
    return raw.strip() or None if raw else None


def is_designated(db: Session, config_uuid: str) -> bool:
    """Whether ``config_uuid`` is the row currently carrying auth mail.

    Compared as UUIDs, not strings, so a differently-cased or brace-wrapped
    stored value still matches the row it names.
    """
    current = designated_uuid(db)
    if not current:
        return False
    try:
        return uuid_pkg.UUID(current) == uuid_pkg.UUID(config_uuid)
    except ValueError:
        return False


def describe_designation(db: Session) -> DesignationStatus:
    """Describe the current designation for the admin UI.

    Args:
        db: Open session; not committed here.

    Returns:
        The designation and whether it resolves to a usable provider.
    """
    env_smtp = bool(settings.SMTP_HOST)
    current = designated_uuid(db)
    if not current:
        return DesignationStatus(
            config_uuid=None,
            config_name=None,
            provider=None,
            is_enabled=None,
            resolves=False,
            status=STATUS_NOT_DESIGNATED,
            env_smtp_configured=env_smtp,
        )

    config = _find(db, current)
    if config is None:
        return DesignationStatus(
            config_uuid=current,
            config_name=None,
            provider=None,
            is_enabled=None,
            resolves=False,
            status=STATUS_MISSING,
            env_smtp_configured=env_smtp,
        )

    enabled = bool(config.is_enabled)
    return DesignationStatus(
        config_uuid=str(config.uuid),
        config_name=str(config.name),
        provider=str(config.provider),
        is_enabled=enabled,
        resolves=enabled,
        status=STATUS_ACTIVE if enabled else STATUS_DISABLED,
        env_smtp_configured=env_smtp,
    )


def set_designation(db: Session, config_uuid: str | None) -> DesignationStatus:
    """Designate (or clear) the config that carries auth mail.

    Args:
        db: Open session; committed by ``set_setting``.
        config_uuid: UUID of an existing, enabled config. Empty or ``None``
            clears the designation and falls back to the env SMTP transport.

    Returns:
        The resulting designation, which always resolves when a UUID was given.

    Raises:
        ValueError: The value is not a UUID, names no config, or names a
            disabled one. The caller turns this into a 400 — the message is
            written for the admin who typed it.
    """
    value = (config_uuid or "").strip()
    if not value:
        set_setting(db, AUTH_EMAIL_CONFIG_SETTING_KEY, "", description=SETTING_DESCRIPTION)
        logger.info("Auth mail designation cleared; auth mail falls back to env SMTP.")
        return describe_designation(db)

    try:
        parsed = uuid_pkg.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a valid email configuration UUID") from exc

    config = _find(db, str(parsed))
    if config is None:
        raise ValueError(
            f"No email configuration with UUID {parsed} exists. Create the "
            "configuration first, then designate it."
        )
    if not config.is_enabled:
        raise ValueError(
            f"Email configuration {config.name!r} is disabled. Enable it before "
            "designating it to carry authentication email."
        )

    set_setting(
        db, AUTH_EMAIL_CONFIG_SETTING_KEY, str(config.uuid), description=SETTING_DESCRIPTION
    )
    logger.info("Auth mail is now delivered through email config %r", config.name)
    return describe_designation(db)
