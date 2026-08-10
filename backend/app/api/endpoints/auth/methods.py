"""Auth-method discovery and login-banner endpoints."""

from datetime import UTC

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.core.auth_settings import get_auth_settings
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import AuthMethodsResponse
from app.schemas.user import LoginBannerResponse

router = APIRouter()


@router.get("/methods", response_model=AuthMethodsResponse)
@limiter.limit(get_auth_rate_limit())
def get_auth_methods(request: Request, response: Response, db: Session = Depends(get_db)):
    """
    Get available authentication methods.

    Returns a list of enabled authentication methods that the frontend
    can use to display appropriate login options. Checks database settings
    first (set via admin UI), falls back to environment variables.

    Rate-limited: it is unauthenticated and reports the deployment's entire auth
    topology, so it should not be a free reconnaissance oracle.
    """
    # Use dynamic settings to check database first, then fall back to .env
    auth_settings = get_auth_settings(db)

    ldap_enabled = auth_settings.get_bool("ldap_enabled", settings.LDAP_ENABLED)
    oidc_enabled = auth_settings.get_bool("oidc_enabled", settings.OIDC_ENABLED)
    pki_enabled = auth_settings.pki_enabled or settings.PKI_ENABLED
    proxy_enabled = auth_settings.proxy_enabled
    local_enabled = auth_settings.local_enabled
    allow_registration = auth_settings.allow_registration
    mfa_enabled = auth_settings.mfa_enabled or settings.MFA_ENABLED
    mfa_required = auth_settings.get_bool("mfa_required", settings.MFA_REQUIRED)
    login_banner_enabled = auth_settings.get_bool(
        "login_banner_enabled", settings.LOGIN_BANNER_ENABLED
    )

    # "local" is no longer unconditional. It was hardcoded as
    # `methods = ["local"]  # Always available`, which is why a deployment whose
    # identity lives entirely in an external IdP still advertised — and accepted —
    # local password login.
    methods = []
    if local_enabled:
        methods.append("local")
    if ldap_enabled:
        methods.append("ldap")
    if oidc_enabled:
        methods.append("oidc")
    if pki_enabled:
        methods.append("pki")
    # Advertised so the login page can offer "Continue with single sign-on", which
    # POSTs to /auth/proxy/authenticate. The headers are already on the request by
    # then — the proxy put them there — so there is nothing for the client to send.
    if proxy_enabled:
        methods.append("proxy")

    # Registry-based external providers (the cloud edition registers managed
    # IdPs; community registers none). Presence in the registry == enabled.
    from app.auth.provider_registry import get_registered_providers

    external_providers = get_registered_providers()
    methods.extend(p for p in external_providers if p not in methods)

    return AuthMethodsResponse(
        methods=methods,
        oidc_enabled=oidc_enabled,
        pki_enabled=pki_enabled,
        proxy_enabled=proxy_enabled,
        ldap_enabled=ldap_enabled,
        local_enabled=local_enabled,
        allow_registration=allow_registration,
        # Registered external IdP providers, reported generically. Clients gate
        # on membership in this list (e.g. `external_providers.length > 0`),
        # never on a hardcoded per-vendor flag.
        external_providers=external_providers,
        mfa_enabled=mfa_enabled,
        mfa_required=mfa_required,
        login_banner_enabled=login_banner_enabled,
        # Read the banner copy from the same place as the flag. Reading the flag
        # from the DB but the text from the environment meant enabling the banner
        # in the admin UI produced an EMPTY banner — the inverse of the AC-8
        # control it implements.
        login_banner_text=auth_settings.login_banner_text if login_banner_enabled else "",
        login_banner_classification=(
            auth_settings.login_banner_classification if login_banner_enabled else "UNCLASSIFIED"
        ),
    )


@router.get("/banner", response_model=LoginBannerResponse)
def get_login_banner(db: Session = Depends(get_db)):
    """
    PUBLIC endpoint - returns banner text without authentication.
    Called before login to display classification banner.
    FedRAMP AC-8 compliance.

    Reads the DB-backed settings, not the environment: this is the endpoint the
    SPA actually calls for the banner, and it ignored every value a super_admin
    set in the admin UI.
    """
    auth_settings = get_auth_settings(db)
    if not auth_settings.get_bool("login_banner_enabled", settings.LOGIN_BANNER_ENABLED):
        return LoginBannerResponse(
            enabled=False,
            text="",
            classification="",
            requires_acknowledgment=False,
        )

    return LoginBannerResponse(
        enabled=True,
        text=auth_settings.login_banner_text,
        classification=auth_settings.login_banner_classification,
        requires_acknowledgment=True,
    )


@router.post("/banner/acknowledge")
@limiter.limit(get_auth_rate_limit())
def acknowledge_banner(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Record banner acknowledgment for the current user.

    Must be called after login before granting full access — and since v375 that
    is enforced rather than merely documented: ``get_current_active_user`` refuses
    every non-exempt route with ``detail.code == "banner_acknowledgment_required"``
    while the banner is enabled and this timestamp is missing or predates the last
    edit of the banner text. This endpoint is one of the routes exempt from that
    gate, or it could never be reached to clear it.

    FedRAMP AC-8 compliance.
    """
    from datetime import datetime

    # A bare "now" is all the gate needs: it compares this against the
    # login_banner_text row's updated_at, so acknowledging always lands after the
    # wording currently on screen.
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
