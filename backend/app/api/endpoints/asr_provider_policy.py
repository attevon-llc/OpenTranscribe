"""Which cloud ASR providers users may choose - super_admin only (issue #1246, WP6).

Credentials are not stored here: an administrator creates an ordinary ASR configuration
(``POST /asr-settings``, key write-only) and shares it, and this router lists those shared
configurations per provider as flags so the settings page can show "credentials set".
The policy itself (``services/asr_provider_policy``) is deployment-wide; the credentials
listed are those of the caller's ACTIVE organisation, so in multi-tenant mode each
organisation keeps its own shared credentials.

- ``GET /admin/asr-providers`` - ``{providers[], users_may_add_own_keys}``
- ``PUT /admin/asr-providers`` - ``{allowed_for_users: [id] | null, users_may_add_own_keys}``
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from sqlalchemy.orm import Session

from app import models
from app.api.deps_context import RequestContext
from app.api.deps_context import get_base_context
from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ELEVATED_ROLES
from app.db.base import get_db
from app.models.user_asr_settings import UserASRSettings
from app.schemas.asr_provider_policy import ProviderPolicyEntry
from app.schemas.asr_provider_policy import ProviderPolicyResponse
from app.schemas.asr_provider_policy import ProviderPolicyUpdate
from app.schemas.asr_provider_policy import SharedCredential
from app.services import asr_provider_policy as policy_service
from app.utils.tenant_sharing import owner_in_tenant

logger = logging.getLogger(__name__)
router = APIRouter()


def _shared_by_provider(db: Session, org_id: int | None) -> dict[str, list[SharedCredential]]:
    """Admin-shared configs visible in the tenant, grouped by provider."""
    rows = (
        db.query(UserASRSettings, models.User)
        .join(models.User, models.User.id == UserASRSettings.user_id)
        .filter(
            UserASRSettings.is_shared.is_(True),
            models.User.role.in_(ELEVATED_ROLES),
            owner_in_tenant(UserASRSettings.user_id, org_id),
        )
        .order_by(UserASRSettings.created_at)
        .all()
    )
    grouped: dict[str, list[SharedCredential]] = {}
    for config, owner in rows:
        grouped.setdefault(str(config.provider), []).append(
            SharedCredential(
                config_uuid=str(config.uuid),
                name=str(config.name),
                has_api_key=bool(config.api_key),
                has_access_key_id=bool(config.access_key_id),
                owner_name=owner.full_name,
            )
        )
    return grouped


def _document(db: Session, org_id: int | None) -> ProviderPolicyResponse:
    from app.services.asr.factory import ASR_PROVIDER_CATALOG

    policy = policy_service.load_policy(db)
    shared = _shared_by_provider(db, org_id)
    entries = [
        ProviderPolicyEntry(
            id=pid,
            display_name=str(ASR_PROVIDER_CATALOG[pid].get("display_name") or pid),
            allowed_for_users=policy.allowed is None or pid in policy.allowed,
            shared_credentials=shared.get(pid, []),
        )
        for pid in policy_service.cloud_provider_ids()
    ]
    return ProviderPolicyResponse(providers=entries, users_may_add_own_keys=policy.own_keys)


@router.get("", response_model=ProviderPolicyResponse)
def get_provider_policy(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_base_context),
    current_user: models.User = Depends(get_current_active_superuser),
) -> ProviderPolicyResponse:
    """The allow-list, the own-keys switch and the shared credentials per provider."""
    return _document(db, ctx.org_id)


@router.put("", response_model=ProviderPolicyResponse)
def put_provider_policy(
    request: Request,
    body: ProviderPolicyUpdate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_base_context),
    current_user: models.User = Depends(get_current_active_superuser),
) -> ProviderPolicyResponse:
    """Replace the policy. Unknown ids (or ``local``) are a 422 and change nothing."""
    try:
        policy_service.save_policy(db, body.allowed_for_users, body.users_may_add_own_keys)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_asr_provider",
                "message": policy_service.UNKNOWN_PROVIDER_MESSAGE,
            },
        ) from None
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "setting": "asr_provider_policy",
            "action": "update",
            "allowed_for_users": body.allowed_for_users,
            "users_may_add_own_keys": body.users_may_add_own_keys,
        },
    )
    logger.info("Super admin %s updated the ASR provider policy", current_user.id)
    return _document(db, ctx.org_id)
