"""The opt-in anonymous usage report (issue #1019) - super_admin only.

- ``GET /admin/telemetry`` - the choice, what forces it off, the last send and the exact
  request body that would be sent (built by the sender's own function).
- ``PUT /admin/telemetry`` - ``{"enabled": bool}``. Turning it on is refused with 409 and the
  reason codes while anything forces it off (``DO_NOT_TRACK``, ``OPENTRANSCRIBE_TELEMETRY``,
  demo mode, no receiver configured). Turning it off deletes the install id.

Logic lives in ``services/telemetry.py``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.api.endpoints.auth.lifecycle_gates import _get_client_info
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.db.base import get_db
from app.schemas.telemetry import TelemetryStatus
from app.schemas.telemetry import TelemetryUpdate
from app.services import telemetry

logger = logging.getLogger(__name__)
router = APIRouter()


def _status(db: Session) -> TelemetryStatus:
    state = telemetry.read_state(db)
    reasons = telemetry.off_reasons()
    install_id = state["install_id"] or telemetry.PREVIEW_INSTALL_ID
    return TelemetryStatus(
        enabled=state["enabled"],
        sending=state["enabled"] and not reasons,
        off_reasons=reasons,
        last_sent_at=state["last_sent_at"],
        endpoint=telemetry.receiver_endpoint(),
        preview=telemetry.build_request(db, install_id),
    )


@router.get("", response_model=TelemetryStatus)
def get_telemetry_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> TelemetryStatus:
    """The usage report's state and the exact request it would send."""
    return _status(db)


@router.put("", response_model=TelemetryStatus)
def set_telemetry_enabled(
    request: Request,
    body: TelemetryUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> TelemetryStatus:
    """Turn the usage report on or off."""
    try:
        telemetry.set_enabled(db, body.enabled)
    except telemetry.TelemetryRefusedError:
        # The reasons are re-read from the switches, never taken off the exception, so nothing a
        # raise carries can reach the response.
        raise HTTPException(
            status_code=409,
            detail={"code": "telemetry_refused", "reasons": telemetry.off_reasons()},
        ) from None
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"setting": "telemetry.enabled", "value": body.enabled},
    )
    logger.info(
        "Super admin %s turned the usage report %s",
        current_user.id,
        "on" if body.enabled else "off",
    )
    return _status(db)
