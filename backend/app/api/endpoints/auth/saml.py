"""SAML 2.0 SP endpoints: metadata, SSO (SP-initiated), ACS, and SLS.

Unlike the OIDC flow, ACS and SLS are **browser POST/redirect targets the IdP talks
to directly** — there is no SPA JavaScript in that leg of the round trip, so these
handlers finish by issuing an HTTP redirect to the SPA with the session already
established via httpOnly cookies, not by returning JSON for a `fetch()` caller.
"""

import logging
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.login import record_successful_login
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.saml.assertion import extract_saml_user_data
from app.auth.saml.config import SAMLConfig
from app.auth.saml.provisioning import sync_saml_user_to_db
from app.auth.saml.sp import build_auth
from app.auth.saml.sp import saml_request_data
from app.auth.token_service import token_service
from app.core.config import settings
from app.db.base import get_db

router = APIRouter()

logger = logging.getLogger(__name__)

#: Audit ``auth_method`` for every event this module writes.
AUTH_METHOD = "saml"

#: Where the browser lands after a successful ACS — the SPA's root, same as every
#: other cookie-based login completes to. SAML has no equivalent of OIDC's
#: SPA-owned `/login?code=...` callback route to redirect through instead.
_POST_LOGIN_REDIRECT = "/"


def _require_enabled_config(db: Session) -> SAMLConfig:
    cfg = SAMLConfig.from_db(db)
    if not cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SAML authentication is not enabled",
        )
    return cfg


@router.get("/saml/metadata")
async def saml_metadata(request: Request, db: Session = Depends(get_db)):
    """Serve this SP's metadata XML, for the IdP administrator to import.

    Public and unauthenticated by necessity — an IdP admin fetches this once, out
    of band, to configure the SP side of the trust relationship. It carries no
    secret: entity id, ACS/SLS URLs, and (if configured) the SP's public signing
    certificate only, never the private key.
    """
    cfg = SAMLConfig.from_db(db)
    request_data = await saml_request_data(request)
    auth = build_auth(request_data, cfg)
    settings_obj = auth.get_settings()
    metadata = settings_obj.get_sp_metadata()
    errors = settings_obj.validate_metadata(metadata)
    if errors:
        logger.error("SAML SP metadata is invalid: %s", errors)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SAML SP metadata could not be generated",
        )
    return Response(content=metadata, media_type="application/xml")


@router.get("/saml/login")
@limiter.limit(get_auth_rate_limit())
async def saml_login(request: Request, response: Response, db: Session = Depends(get_db)):
    """Initiate SP-initiated SSO: redirect the browser to the IdP's SSO endpoint."""
    cfg = _require_enabled_config(db)
    request_data = await saml_request_data(request)
    auth = build_auth(request_data, cfg)
    redirect_url = auth.login(return_to=_POST_LOGIN_REDIRECT)
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.post("/saml/acs")
@limiter.limit(get_auth_rate_limit())
async def saml_acs(request: Request, response: Response, db: Session = Depends(get_db)):
    """Assertion Consumer Service: the IdP POSTs the SAMLResponse here.

    Signature verification, timing/audience/destination checks and (if configured)
    assertion decryption are all python3-saml's — this handler only reads the
    result. Runs whether the flow was SP- or IdP-initiated, since neither this SP
    nor the browser can distinguish the two on the wire.
    """
    client_ip, user_agent = _get_client_info(request)
    cfg = _require_enabled_config(db)
    request_data = await saml_request_data(request)
    auth = build_auth(request_data, cfg)

    auth.process_response()
    errors = auth.get_errors()
    if errors or not auth.is_authenticated():
        reason = auth.get_last_error_reason()
        logger.warning("SAML assertion rejected: %s (%s)", errors, reason)
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_ASSERTION",
            auth_method=AUTH_METHOD,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid SAML assertion",
        )

    saml_data = extract_saml_user_data(auth, cfg)
    if not saml_data["saml_subject"]:
        logger.warning("SAML assertion carried no NameID")
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="MISSING_NAMEID",
            auth_method=AUTH_METHOD,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid SAML assertion",
        )

    # sync_saml_user_to_db runs admission (allow/block groups) before creating or
    # linking any row — see its docstring for why that ordering matters.
    user = sync_saml_user_to_db(db, saml_data, cfg)

    if not user.is_active:
        logger.warning(f"SAML user account is inactive: {saml_data['saml_subject']}")
        audit_logger.log_login_failure(
            username=saml_data.get("email", "unknown"),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INACTIVE_USER",
            auth_method=AUTH_METHOD,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": str(user.uuid), "role": user.role}
    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    audit_logger.log_login_success(
        user_id=user.id,
        username=user.email,
        source_ip=client_ip,
        user_agent=user_agent,
        auth_method=AUTH_METHOD,
        details={"groups": saml_data["groups"]},
    )

    record_successful_login(db, user)
    logger.info(f"SAML authentication successful for user: {user.email}")

    refresh_token, _session_row = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=str(user.uuid),
        role=str(user.role),
        user_agent=user_agent,
        ip_address=client_ip,
    )

    relay_state = request_data["post_data"].get("RelayState") or _POST_LOGIN_REDIRECT
    # RelayState is IdP-controlled input. Only ever treated as a same-origin path,
    # never followed as an absolute URL — the same open-redirect guard OIDC's flow
    # does not need (it never takes a redirect target from the provider) but SAML's
    # RelayState convention does.
    redirect_to = relay_state if relay_state.startswith("/") else _POST_LOGIN_REDIRECT

    response = RedirectResponse(url=redirect_to, status_code=status.HTTP_302_FOUND)
    from app.auth.cookies import set_auth_cookies

    set_auth_cookies(response, access_token, refresh_token)
    return response


