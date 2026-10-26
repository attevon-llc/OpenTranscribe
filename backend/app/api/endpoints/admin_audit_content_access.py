"""The super_admin switch for content-access auditing (issue #1277).

View, media-access and export events are high volume, so they are written only when this
setting AND the deployment's ``audit.content_access`` capability are on
(``services/content_audit.content_audit_enabled``). It defaults to OFF: a deployment opts in
here, with no environment variable and no restart. Governance events (collection membership
and visibility, legal holds) are recorded regardless.

- ``GET /admin/audit/content-access`` - the setting, whether it is in effect, and the storage estimate
- ``PUT /admin/audit/content-access`` - turn it on or off (audited as ``admin.settings.change``)
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.capabilities import capability_enabled
from app.core.constants import AUDIT_ACTIVE_USER_BYTES_PER_DAY_ESTIMATE
from app.core.constants import AUDIT_CONTENT_ACCESS_DEFAULT
from app.core.constants import AUDIT_CONTENT_ACCESS_SETTING_KEY
from app.core.constants import AUDIT_EVENT_BYTES_ESTIMATE
from app.core.constants import CONTENT_AUDIT_DEDUP_SECONDS
from app.db.base import get_db
from app.services.content_audit import CONTENT_ACCESS_CAPABILITY
from app.services.system_settings_service import get_setting_bool
from app.services.system_settings_service import set_setting

router = APIRouter()


class ContentAccessAuditStatus(BaseModel):
    """The switch, whether it is in effect, and what leaving it on costs."""

    enabled: bool = Field(description="The super_admin setting.")
    capability_available: bool = Field(
        description="False when this deployment's edition withholds content auditing."
    )
    active: bool = Field(description="enabled AND capability_available: events are being written.")
    dedup_seconds: int = Field(
        description="A repeat view or stream-URL event inside this window is not written again."
    )
    event_bytes_estimate: int
    bytes_per_active_user_per_day_estimate: int


class ContentAccessAuditUpdate(BaseModel):
    enabled: bool


def _status(db: Session, request: Request) -> ContentAccessAuditStatus:
    enabled = get_setting_bool(db, AUDIT_CONTENT_ACCESS_SETTING_KEY, AUDIT_CONTENT_ACCESS_DEFAULT)
    available = capability_enabled(CONTENT_ACCESS_CAPABILITY, request)
    return ContentAccessAuditStatus(
        enabled=enabled,
        capability_available=available,
        active=enabled and available,
        dedup_seconds=CONTENT_AUDIT_DEDUP_SECONDS,
        event_bytes_estimate=AUDIT_EVENT_BYTES_ESTIMATE,
        bytes_per_active_user_per_day_estimate=AUDIT_ACTIVE_USER_BYTES_PER_DAY_ESTIMATE,
    )


@router.get("", response_model=ContentAccessAuditStatus)
def get_content_access_audit(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> ContentAccessAuditStatus:
    """Whether view, media-access and export events are being recorded (super_admin only)."""
    return _status(db, request)


@router.put("", response_model=ContentAccessAuditStatus)
def set_content_access_audit(
    body: ContentAccessAuditUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> ContentAccessAuditStatus:
    """Turn content-access auditing on or off. Takes effect within the settings cache TTL."""
    previous = get_setting_bool(db, AUDIT_CONTENT_ACCESS_SETTING_KEY, AUDIT_CONTENT_ACCESS_DEFAULT)
    set_setting(
        db,
        AUDIT_CONTENT_ACCESS_SETTING_KEY,
        body.enabled,
        description="Record file view, media access and export events in the audit log",
    )
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "setting": AUDIT_CONTENT_ACCESS_SETTING_KEY,
            "from": previous,
            "to": body.enabled,
        },
    )
    return _status(db, request)
