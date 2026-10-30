"""Which email configs one watch source notifies: list, discover, link (upsert), unlink.

Owner-or-admin tier through the tenant gate in ``watch_sources_access.py``, deliberately not
the super_admin tier of the configs themselves (``watch_sources_email_configs.py``).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.watch_sources_access import _get_source_or_404
from app.db.base import get_db
from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail
from app.models.user import User
from app.schemas.watch_source import EmailConfigOption
from app.schemas.watch_source import EmailLinkCreate
from app.schemas.watch_source import EmailLinkResponse
from app.services.request_context import RequestContext

router = APIRouter()


# --------------------------------------------------------------------------- #
# Email links
# --------------------------------------------------------------------------- #
@router.get("/{source_uuid}/emails", response_model=list[EmailLinkResponse])
def list_email_links(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[EmailLinkResponse]:
    """The email configs this source notifies, with each link's own options.

    Consumed by the settings page's per-source notification panel. Owner or admin —
    the same tier as the link/unlink routes below, deliberately NOT the super_admin
    tier that governs the configs themselves: subscribing your own source to an
    existing mailer is not the same right as holding its credentials.

    ``notify_on_success``/``notify_on_error`` are evaluated **per scan**, not per
    file: ``send_notification`` classifies a whole scan by whether it recorded any
    error, so ``notify_on_error`` means "a scan in which at least one file failed".
    """
    source = _get_source_or_404(db, source_uuid, ctx, need="read")
    links = (
        db.query(WatchSourceEmail)
        .options(selectinload(WatchSourceEmail.email_config))
        .filter(WatchSourceEmail.watch_source_id == source.id)
        .all()
    )
    return [
        EmailLinkResponse(
            email_config_uuid=str(link.email_config.uuid),
            email_config_name=link.email_config.name,
            email_config_provider=link.email_config.provider,
            config_is_enabled=link.email_config.is_enabled,
            config_has_default_recipients=bool(
                link.email_config.default_recipients
                and link.email_config.default_recipients.strip()
            ),
            additional_recipients=link.additional_recipients,
            notify_on_success=link.notify_on_success,
            notify_on_error=link.notify_on_error,
        )
        for link in links
        if link.email_config is not None
    ]


@router.get("/{source_uuid}/emails/available", response_model=list[EmailConfigOption])
def list_available_email_configs(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[EmailConfigOption]:
    """Configs this source could still be linked to (owner or admin).

    Consumed by the notification panel's picker. It exists because of an asymmetry
    that otherwise makes that panel unbuildable: any source owner may link a config,
    but ``GET /email-configs`` is **super_admin** — so an ordinary owner had the right
    to subscribe and no way to discover what to subscribe to.

    Deliberately source-scoped rather than a second listing under ``/email-configs``:
    that prefix is in ``SUPER_ADMIN_PREFIXES``
    (``tests/unit/test_route_privilege_tiers.py``), and a non-super_admin route beneath
    it would fail that gate — correctly, since the gate exists to stop exactly this
    kind of quiet tier drift. Scoping it here also lets the server exclude what is
    already linked instead of making the client subtract two lists.

    The projection is minimal on purpose — see ``EmailConfigOption``. Do not widen it
    to ``EmailConfigResponse``: that carries the deployment's mail hostnames and
    usernames, and every authenticated user can read this.
    """
    source = _get_source_or_404(db, source_uuid, ctx, need="read")
    linked_ids = {
        link.email_config_id
        for link in db.query(WatchSourceEmail)
        .filter(WatchSourceEmail.watch_source_id == source.id)
        .all()
    }
    configs = db.query(EmailNotificationConfig).order_by(EmailNotificationConfig.name).all()
    return [
        EmailConfigOption(
            uuid=str(cfg.uuid),
            name=cfg.name,
            provider=cfg.provider,
            is_enabled=cfg.is_enabled,
            has_default_recipients=bool(cfg.default_recipients and cfg.default_recipients.strip()),
        )
        for cfg in configs
        if cfg.id not in linked_ids
    ]


@router.post("/{source_uuid}/emails")
def link_email_config(
    source_uuid: str,
    data: EmailLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict:
    """Attach an email config to a watch source, or update the existing link.

    Consumed by the settings page's notification panel. Authorized as the *source*
    owner (or an admin) — note the asymmetry: creating and editing the email configs
    themselves is super_admin, but any source owner may subscribe their own source to
    one that already exists, and there is no separate gate on which config they pick.

    Upsert, not insert: re-posting the same ``email_config_uuid`` overwrites the
    recipients and the notify-on-success/error flags rather than 409-ing, so it is
    safe to re-run.
    """
    source = _get_source_or_404(db, source_uuid, ctx)
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == data.email_config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    existing = (
        db.query(WatchSourceEmail)
        .filter(
            WatchSourceEmail.watch_source_id == source.id,
            WatchSourceEmail.email_config_id == cfg.id,
        )
        .first()
    )
    if existing:
        existing.additional_recipients = data.additional_recipients
        existing.notify_on_success = data.notify_on_success
        existing.notify_on_error = data.notify_on_error
    else:
        db.add(
            WatchSourceEmail(
                watch_source_id=source.id,
                email_config_id=cfg.id,
                additional_recipients=data.additional_recipients,
                notify_on_success=data.notify_on_success,
                notify_on_error=data.notify_on_error,
            )
        )
    db.commit()
    return {"success": True}


@router.delete("/{source_uuid}/emails/{config_uuid}")
def unlink_email_config(
    source_uuid: str,
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict:
    """Detach an email config from a watch source (source owner or admin).

    Consumed by the settings page's notification panel. Removes only the link row;
    the email config itself survives and stays available to other sources.

    Idempotent — a missing link still answers ``{"success": true}``, so a retry after
    a dropped response does not 404. An unknown *config* uuid does 404, because that
    is a caller mistake rather than an already-applied delete.
    """
    source = _get_source_or_404(db, source_uuid, ctx)
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    link = (
        db.query(WatchSourceEmail)
        .filter(
            WatchSourceEmail.watch_source_id == source.id,
            WatchSourceEmail.email_config_id == cfg.id,
        )
        .first()
    )
    if link:
        db.delete(link)
        db.commit()
    return {"success": True}
