"""Refresh-token rotation, logout, and active-session endpoints."""

import logging
from datetime import UTC
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from jose import JWTError
from jose import jwt
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.dependencies import oauth2_scheme
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_KEYCLOAK
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.keycloak_auth import KeycloakConfig
from app.auth.keycloak_auth import call_keycloak_logout
from app.auth.mfa import MFAService
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.token_service import token_service
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import Token
from app.schemas.user import TokenRefreshRequest

router = APIRouter()


logger = logging.getLogger(__name__)


@router.post("/token/refresh", response_model=Token)
@limiter.limit(get_auth_rate_limit())
def refresh_access_token(
    request: Request,
    body: TokenRefreshRequest,
    db: Session = Depends(get_db),
):
    """
    Exchange a refresh token for new access and refresh tokens.

    This endpoint allows clients to obtain a new access token without
    requiring the user to re-authenticate with credentials.

    Security (FedRAMP AC-12, OAuth 2.1):
    - Validates refresh token signature and expiration
    - Checks token is not revoked (Redis blacklist)
    - Rate limited to prevent abuse
    - Implements refresh token rotation (revokes old, issues new)
    - Limits impact of stolen refresh tokens

    Args:
        request: FastAPI request object (for rate limiting)
        body: Request body containing refresh_token
        db: Database session

    Returns:
        New access token, token type, and new rotated refresh token

    Raises:
        HTTPException: If refresh token is invalid, expired, or revoked
    """
    # Try body first, then fall back to httpOnly cookie for refresh token
    from app.auth.cookies import get_refresh_token_from_cookie

    refresh_token_value = body.refresh_token
    if not refresh_token_value:
        refresh_token_value = get_refresh_token_from_cookie(request) or ""

    # Verify refresh token
    payload, refresh_token_record = token_service.verify_refresh_token(db, refresh_token_value)

    if not payload or not refresh_token_record:
        logger.warning("Token refresh failed: invalid or revoked refresh token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Get user to verify still active
    user_uuid_str_raw = payload.get("sub")
    if not user_uuid_str_raw:
        logger.warning("Token refresh failed: no user UUID in payload")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_uuid_str = str(user_uuid_str_raw)
    user_uuid = UUID(user_uuid_str)
    user = db.query(User).filter(User.uuid == user_uuid).first()

    if not user:
        logger.warning(f"Token refresh failed: user not found (uuid={user_uuid_str[:8]}...)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        logger.warning(f"Token refresh failed: user inactive (id={user.id})")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Generate new access token
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": user_uuid_str, "role": str(user.role), "type": "access"}
    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    # Rotate refresh token (revoke old, create new) - OAuth 2.1 best practice
    client_ip, user_agent = _get_client_info(request)
    new_refresh_token, _ = token_service.rotate_refresh_token(
        db=db,
        old_token=refresh_token_value,
        old_token_record=refresh_token_record,
        user_id=user.id,
        user_uuid=user_uuid_str,
        role=str(user.role),
        user_agent=user_agent,
        ip_address=client_ip,
    )

    # Log token refresh with rotation
    audit_logger.log_token_refresh(
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    logger.info(f"Token refresh with rotation successful for user {user.id}")

    response = JSONResponse(
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "refresh_token": new_refresh_token,
            "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    )

    # Set new httpOnly cookies with rotated tokens (C2 security hardening)
    from app.auth.cookies import set_auth_cookies

    set_auth_cookies(response, access_token, new_refresh_token)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """
    Logout the current session by revoking the current tokens.

    This endpoint revokes both the access token (via JTI blacklist) and
    any associated refresh token, effectively logging out the current session.
    For Keycloak users, also terminates the federated Keycloak session.

    Security (FedRAMP AC-12):
    - Adds access token JTI to Redis blacklist
    - Revokes associated refresh token in database
    - Terminates Keycloak SSO session (if applicable)
    - Tokens cannot be reused after logout

    Args:
        request: FastAPI request object
        token: Current access token (from Authorization header)
        db: Database session

    Returns:
        Success message
    """
    from app.auth.cookies import clear_auth_cookies
    from app.auth.cookies import get_access_token_from_cookie

    # Fall back to cookie if no Bearer token
    if not token:
        token = get_access_token_from_cookie(request)

    client_ip, user_agent = _get_client_info(request)

    try:
        if token:
            # Decode token to get JTI and user info
            payload = jwt.decode(
                token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
            )
            jti = payload.get("jti")
            exp_timestamp = payload.get("exp")
            user_uuid_str = payload.get("sub")

            if jti:
                # Calculate expiration datetime from timestamp
                from datetime import datetime

                expires_at = (
                    datetime.fromtimestamp(exp_timestamp, tz=UTC) if exp_timestamp else None
                )

                # Revoke access token
                token_service.revoke_token(db, jti, expires_at)
                logger.info(f"Logout: revoked access token (jti={jti[:8]}...)")

            # Log logout event and handle Keycloak federated logout
            if user_uuid_str:
                user_uuid = UUID(user_uuid_str)
                user = db.query(User).filter(User.uuid == user_uuid).first()
                if user:
                    # Keycloak federated logout (issue #125)
                    if user.auth_type == AUTH_TYPE_KEYCLOAK and user.keycloak_refresh_token:
                        try:
                            kc_cfg = KeycloakConfig.from_db(db)
                            decrypted_rt = MFAService.decrypt_totp_secret(
                                user.keycloak_refresh_token
                            )
                            await call_keycloak_logout(decrypted_rt, cfg=kc_cfg)
                        except Exception as e:
                            logger.warning(f"Keycloak federated logout failed: {e}")
                        finally:
                            # Always clear stored token regardless of outcome
                            user.keycloak_refresh_token = None
                            db.commit()

                    audit_logger.log_logout(
                        user_id=user.id,
                        username=str(user.email),
                        source_ip=client_ip,
                        user_agent=user_agent,
                        all_sessions=False,
                    )

        response = JSONResponse(content={"message": "Successfully logged out"})
        clear_auth_cookies(response)
        return response

    except JWTError as e:
        logger.warning(f"Logout with invalid token: {e}")
        # Still return success - user wanted to logout anyway
        response = JSONResponse(content={"message": "Successfully logged out"})
        clear_auth_cookies(response)
        return response


@router.post("/logout/all")
async def logout_all_sessions(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Logout from all sessions by revoking all user's refresh tokens.

    This endpoint revokes all refresh tokens for the current user,
    effectively logging them out from all devices/sessions.
    For Keycloak users, also terminates the federated Keycloak session.

    Security (FedRAMP AC-12):
    - Revokes all user's refresh tokens
    - Adds all token JTIs to Redis blacklist
    - Terminates Keycloak SSO session (if applicable)
    - Useful for security events (password change, compromised account)

    Args:
        request: FastAPI request object
        current_user: Current authenticated user
        db: Database session

    Returns:
        Success message with count of revoked sessions
    """
    count = token_service.revoke_all_user_tokens(db, current_user.id)

    # Keycloak federated logout (issue #125)
    if current_user.auth_type == AUTH_TYPE_KEYCLOAK and current_user.keycloak_refresh_token:
        try:
            kc_cfg = KeycloakConfig.from_db(db)
            decrypted_rt = MFAService.decrypt_totp_secret(current_user.keycloak_refresh_token)
            await call_keycloak_logout(decrypted_rt, cfg=kc_cfg)
        except Exception as e:
            logger.warning(f"Keycloak federated logout failed: {e}")
        finally:
            # Always clear stored token regardless of outcome
            current_user.keycloak_refresh_token = None
            db.commit()

    # Log logout from all sessions
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log_logout(
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        all_sessions=True,
    )

    logger.info(f"User {current_user.id} logged out from all sessions ({count} tokens revoked)")

    from app.auth.cookies import clear_auth_cookies

    response = JSONResponse(
        content={
            "message": "Successfully logged out from all sessions",
            "sessions_revoked": count,
        }
    )
    clear_auth_cookies(response)
    return response


@router.get("/sessions")
def get_active_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get all active sessions for the current user.

    Returns a list of active refresh tokens (sessions) with metadata
    like creation time, user agent, and IP address.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of active session info
    """
    sessions = token_service.get_user_active_sessions(db, current_user.id)

    return {
        "sessions": sessions,
        "total": len(sessions),
    }
