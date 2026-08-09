"""Which email configuration carries transactional auth mail (super_admin).

Mounted under ``/api/admin/auth-config`` rather than beside the
``EmailNotificationConfig`` CRUD in ``watch_sources.py``, for two reasons:

* **It is an authentication decision, not a watch-source one.** The designated
  row carries password resets, invitations and verification links — credentials.
  The provider rows happen to be managed from the watch-sources panel because
  that is where they were introduced; which one speaks for authentication
  belongs with the rest of the deployment's auth configuration.
* **Capability gating.** ``/api/watch-sources`` is mounted with
  ``capability="watch_sources"`` and 404s wholesale when an edition disables
  auto-import. Auth mail must stay configurable in that deployment, so this
  router rides ``auth.config_ui`` with the other auth-config surfaces.

The path is two segments (``/email/designation``) on purpose: ``auth_config.py``
owns ``GET``/``PUT`` on ``/{category}``, and a single-segment sibling would be
shadowed — or worse, silently rejected by its category allow-list — depending on
router include order.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.core.constants import AUTH_EMAIL_CONFIG_SETTING_KEY
from app.db.base import get_db
from app.models.user import User
from app.schemas.email_notification import AuthMailDesignationResponse
from app.schemas.email_notification import AuthMailDesignationUpdate
from app.services import auth_mail_config_service as designation

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/email/designation", response_model=AuthMailDesignationResponse)
def get_auth_mail_designation(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> AuthMailDesignationResponse:
    """Report the config designated to carry auth mail, and whether it resolves.

    Returns:
        The designation with its resolution status, so the UI can warn about a
        designation left dangling by a later delete or disable.
    """
    return AuthMailDesignationResponse(**asdict(designation.describe_designation(db)))


@router.put("/email/designation", response_model=AuthMailDesignationResponse)
def update_auth_mail_designation(
    request: Request,
    body: AuthMailDesignationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> AuthMailDesignationResponse:
    """Designate — or clear — the config that carries auth mail.

    Args:
        request: Used for the audited source IP / user agent.
        body: ``config_uuid`` of an existing, enabled config; empty clears it.

    Returns:
        The resulting designation.

    Raises:
        HTTPException: 400 when the UUID is malformed, names no config, or names
            a disabled one. The read path degrades to env SMTP quietly enough
            that a broken designation would otherwise only show up as
            undelivered password resets.
    """
    previous = designation.describe_designation(db)
    try:
        current = designation.set_designation(db, body.config_uuid)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    client_ip, user_agent = _get_client_info(request)
    audit_logger.log_admin_action(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        admin_user_id=int(current_user.id),
        admin_username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "action": "auth_mail_designation",
            "setting": AUTH_EMAIL_CONFIG_SETTING_KEY,
            "old_value": previous.config_uuid,
            "new_value": current.config_uuid,
        },
    )
    return AuthMailDesignationResponse(**asdict(current))
