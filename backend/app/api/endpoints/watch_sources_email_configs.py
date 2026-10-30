"""Email-notification configs for watch-source scan reports (super_admin).

The configs are deployment-wide and carry SMTP / M365 / Exchange credentials, which are
AES-256-GCM encrypted on write and never returned. Static ``/email-configs`` paths, registered
on the parent router before ``/{source_uuid}``. Linking a config to one source is
``watch_sources_email_links.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

# Deployment configuration is the super_admin tier: this router
# holds the SMTP / M365 / Exchange credentials for scan notifications.
from app.api.endpoints.auth import get_current_active_superuser
from app.db.base import get_db
from app.models.email_notification_config import EmailNotificationConfig
from app.models.user import User
from app.schemas.email_notification import EmailConfigCreate
from app.schemas.email_notification import EmailConfigResponse
from app.schemas.email_notification import EmailConfigsList
from app.schemas.email_notification import EmailConfigUpdate
from app.schemas.email_notification import EmailTestResponse
from app.services.auth_mail_config_service import IN_USE_MESSAGE
from app.services.auth_mail_config_service import is_designated
from app.utils.encryption import encrypt_api_key

router = APIRouter()


# --------------------------------------------------------------------------- #
# Email notification configs (admin)
# --------------------------------------------------------------------------- #
def _email_to_response(cfg: EmailNotificationConfig) -> dict:
    """Build an EmailConfigResponse-compatible dict (never includes secrets)."""
    return {
        "uuid": str(cfg.uuid),
        "name": cfg.name,
        "provider": cfg.provider,
        "is_enabled": cfg.is_enabled,
        "from_address": cfg.from_address,
        "default_recipients": cfg.default_recipients,
        "smtp_host": cfg.smtp_host,
        "smtp_port": cfg.smtp_port,
        "smtp_use_tls": cfg.smtp_use_tls,
        "smtp_username": cfg.smtp_username,
        "has_smtp_password": cfg.has_smtp_password,
        "m365_tenant_id": cfg.m365_tenant_id,
        "m365_client_id": cfg.m365_client_id,
        "has_m365_secret": cfg.has_m365_secret,
        "exchange_server": cfg.exchange_server,
        "exchange_domain": cfg.exchange_domain,
        "exchange_username": cfg.exchange_username,
        "has_exchange_password": cfg.has_exchange_password,
        "last_tested_at": cfg.last_tested_at,
        "test_status": cfg.test_status,
        "test_message": cfg.test_message,
        "created_at": cfg.created_at,
        "updated_at": cfg.updated_at,
        # Deleting a config cascades its links away, silently un-notifying every
        # source that used it. Surfacing the count is what makes that consequence
        # visible at the moment of the decision rather than after the fact.
        #
        # ``or []`` because this helper serialises whatever it is handed. A real ORM
        # row always has a collection here, but its callers include test stands-in
        # whose unset columns are ``None`` — and a response builder that raises on an
        # empty column is a worse contract than one that reports zero.
        "linked_source_count": len(cfg.links or []),
    }


@router.get("/email-configs", response_model=EmailConfigsList)
def list_email_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """List every email-notification config, name-ordered (super_admin only).

    Consumed by the admin Settings UI and by ops scripts auditing which mailers a
    deployment has. The configs are deployment-wide, not per-user, which is why
    this is the super_admin tier and why there is no owner filter. Secrets are
    never returned — ``_email_to_response`` emits ``has_*`` booleans instead.
    """
    # ``selectinload`` because ``_email_to_response`` counts ``cfg.links``: without it
    # that is one extra query per config on a list endpoint.
    configs = (
        db.query(EmailNotificationConfig)
        .options(selectinload(EmailNotificationConfig.links))
        .order_by(EmailNotificationConfig.name)
        .all()
    )
    return {"configs": [_email_to_response(c) for c in configs]}


@router.post("/email-configs", response_model=EmailConfigResponse)
def create_email_config(
    data: EmailConfigCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """Create an email-notification config for watch-source scan reports.

    Consumed by the admin Settings UI; also a scriptable way to provision a mailer
    on a fresh deployment. super_admin only — the payload carries SMTP / M365 /
    Exchange credentials.

    All three secret fields are AES-256-GCM encrypted on write and are absent from
    the response. Nothing here validates the credentials; call
    ``POST /email-configs/{uuid}/test`` for that.
    """
    cfg = EmailNotificationConfig(
        uuid=uuid_pkg.uuid4(),
        name=data.name,
        provider=data.provider.value,
        is_enabled=data.is_enabled,
        from_address=data.from_address,
        default_recipients=data.default_recipients,
        smtp_host=data.smtp_host,
        smtp_port=data.smtp_port,
        smtp_use_tls=data.smtp_use_tls,
        smtp_username=data.smtp_username,
        encrypted_smtp_password=encrypt_api_key(data.smtp_password) if data.smtp_password else None,
        m365_tenant_id=data.m365_tenant_id,
        m365_client_id=data.m365_client_id,
        encrypted_m365_client_secret=(
            encrypt_api_key(data.m365_client_secret) if data.m365_client_secret else None
        ),
        exchange_server=data.exchange_server,
        exchange_domain=data.exchange_domain,
        exchange_username=data.exchange_username,
        encrypted_exchange_password=(
            encrypt_api_key(data.exchange_password) if data.exchange_password else None
        ),
        created_by=current_user.id,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return _email_to_response(cfg)


@router.put("/email-configs/{config_uuid}", response_model=EmailConfigResponse)
def update_email_config(
    config_uuid: str,
    data: EmailConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """Patch an email-notification config (super_admin only).

    Consumed by the admin Settings UI. ``exclude_unset`` makes this a true partial
    update, and a secret field sent empty or null is *skipped* rather than cleared —
    so the UI can round-trip a form it never received the secret for.

    Refuses with 409 when it would disable the config designated as the auth mailer;
    see the inline comment for why silently breaking password resets is worse.
    """
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    payload = data.model_dump(exclude_unset=True)
    # Disabling the designated auth mailer silently routes password resets to the
    # env SMTP transport, which is unset in every stock deployment — so it stops
    # them altogether, visible only as an ERROR log. Refuse and name the remedy.
    if payload.get("is_enabled") is False and is_designated(db, str(cfg.uuid)):
        raise HTTPException(status_code=409, detail=IN_USE_MESSAGE)
    secret_map = {
        "smtp_password": "encrypted_smtp_password",
        "m365_client_secret": "encrypted_m365_client_secret",
        "exchange_password": "encrypted_exchange_password",
    }
    for key, value in payload.items():
        if key in secret_map:
            if value:
                setattr(cfg, secret_map[key], encrypt_api_key(value))
            continue
        setattr(cfg, key, value)
    db.commit()
    db.refresh(cfg)
    return _email_to_response(cfg)


@router.delete("/email-configs/{config_uuid}")
def delete_email_config(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """Delete an email-notification config (super_admin only).

    Consumed by the admin Settings UI. Refuses with 409 for the designated auth
    mailer — same reasoning as the disable guard in ``update_email_config``, plus
    the encrypted credentials go with the row and cannot be recovered. Any
    watch-source links to this config are removed by cascade.
    """
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    # Same reasoning as the disable guard, plus the row itself is unrecoverable.
    if is_designated(db, str(cfg.uuid)):
        raise HTTPException(status_code=409, detail=IN_USE_MESSAGE)
    db.delete(cfg)
    db.commit()
    return {"success": True}


@router.post("/email-configs/{config_uuid}/test", response_model=EmailTestResponse)
def test_email_config(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> EmailTestResponse:
    """Send a live test message through one email config (super_admin only).

    Consumed by the admin Settings UI's "Test" button and by ops verifying a mailer
    after a credential rotation. Talks to the real SMTP / Graph / Exchange endpoint,
    so it is slow and has an external side effect.

    The outcome is persisted onto the config (``last_tested_at``, ``test_status``,
    ``test_message``) so the UI can show the last known state without re-testing.
    A failure is reported as ``success=false`` in a 200 body, not an error status —
    a failed connection is a successful *test*.
    """
    from app.services import watch_email_service

    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    ok, message = watch_email_service.test_connection(cfg)
    cfg.last_tested_at = datetime.now(UTC)
    cfg.test_status = "success" if ok else "failed"
    cfg.test_message = message
    db.commit()
    return EmailTestResponse(success=ok, message=message)
