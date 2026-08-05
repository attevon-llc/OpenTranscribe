"""The ``/token`` + ``/login`` endpoint and its authentication orchestration."""

import logging
import os
from datetime import UTC
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.endpoints.auth.authenticators import _authenticate_production_user
from app.api.endpoints.auth.authenticators import _authenticate_testing_user
from app.api.endpoints.auth.authenticators import _get_user_role
from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.mfa_tokens import _create_mfa_token
from app.api.endpoints.auth.mfa_tokens import _is_mfa_enabled
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_KEYCLOAK
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import AUTH_TYPE_PKI
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.lockout import check_and_record_attempt
from app.auth.lockout import get_lockout_info
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.token_service import token_service
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.models.user_mfa import UserMFA
from app.schemas.user import Token

router = APIRouter()


logger = logging.getLogger(__name__)


def _perform_authentication(
    db: Session, username: str, password: str
) -> tuple[bool, str, dict, str]:
    """Handle testing vs production authentication.

    Args:
        db: Database session
        username: Username to authenticate
        password: Password to verify

    Returns:
        Tuple of (auth_success, user_uuid_str, user_data, actual_auth_method)
        - auth_success: True if authentication succeeded
        - user_uuid_str: User UUID string (empty if failed)
        - user_data: User data dict (empty if failed or testing)
        - actual_auth_method: How the user actually authenticated ("local", "ldap", etc.)

    Raises:
        HTTPException: If user is inactive (400) or other non-auth errors
    """
    # TESTING enables auth shortcuts (a fabricated user, a password-free login path).
    # Gate on is_hardened as well, so the flag can never take effect in a real
    # deployment even if it leaks into the environment (issue #284 A0.8).
    testing_environment = (
        os.environ.get("TESTING", "False").lower() == "true" and not settings.is_hardened
    )

    if testing_environment:
        user_uuid_str = _authenticate_testing_user(db, username, password)
        return True, user_uuid_str, {}, "local"

    try:
        user_uuid_str, user_data, actual_auth_method = _authenticate_production_user(
            db, username, password
        )
        return True, user_uuid_str, user_data, actual_auth_method
    except HTTPException as auth_error:
        if auth_error.status_code == status.HTTP_401_UNAUTHORIZED:
            return False, "", {}, ""
        # Re-raise non-auth errors (400 for inactive user, etc.)
        raise


def _handle_lockout_check(
    username: str,
    auth_success: bool,
    client_ip: str,
    user_agent: str,
    exempt_from_lockout: bool = False,
) -> tuple[bool, int | None]:
    """Handle lockout logic with atomic check-and-record.

    Args:
        username: Username being authenticated
        auth_success: Whether authentication succeeded
        client_ip: Client IP address
        user_agent: Client user agent
        exempt_from_lockout: If True, record attempts for audit but never lock.
            Used for super admin accounts with allow_local_fallback.

    Returns:
        Tuple of (is_locked, unlock_time)
        - is_locked: True if account is locked
        - unlock_time: Time when account unlocks (or None)

    Note:
        Also logs audit events for lockout and login failures.
    """

    # Atomic lockout check and record (prevents race conditions - CRITICAL-1 fix)
    lockout_result = check_and_record_attempt(
        username, success=auth_success, exempt_from_lockout=exempt_from_lockout
    )
    is_locked, unlock_datetime = lockout_result
    unlock_time: int | None = int(unlock_datetime.timestamp()) if unlock_datetime else None

    if is_locked:
        # Check if account was just locked (lockout event) vs already locked
        lockout_info = get_lockout_info(username)
        if lockout_info["failed_attempts"] >= settings.ACCOUNT_LOCKOUT_THRESHOLD:
            # Account was just locked - log lockout event
            audit_logger.log_account_lockout(
                username=username,
                source_ip=client_ip,
                user_agent=user_agent,
                lockout_duration_minutes=settings.ACCOUNT_LOCKOUT_DURATION_MINUTES,
                failed_attempts=lockout_info["failed_attempts"],
            )
        return True, unlock_time

    if not auth_success:
        # Authentication failed but not locked (yet)
        lockout_info = get_lockout_info(username)
        audit_logger.log_login_failure(
            username=username,
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_CREDENTIALS",
            auth_method="ldap" if settings.LDAP_ENABLED else "local",
            lockout_count=lockout_info.get("lockout_count", 0),
        )

    return False, None


def _check_mfa_requirement(
    db: Session,
    user: User,
    user_uuid_str: str,
    user_role: str,
    actual_auth_method: str = "",
) -> JSONResponse | None:
    """Check if MFA is required for user and return MFA response if needed.

    Args:
        db: Database session
        user: User model object
        user_uuid_str: User UUID string
        user_role: User's role
        actual_auth_method: How the user actually authenticated. When a PKI/Keycloak
            user authenticates via password fallback, actual_auth_method will be "local"
            and MFA should still apply.

    Returns:
        JSONResponse with MFA token if MFA required, None otherwise
    """
    # Skip MFA check if MFA is disabled (check DB first, then .env)
    if not _is_mfa_enabled(db):
        return None

    # Skip MFA for PKI and Keycloak users ONLY if they authenticated via their native method.
    # If they used local password fallback, MFA must still apply.
    if (
        user.auth_type in [AUTH_TYPE_PKI, AUTH_TYPE_KEYCLOAK]
        and actual_auth_method != AUTH_TYPE_LOCAL
    ):
        return None

    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == user.id).first()

    if user_mfa and user_mfa.totp_enabled:
        # User has MFA enabled - return MFA token instead of access token
        mfa_token = _create_mfa_token(user_uuid_str, user_role)
        logger.info(f"MFA verification required for user: {str(user.email)}")
        return JSONResponse(
            content={
                "mfa_required": True,
                "mfa_token": mfa_token,
                "message": "MFA verification required",
            }
        )

    return None