@router.get("/saml/sls")
@router.post("/saml/sls")
@limiter.limit(get_auth_rate_limit())
async def saml_sls(request: Request, response: Response, db: Session = Depends(get_db)):
    """Single Logout Service.

    Handles an IdP-initiated LogoutRequest (front-channel redirect binding — the
    browser is present, so the current session's own cookies are what gets
    revoked) and a LogoutResponse returned from an IdP after this SP itself
    requested logout.

    **Scope note:** this SP does not track ``(NameID, SessionIndex)`` per session
    (that needs a schema change beyond this revision — see the module docstring in
    ``auth/saml/provisioning.py`` for the same kind of explicitly deferred scope),
    so SP-initiated logout only terminates the local session; it does not send the
    IdP a LogoutRequest of its own. IdP-initiated logout is fully handled: the
    local session tied to the browser's own cookies is revoked either way.
    """
    client_ip, user_agent = _get_client_info(request)
    cfg = _require_enabled_config(db)
    request_data = await saml_request_data(request)
    auth = build_auth(request_data, cfg)

    from app.auth.cookies import clear_auth_cookies
    from app.auth.cookies import get_access_token_from_cookie
    from app.auth.cookies import get_refresh_token_from_cookie

    def _revoke_current_session() -> None:
        refresh_value = get_refresh_token_from_cookie(request)
        if refresh_value:
            payload, refresh_row = token_service.verify_refresh_token(db, refresh_value)
            if payload and refresh_row:
                token_service.revoke_token(db, str(refresh_row.jti), refresh_row.expires_at)
        access_value = get_access_token_from_cookie(request)
        if access_value:
            try:
                from app.core.security import verify_token

                access_payload = verify_token(access_value, expected_type=None)
                jti = access_payload.get("jti")
                if jti:
                    token_service.revoke_token(db, jti)
            except HTTPException:
                pass

    try:
        redirect_url = auth.process_slo(delete_session_cb=_revoke_current_session)
    except Exception as e:  # noqa: BLE001 - python3-saml raises broad OneLogin_Saml2_ValidationError
        logger.warning("SAML SLO processing failed: %s", e)
        redirect_url = None

    errors = auth.get_errors()
    if errors:
        logger.warning("SAML SLO errors: %s", errors)

    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGOUT,
        outcome=AuditOutcome.FAILURE if errors else AuditOutcome.SUCCESS,
        source_ip=client_ip,
        user_agent=user_agent,
        details={"all_sessions": False, "auth_method": AUTH_METHOD},
    )

    response = RedirectResponse(
        url=redirect_url or _POST_LOGIN_REDIRECT, status_code=status.HTTP_302_FOUND
    )
    clear_auth_cookies(response)
    return response
