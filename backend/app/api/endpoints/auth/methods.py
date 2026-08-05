"""Auth-method discovery and login-banner endpoints."""

from datetime import UTC

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.auth_settings import get_auth_settings
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import LoginBannerResponse

router = APIRouter()


@router.get("/methods")
def get_auth_methods(db: Session = Depends(get_db)):
    """
    Get available authentication methods.

    Returns a list of enabled authentication methods that the frontend
    can use to display appropriate login options. Checks database settings
    first (set via admin UI), falls back to environment variables.
    """
    # Use dynamic settings to check database first, then fall back to .env
    auth_settings = get_auth_settings(db)

    ldap_enabled = auth_settings.get_bool("ldap_enabled", settings.LDAP_ENABLED)
    keycloak_enabled = auth_settings.get_bool("keycloak_enabled", settings.KEYCLOAK_ENABLED)
    pki_enabled = auth_settings.pki_enabled or settings.PKI_ENABLED
    mfa_enabled = auth_settings.mfa_enabled or settings.MFA_ENABLED
    mfa_required = auth_settings.get_bool("mfa_required", settings.MFA_REQUIRED)
    login_banner_enabled = auth_settings.get_bool(
        "login_banner_enabled", settings.LOGIN_BANNER_ENABLED
    )

    methods = ["local"]  # Always available

    if ldap_enabled:
        methods.append("ldap")
    if keycloak_enabled:
        methods.append("keycloak")
    if pki_enabled:
        methods.append("pki")

    # Registry-based external providers (the cloud edition registers managed
    # IdPs; community registers none). Presence in the registry == enabled.
    from app.auth.provider_registry import get_registered_providers

    external_providers = get_registered_providers()
    methods.extend(p for p in external_providers if p not in methods)

    return {
        "methods": methods,
        "keycloak_enabled": keycloak_enabled,
        "pki_enabled": pki_enabled,
        "ldap_enabled": ldap_enabled,
        # Registered external IdP providers, reported generically. Clients gate
        # on membership in this list (e.g. `external_providers.length > 0`),
        # never on a hardcoded per-vendor flag.
        "external_providers": external_providers,
        "mfa_enabled": mfa_enabled,
        "mfa_required": mfa_required,
        "login_banner_enabled": login_banner_enabled,
        "login_banner_text": settings.LOGIN_BANNER_TEXT if login_banner_enabled else "",
        "login_banner_classification": settings.LOGIN_BANNER_CLASSIFICATION
        if login_banner_enabled
        else "UNCLASSIFIED",
    }


@router.get("/banner", response_model=LoginBannerResponse)
def get_login_banner():
    """
    PUBLIC endpoint - returns banner text without authentication.
    Called before login to display classification banner.
    FedRAMP AC-8 compliance.
    """
    if not settings.LOGIN_BANNER_ENABLED:
        return LoginBannerResponse(
            enabled=False,
            text="",
            classification="",
            requires_acknowledgment=False,
        )

    return LoginBannerResponse(
        enabled=True,
        text=settings.LOGIN_BANNER_TEXT,
        classification=settings.LOGIN_BANNER_CLASSIFICATION,
        requires_acknowledgment=True,
    )


@router.post("/banner/acknowledge")
def acknowledge_banner(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Record banner acknowledgment for the current user.
    Must be called after login before granting full access.
    FedRAMP AC-8 compliance.
    """
    from datetime import datetime

    current_user.banner_acknowledged_at = datetime.now(UTC)  # type: ignore[assignment]
    db.commit()

    # Audit log
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.AUTH_BANNER_ACKNOWLEDGED,
        user_id=current_user.id,
        username=str(current_user.email),
        outcome=AuditOutcome.SUCCESS,
        source_ip=client_ip,
        user_agent=user_agent,
    )

    return {"acknowledged": True}