def _generate_login_tokens(
    db: Session,
    user: User,
    user_uuid_str: str,
    user_role: str,
    user_agent: str,
    client_ip: str,
) -> JSONResponse:
    """Generate access and refresh tokens for successful login.

    Args:
        db: Database session
        user: User model object
        user_uuid_str: User UUID string
        user_role: User's role
        user_agent: Client user agent
        client_ip: Client IP address

    Returns:
        JSONResponse with access_token, refresh_token, and token metadata
    """
    # Generate the JWT access token with role information
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": user_uuid_str, "type": "access"}
    if user_role:
        token_data["role"] = user_role

    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    # Generate refresh token (FedRAMP AC-12)
    refresh_token, _ = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=user_uuid_str,
        role=user_role,
        user_agent=user_agent,
        ip_address=client_ip,
    )

    # Log successful login
    audit_logger.log_login_success(
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        auth_method="ldap"
        if settings.LDAP_ENABLED and user.auth_type != AUTH_TYPE_LOCAL
        else "local",
    )

    logger.info(f"Login successful for user: {str(user.email)}")
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


@router.post("/token", response_model=Token)
@router.post("/login", response_model=Token)  # Add alias for frontend compatibility
@limiter.limit(get_auth_rate_limit())
def login_for_access_token(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """OAuth2 compatible token login, get an access token for future requests.

    Rate limited to prevent brute force attacks.
    Account lockout enforced after repeated failed attempts.

    Uses atomic check-and-record for lockout to prevent race conditions where
    multiple concurrent requests could bypass lockout by checking before
    the failed attempt is recorded.

    Args:
        request: FastAPI request object (for rate limiting)
        form_data: OAuth2 form data with username and password
        db: Database session

    Returns:
        Access token and token type

    Raises:
        HTTPException: If authentication fails, account locked, or rate limit exceeded
    """
    username = form_data.username
    logger.info(f"Login attempt for user: {username}")

    try:
        # Perform authentication (handles testing vs production)
        auth_success, user_uuid_str, user_data, actual_auth_method = _perform_authentication(
            db, username, form_data.password
        )

        # Get client info for audit logging
        client_ip, user_agent = _get_client_info(request)

        # Determine if user is exempt from lockout (super admin with local fallback)
        exempt_from_lockout = False
        if auth_success:
            _user_for_lockout = db.query(User).filter(User.uuid == UUID(user_uuid_str)).first()
            if _user_for_lockout and getattr(_user_for_lockout, "allow_local_fallback", False):
                exempt_from_lockout = _user_for_lockout.role == "super_admin"

        # Handle lockout check and recording
        is_locked, _ = _handle_lockout_check(
            username, auth_success, client_ip, user_agent, exempt_from_lockout=exempt_from_lockout
        )

        if is_locked:
            # Return same error as invalid credentials to prevent username enumeration
            logger.warning(f"Login blocked for locked account: {username}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not auth_success:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Get user from database
        user_uuid = UUID(user_uuid_str)
        user_db = db.query(User).filter(User.uuid == user_uuid).first()
        if not user_db:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Get user's role for inclusion in the token
        user_role = _get_user_role(db, user_uuid_str, user_data)

        # FedRAMP AC-10: Enforce concurrent session limit
        if settings.MAX_CONCURRENT_SESSIONS > 0:
            from datetime import datetime

            from app.models.refresh_token import RefreshToken

            # Use SELECT FOR UPDATE to prevent race conditions when checking/modifying sessions
            # This acquires row-level locks on the user's active sessions
            # Note: Cannot use with_for_update() on aggregate queries, so we query rows and count
            sessions_stmt = (
                select(RefreshToken)
                .where(
                    RefreshToken.user_id == user_db.id,
                    RefreshToken.revoked_at.is_(None),
                    RefreshToken.expires_at > datetime.now(UTC),
                )
                .with_for_update()
            )
            active_session_rows = db.execute(sessions_stmt).scalars().all()
            active_sessions = len(active_session_rows)

            if active_sessions >= settings.MAX_CONCURRENT_SESSIONS:
                if settings.CONCURRENT_SESSION_POLICY == "reject":
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=f"Maximum {settings.MAX_CONCURRENT_SESSIONS} concurrent sessions reached. Please logout from another device.",
                    )
                elif (
                    settings.CONCURRENT_SESSION_POLICY == "terminate_oldest" and active_session_rows
                ):
                    # Terminate oldest session - rows are already locked from the query above
                    # Sort by created_at to find the oldest
                    oldest_token = min(active_session_rows, key=lambda t: t.created_at)
                    oldest_token.revoked_at = datetime.now(UTC)  # type: ignore[assignment]
                    db.commit()

                    audit_logger.log(
                        event_type=AuditEventType.AUTH_SESSION_EXPIRED,
                        user_id=user_db.id,
                        username=str(user_db.email),
                        outcome=AuditOutcome.SUCCESS,
                        source_ip=client_ip,
                        user_agent=user_agent,
                        details={
                            "reason": "concurrent_session_limit",
                            "policy": "terminate_oldest",
                        },
                    )

        # Check if MFA is required for this user (FedRAMP IA-2)
        # Pass actual_auth_method so fallback logins still get MFA
        mfa_response = _check_mfa_requirement(
            db, user_db, user_uuid_str, user_role, actual_auth_method=actual_auth_method
        )
        if mfa_response:
            return mfa_response

        # Generate tokens and return response
        return _generate_login_tokens(db, user_db, user_uuid_str, user_role, user_agent, client_ip)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error during authentication: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred during authentication",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
