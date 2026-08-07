"""Keycloak OIDC login and callback endpoints."""

import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import audit_logger
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.keycloak_auth import KeycloakConfig
from app.auth.keycloak_auth import exchange_code_for_tokens
from app.auth.keycloak_auth import get_authorization_url
from app.auth.keycloak_auth import sync_keycloak_user_to_db
from app.auth.keycloak_auth import validate_token as validate_keycloak_token
from app.auth.mfa import MFAService
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.session import OIDCStateStore
from app.auth.token_service import token_service
from app.core.config import settings
from app.db.base import get_db

router = APIRouter()


logger = logging.getLogger(__name__)


# Uses Redis for distributed deployments with automatic in-memory fallback
_oidc_state_store = OIDCStateStore()

# OIDC state expiry time in seconds (10 minutes)
_OIDC_STATE_EXPIRY_SECONDS = 600


@router.get("/keycloak/login")
@limiter.limit(get_auth_rate_limit())
async def keycloak_login(request: Request, db: Session = Depends(get_db)):
    """
    Initiate Keycloak OIDC login flow.

    Returns an authorization URL that the frontend should redirect to.
    Supports PKCE (RFC 7636) for OAuth 2.1 compliance when enabled.

    Security:
    - Rate limited per IP: the route is anonymous and writes an OIDC state per call
    - Uses Redis-backed state storage for distributed deployments
    - Enforces maximum state count to prevent state exhaustion attacks
    - States expire after 10 minutes and are single-use
    """
    # This handler became async so the authorization endpoint can come from the
    # provider's discovery document. Its blocking prologue (sync DB reads, and a Redis
    # state-count scan inside store_state) therefore has to be pushed back off the event
    # loop explicitly — as a sync handler FastAPI did that for us.
    kc_cfg = await run_in_threadpool(KeycloakConfig.from_db, db)
    if not kc_cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keycloak authentication is not enabled",
        )

    # Generate CSRF protection state
    state = secrets.token_urlsafe(32)

    authorization_url, code_verifier = await get_authorization_url(state, cfg=kc_cfg)

    # Store state with PKCE verifier in Redis-backed store
    # Returns False if state limit exceeded (prevents exhaustion attack)
    state_data = {"code_verifier": code_verifier} if code_verifier else {}
    stored = await run_in_threadpool(
        _oidc_state_store.store_state,
        state=state,
        data=state_data,
        expires_seconds=_OIDC_STATE_EXPIRY_SECONDS,
    )

    if not stored:
        logger.error("Failed to store OIDC state - state limit exceeded")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable. Please try again later.",
        )

    if code_verifier:
        logger.info("Keycloak login initiated with PKCE, redirecting to authorization URL")
    else:
        logger.info("Keycloak login initiated, redirecting to authorization URL")

    return {"authorization_url": authorization_url}


@router.get("/keycloak/callback")
@limiter.limit(get_auth_rate_limit())
async def keycloak_callback(
    request: Request,
    code: str = Query(..., description="Authorization code from Keycloak"),
    state: str = Query(..., description="State parameter for CSRF protection"),
    db: Session = Depends(get_db),
):
    """
    Handle Keycloak OIDC callback.

    Exchanges authorization code for tokens, validates them,
    and creates/updates user in database.
    Supports PKCE (RFC 7636) for OAuth 2.1 compliance when enabled.

    Security:
    - Rate limited per IP: this anonymous route drives an outbound token exchange
      against the IdP plus a JIT user sync, i.e. an amplifier aimed at someone
      else's identity provider if left unlimited
    - State is single-use (deleted after retrieval to prevent replay attacks)
    - Uses Redis-backed storage for distributed deployments
    """
    client_ip, user_agent = _get_client_info(request)

    # Load Keycloak config from database (DB > .env > defaults)
    kc_cfg = KeycloakConfig.from_db(db)
    if not kc_cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keycloak authentication is not enabled",
        )

    # Retrieve and delete state (single-use, CSRF protection)
    state_data = _oidc_state_store.get_state(state)
    if state_data is None:
        logger.warning("Invalid OIDC state parameter received")
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_STATE",
            auth_method="keycloak",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state parameter",
        )

    # Extract PKCE verifier from state data
    code_verifier = state_data.get("code_verifier")

    # Exchange code for tokens (with PKCE verifier if available)
    tokens = await exchange_code_for_tokens(code, code_verifier, cfg=kc_cfg)
    if not tokens:
        logger.error("Failed to exchange authorization code for tokens")
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="TOKEN_EXCHANGE_FAILED",
            auth_method="keycloak",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to exchange authorization code",
        )

    # Validate token and get user data. The ID token is preferred — it is the only one
    # OIDC guarantees to be a verifiable JWT (issue #353).
    keycloak_data = await validate_keycloak_token(
        tokens.access_token, cfg=kc_cfg, id_token=tokens.id_token or None
    )
    if not keycloak_data:
        logger.error("Invalid token received from the OIDC provider")
        # Log Keycloak login failure
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_TOKEN",
            auth_method="keycloak",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
        )

    # Sync user to database
    user = sync_keycloak_user_to_db(db, keycloak_data)

    # Store encrypted Keycloak refresh token for federated logout (issue #125)
    if tokens.refresh_token:
        try:
            user.keycloak_refresh_token = MFAService.encrypt_totp_secret(tokens.refresh_token)
            db.commit()
        except Exception as e:
            logger.warning(f"Failed to store Keycloak refresh token: {e}")
            # Non-fatal — login still succeeds, federated logout just won't work

    if not user.is_active:
        logger.warning(f"Keycloak user account is inactive: {keycloak_data['keycloak_id']}")
        # Log Keycloak login failure for inactive user
        audit_logger.log_login_failure(
            username=keycloak_data.get("email", "unknown"),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INACTIVE_USER",
            auth_method="keycloak",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    # Generate our own JWT token
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": str(user.uuid), "role": user.role}
    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    # Log Keycloak login success
    audit_logger.log_login_success(
        user_id=user.id,
        username=user.email,
        source_ip=client_ip,
        user_agent=user_agent,
        auth_method="keycloak",
    )

    logger.info(f"Keycloak authentication successful for user: {user.email}")

    # Generate refresh token for Keycloak users too
    refresh_token, _ = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=str(user.uuid),
        role=str(user.role),
        user_agent=user_agent,
        ip_address=client_ip,
    )

    response = JSONResponse(
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "refresh_token": refresh_token,
            "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    )

    # Set httpOnly cookies for browser-based authentication (C2 security hardening)
    from app.auth.cookies import set_auth_cookies

    set_auth_cookies(response, access_token, refresh_token)
    return response
