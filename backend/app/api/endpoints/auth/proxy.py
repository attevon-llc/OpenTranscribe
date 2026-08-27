"""Trusted-header (reverse-proxy) authentication endpoint.

The header is consulted **here, at sign-in, and nowhere else on the hot path**. What
this endpoint returns is an ordinary session — a ``refresh_token`` row plus the usual
httpOnly cookies — so idle and absolute timeouts, the concurrent-session cap, the
revocation epoch and the sessions UI all apply to a proxy login without a single
special case. Re-deriving identity from a header on every request would be both
expensive and a second authorization path to keep correct.

The one thing that *is* checked per request is consistency:
``dependencies._enforce_proxy_identity_consistency`` refuses (and revokes) when the
proxy starts asserting somebody else on a session minted for the previous user. That
is the bug Open WebUI had to retrofit in v0.6.14 after #14406 — signing out of the
upstream IdP and back in as a different person left the old app session live.
"""

import logging
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.login import _check_mfa_requirement
from app.api.endpoints.auth.login import record_successful_login
from app.auth.audit import audit_logger
from app.auth.cookies import set_auth_cookies
from app.auth.direct_auth import create_access_token
from app.auth.lockout import check_and_record_attempt
from app.auth.proxy.assertion import REFUSAL_DETAIL
from app.auth.proxy.assertion import extract_proxy_assertion
from app.auth.proxy.config import ProxyConfig
from app.auth.proxy.provisioning import sync_proxy_user_to_db
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.token_service import token_service
from app.core.config import settings
from app.db.base import get_db
from app.schemas.user import Token

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("/proxy/authenticate", response_model=Token)
@limiter.limit(get_auth_rate_limit())
def proxy_login(request: Request, response: Response, db: Session = Depends(get_db)):
    """Establish a session from an identity a trusted reverse proxy asserted.

    Rate limited and lockout-tracked like every other credential endpoint: it mints
    access and refresh tokens, so an attacker who can reach the backend directly
    must not be able to retry header combinations for free.

    Args:
        request: FastAPI request (the limiter and the trust check both need it).
        response: Required by the rate limiter's header injection.
        db: Database session.

    Returns:
        Access/refresh tokens, plus the httpOnly auth cookies.

    Raises:
        HTTPException: 400 when proxy authentication is disabled or the account is
            inactive; 401 for every refusal — untrusted peer, wrong shared secret,
            unadmitted domain, refused link, JIT disabled. All of them share one
            message so the response is never an oracle.
    """
    cfg = ProxyConfig.from_db(db)
    if not cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Proxy (trusted header) authentication is not enabled",
        )

    client_ip, user_agent = _get_client_info(request)
    assertion = extract_proxy_assertion(request, cfg)

    # Lockout keys on the asserted address — the identity a proxy client is claiming,
    # the analogue of the username on /token. Unattributable refusals (no readable
    # address at all) share the "unknown" bucket, which only throttles further
    # failures.
    identifier = assertion.email if assertion else "unknown"
    is_locked, _unlock_at = check_and_record_attempt(identifier, success=assertion is not None)
    if is_locked:
        audit_logger.log_login_failure(
            username=identifier,
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="ACCOUNT_LOCKED",
            auth_method="proxy",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REFUSAL_DETAIL)

    if assertion is None:
        # Already audited with a specific error_code by extract_proxy_assertion.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REFUSAL_DETAIL)

    user = sync_proxy_user_to_db(db, assertion, cfg)

    if not user.is_active:
        audit_logger.log_login_failure(
            username=assertion.email,
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INACTIVE_USER",
            auth_method="proxy",
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user account")

    # FedRAMP IA-2: an already-MFA-enrolled user must not get a full session
    # through this path just because it skips the local-password form. The
    # local handler's _check_mfa_requirement already exempts PKI/OIDC users
    # authenticating natively; a proxy-asserted identity is not one of those
    # exemptions, so any TOTP the account has enrolled must still be verified
    # here, exactly as it would be on /token.
    mfa_response = _check_mfa_requirement(
        db, user, str(user.uuid), str(user.role), actual_auth_method="proxy"
    )
    if mfa_response:
        return mfa_response

    access_token = create_access_token(
        data={"sub": str(user.uuid), "role": user.role},
        expires_delta=timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token, _row = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=str(user.uuid),
        role=str(user.role),
        user_agent=user_agent,
        ip_address=client_ip,
    )

    audit_logger.log_login_success(
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        auth_method="proxy",
    )
    record_successful_login(db, user)
    logger.info("Proxy authentication successful for %s", user.email)

    result = JSONResponse(
        content={
            "access_token": access_token,
            # OAuth 2.0 token type, not a credential.
            "token_type": "bearer",  # noqa: S105 # nosec B105
            "refresh_token": refresh_token,
            "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    )
    set_auth_cookies(result, access_token, refresh_token)
    return result
