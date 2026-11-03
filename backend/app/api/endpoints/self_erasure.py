"""Self-service account erasure (issue #1354).

* ``router`` (``/users/me/erasure-request``, any signed-in user): read the status, request erasure
  of the caller's own account with a grace period, cancel until ``scheduled_for``.
* ``admin_router`` (``/admin/self-erasure``, super_admin): the two instance settings.

This is the SUBJECT's own action, which is why it has a UI while operator erasure deliberately has
none. The feature is OFF by default; while it is off POST and DELETE answer 404 so it is not
advertised. Contract and error codes: ``app.schemas.self_erasure``.
"""

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app import models
from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import GDPR_SELF_ERASURE_ENABLED_KEY
from app.core.constants import GDPR_SELF_ERASURE_GRACE_DAYS_KEY
from app.core.constants import MAX_GDPR_SELF_ERASURE_GRACE_DAYS
from app.db.base import get_db
from app.models.erasure import ErasureLedgerEntry
from app.schemas.self_erasure import SelfErasureCreate
from app.schemas.self_erasure import SelfErasureOutcome
from app.schemas.self_erasure import SelfErasureRequestReceipt
from app.schemas.self_erasure import SelfErasureSettings
from app.schemas.self_erasure import SelfErasureSettingsUpdate
from app.schemas.self_erasure import SelfErasureStatus
from app.services import self_erasure_service as service
from app.services.self_erasure_reauth import can_reauthenticate
from app.services.self_erasure_reauth import verify_reauth
from app.services.support_access_service import coded_error
from app.services.system_settings_service import set_setting

router = APIRouter()
admin_router = APIRouter()


def _receipt(entry: ErasureLedgerEntry) -> SelfErasureRequestReceipt:
    return SelfErasureRequestReceipt(
        ledger_uuid=entry.uuid,
        requested_at=entry.requested_at,
        scheduled_for=entry.scheduled_for,
        sla_due_at=entry.sla_due_at,
        request_digest=str(entry.request_digest),
        status="cancelled" if entry.status == "cancelled" else "scheduled",
        cancelled_at=entry.cancelled_at,
    )


def _raise(error: service.SelfErasureError) -> NoReturn:
    if error.code in ("disabled", "not_found"):
        raise coded_error(status.HTTP_404_NOT_FOUND, "not_found", "Not found.")
    http = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if error.code == "ledger_unavailable"
        else status.HTTP_409_CONFLICT
    )
    raise coded_error(http, error.code, error.message)


@router.get("", response_model=SelfErasureStatus)
def get_self_erasure_status(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> SelfErasureStatus:
    """Whether the feature is on, the grace period, and the caller's open request (if any)."""
    open_request = service.get_open_request(db, int(ctx.user.id))
    blocked = (
        service.get_last_blocked_request(db, int(ctx.user.id)) if open_request is None else None
    )
    return SelfErasureStatus(
        enabled=service.is_enabled(db),
        grace_days=service.grace_days(db),
        can_reauthenticate=can_reauthenticate(db, ctx.user),
        request=_receipt(open_request) if open_request is not None else None,
        last_outcome=(
            SelfErasureOutcome(
                status="blocked",
                reason="last_super_admin",
                ledger_uuid=blocked.uuid,
                at=blocked.scheduled_for,
            )
            if blocked is not None
            else None
        ),
    )


@router.post("", response_model=SelfErasureRequestReceipt, status_code=status.HTTP_201_CREATED)
@limiter.limit(get_auth_rate_limit(), key_func=user_or_ip_key)
def request_erasure(
    request: Request,
    body: SelfErasureCreate,
    response: Response,  # required by slowapi
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> SelfErasureRequestReceipt:
    """Ask for the caller's own account to be erased after the grace period."""
    user = ctx.user
    if not service.is_enabled(db):
        raise coded_error(status.HTTP_404_NOT_FOUND, "not_found", "Not found.")
    if not can_reauthenticate(db, user):
        raise coded_error(
            status.HTTP_409_CONFLICT,
            "reauth_unavailable",
            "This account cannot confirm a request here. Ask the operator of this deployment.",
        )
    password = body.password.get_secret_value() if body.password else None
    if not verify_reauth(db, user, password=password, mfa_code=body.mfa_code):
        raise coded_error(
            status.HTTP_403_FORBIDDEN, "reauth_required", "The password or code was not accepted."
        )
    try:
        entry = service.request_self_erasure(db, user)
    except service.SelfErasureError as error:
        _raise(error)
    source_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_DELETE,
        outcome=AuditOutcome.SUCCESS,
        user_id=int(user.id),
        target_user_id=int(user.id),
        source_ip=source_ip,
        user_agent=user_agent,
        details={"action": "gdpr_self_erasure_requested", "ledger_uuid": str(entry.uuid)},
    )
    return _receipt(entry)


@router.delete("/{ledger_uuid}", response_model=SelfErasureRequestReceipt)
def cancel_erasure(
    ledger_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> SelfErasureRequestReceipt:
    """Withdraw the caller's own request. Another user's uuid is a 404."""
    if not service.is_enabled(db):
        raise coded_error(status.HTTP_404_NOT_FOUND, "not_found", "Not found.")
    try:
        entry = service.cancel_self_erasure(db, ctx.user, ledger_uuid)
    except service.SelfErasureError as error:
        _raise(error)
    return _receipt(entry)


def _read_settings(db: Session) -> SelfErasureSettings:
    return SelfErasureSettings(
        enabled=service.is_enabled(db),
        grace_days=service.grace_days(db),
        max_grace_days=MAX_GDPR_SELF_ERASURE_GRACE_DAYS,
    )


@admin_router.get("", response_model=SelfErasureSettings)
def get_self_erasure_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> SelfErasureSettings:
    """The self-service erasure switch and grace period (super_admin)."""
    return _read_settings(db)


@admin_router.put("", response_model=SelfErasureSettings)
def put_self_erasure_settings(
    body: SelfErasureSettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> SelfErasureSettings:
    """Partial update (super_admin). Audited as admin.settings.change; keys and values only."""
    changes: list[dict[str, object]] = []
    if body.enabled is not None and body.enabled != service.is_enabled(db):
        set_setting(db, GDPR_SELF_ERASURE_ENABLED_KEY, body.enabled)
        changes.append({"setting": GDPR_SELF_ERASURE_ENABLED_KEY, "value": body.enabled})
    if body.grace_days is not None and body.grace_days != service.grace_days(db):
        set_setting(db, GDPR_SELF_ERASURE_GRACE_DAYS_KEY, body.grace_days)
        changes.append({"setting": GDPR_SELF_ERASURE_GRACE_DAYS_KEY, "value": body.grace_days})
    source_ip, user_agent = _get_client_info(request)
    for change in changes:
        audit_logger.log(
            event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=source_ip,
            user_agent=user_agent,
            details=change,
        )
    return _read_settings(db)
