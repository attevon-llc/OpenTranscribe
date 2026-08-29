"""OIDC login and callback endpoints.

Routes are ``/api/auth/oidc/{login,callback}``. The registered redirect URI points at
the SPA's ``/login`` route, not at these paths, so renaming them from the old
vendor-prefixed spelling needs no change at any identity provider.
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.login import record_successful_login
from app.auth.audit import audit_logger
from app.auth.cookies import clear_oidc_state_binding
from app.auth.cookies import get_oidc_state_binding
from app.auth.cookies import set_oidc_state_binding
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.lockout import check_and_record_attempt
from app.auth.mfa import MFAService
from app.auth.oidc import OIDCConfig
from app.auth.oidc import exchange_code_for_tokens
from app.auth.oidc import get_authorization_url
from app.auth.oidc import sync_oidc_user_to_db
from app.auth.oidc import validate_token as validate_oidc_token
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.session import OIDCStateStore
from app.auth.token_service import token_service
from app.core.config import settings
from app.db.base import get_db

router = APIRouter()


logger = logging.getLogger(__name__)

#: Audit ``auth_method`` for every event this module writes.
AUTH_METHOD = "oidc"

# Uses Redis for distributed deployments with automatic in-memory fallback
_oidc_state_store = OIDCStateStore()

# OIDC state expiry time in seconds (10 minutes)
_OIDC_STATE_EXPIRY_SECONDS = 600


@router.get("/oidc/login")
@limiter.limit(get_auth_rate_limit())
async def oidc_login(request: Request, response: Response, db: Session = Depends(get_db)):
    """
    Initiate the OIDC login flow.

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
    cfg = await run_in_threadpool(OIDCConfig.from_db, db)
    if not cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OIDC authentication is not enabled",
        )

    # Generate CSRF protection state
    state = secrets.token_urlsafe(32)

    # ...and a second secret that never travels in the URL. `state` alone proves
    # the callback corresponds to a flow WE started; it does not prove the flow
    # was started by THIS browser. Without that, an attacker starts a login, keeps
    # their own callback URL, and gets a victim to open it — signing the victim
    # into the attacker's account (login CSRF). The binding cookie closes it.
    state_binding = secrets.token_urlsafe(32)

    authorization_url, code_verifier = await get_authorization_url(state, cfg=cfg)

    # Store state with PKCE verifier in Redis-backed store
    # Returns False if state limit exceeded (prevents exhaustion attack)
    state_data: dict[str, str] = {"binding": _hash_binding(state_binding)}
    if code_verifier:
        state_data["code_verifier"] = code_verifier
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
        logger.info("OIDC login initiated with PKCE, redirecting to authorization URL")
    else:
        logger.info("OIDC login initiated, redirecting to authorization URL")

    response = JSONResponse(content={"authorization_url": authorization_url})
    set_oidc_state_binding(response, state_binding, _OIDC_STATE_EXPIRY_SECONDS)
    return response


def _hash_binding(secret: str) -> str:
    """Hash the binding secret before storing it.

    The store holds only the hash, so a Redis read cannot reconstruct a cookie
    that would let an attacker complete someone else's in-flight login.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def _store_session_id_token(db: Session, session_row, id_token: str) -> None:
    """Persist the ID token on the session row it belongs to.

    RP-initiated logout (OIDC RP-Initiated Logout 1.0) needs ``id_token_hint``, so the
    token has to outlive the callback. It is kept **server-side on the
    ``refresh_token`` row and encrypted at rest** — never handed to the browser. The
    reference implementation everyone copies puts it in a cookie by default and its
    own documentation calls that unsafe; a cookie makes the ID token, with its full
    identity claim set, readable by anything that reaches the cookie jar and outlives
    the session that justified keeping it. On this row it dies with the session:
    rotation, revocation and the concurrent-session cap already delete these rows.

    Failure is non-fatal — the login succeeds, and only federated logout degrades.
    """
    try:
        session_row.oidc_id_token = MFAService.encrypt_totp_secret(id_token)
        db.commit()
    except Exception as e:  # pragma: no cover - defensive
        db.rollback()
        logger.warning(f"Failed to store the OIDC id_token on the session row: {e}")


@router.get("/oidc/callback")
@limiter.limit(get_auth_rate_limit())
async def oidc_callback(
    request: Request,
    response: Response,
    code: str = Query(..., description="Authorization code from the identity provider"),
    state: str = Query(..., description="State parameter for CSRF protection"),
    db: Session = Depends(get_db),
):
    """
    Handle the OIDC callback.

    Exchanges the authorization code for tokens, validates the ID token, and
    creates/updates the user in the database.
    Supports PKCE (RFC 7636) for OAuth 2.1 compliance when enabled.

    Security:
    - Rate limited per IP: this anonymous route drives an outbound token exchange
      against the IdP plus a JIT user sync, i.e. an amplifier aimed at someone
      else's identity provider if left unlimited
    - State is single-use (deleted after retrieval to prevent replay attacks)
    - Uses Redis-backed storage for distributed deployments
    """
    client_ip, user_agent = _get_client_info(request)

    # Load OIDC config from database (DB > .env > defaults)
    cfg = OIDCConfig.from_db(db)
    if not cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OIDC authentication is not enabled",
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
            auth_method=AUTH_METHOD,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state parameter",
        )

    # The state proved this callback belongs to a flow we started. The binding
    # cookie proves it arrived in the browser that started it — without it, a
    # captured callback URL replayed in a victim's browser signs the victim into
    # the attacker's account.
    expected_binding = state_data.get("binding")
    presented = get_oidc_state_binding(request)
    if not expected_binding or not presented:
        logger.warning("OIDC callback missing its state-binding cookie")
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="MISSING_STATE_BINDING",
            auth_method=AUTH_METHOD,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state parameter",
        )
    if not secrets.compare_digest(_hash_binding(presented), expected_binding):
        logger.warning("OIDC callback presented a state-binding cookie that does not match")
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="STATE_BINDING_MISMATCH",
            auth_method=AUTH_METHOD,
        )
        # Same message as an unknown state: a mismatch must not be distinguishable.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state parameter",
        )

    # Extract PKCE verifier from state data
    code_verifier = state_data.get("code_verifier")

    # Exchange code for tokens (with PKCE verifier if available)
    tokens = await exchange_code_for_tokens(code, code_verifier, cfg=cfg)
    if not tokens:
        logger.error("Failed to exchange authorization code for tokens")
        # No identity is known yet at this point in the flow, so this shares the
        # "unknown" bucket with every other pre-identity refusal below — same
        # shape as proxy_login's unattributable-assertion bucket. Retries still
        # throttle: unlimited retry against a stolen/guessed authorization code
        # is exactly what account lockout (NIST AC-7) exists to bound.
        check_and_record_attempt("unknown", success=False)
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="TOKEN_EXCHANGE_FAILED",
            auth_method=AUTH_METHOD,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to exchange authorization code",
        )

    # Only the ID token authenticates. A response without one, or with one that fails
    # validation, is a hard 401 — there is deliberately no access-token fallback.
    oidc_data = await validate_oidc_token(
        tokens.access_token, cfg=cfg, id_token=tokens.id_token or None
    )
    if not oidc_data:
        logger.error("Invalid or missing ID token received from the OIDC provider")
        check_and_record_attempt("unknown", success=False)
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_TOKEN",
            auth_method=AUTH_METHOD,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
        )

    # Sync user to database. The already-resolved cfg is passed through so the
    # admission check inside runs against the same configuration this flow was
    # validated with, rather than re-reading it mid-login.
    user = sync_oidc_user_to_db(db, oidc_data, cfg)

    # Store the encrypted provider refresh token for federated logout (issue #125)
    if tokens.refresh_token:
        try:
            user.oidc_refresh_token = MFAService.encrypt_totp_secret(tokens.refresh_token)
            db.commit()
        except Exception as e:
            logger.warning(f"Failed to store the OIDC refresh token: {e}")
            # Non-fatal — login still succeeds, federated logout just won't work

    if not user.is_active:
        logger.warning(f"OIDC user account is inactive: {oidc_data['oidc_subject']}")
        check_and_record_attempt(oidc_data.get("email", "unknown"), success=False)
        audit_logger.log_login_failure(
            username=oidc_data.get("email", "unknown"),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INACTIVE_USER",
            auth_method=AUTH_METHOD,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    # A successful authentication clears whatever failure count this identifier
    # had accrued — the same clear-on-success semantics /token gives a local
    # password login.
    check_and_record_attempt(user.email, success=True)

    # Generate our own JWT token
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": str(user.uuid), "role": user.role}
    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    audit_logger.log_login_success(
        user_id=user.id,
        username=user.email,
        source_ip=client_ip,
        user_agent=user_agent,
        auth_method=AUTH_METHOD,
        # P1.2: claim *names* only, never values — lets an admin see in the audit
        # log that "groups" exists and "realm_access" does not, without this
        # becoming a second place group/claim values leak to.
        details={
            "claim_keys": oidc_data["claim_keys"],
            "roles_claim": cfg.roles_claim,
            "roles_claim_source": oidc_data["roles_claim_source"],
        },
    )

    # Every successful auth path stamps last_login_at — see pki.py for why.
    record_successful_login(db, user)

    logger.info(f"OIDC authentication successful for user: {user.email}")

    # Generate refresh token for OIDC users too
    refresh_token, session_row = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=str(user.uuid),
        role=str(user.role),
        user_agent=user_agent,
        ip_address=client_ip,
    )

    if tokens.id_token:
        _store_session_id_token(db, session_row, tokens.id_token)

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
    # The flow is complete; the binding cookie has served its purpose.
    clear_oidc_state_binding(response)
    return response
