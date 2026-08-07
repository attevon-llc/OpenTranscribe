"""The ``/token`` + ``/login`` endpoint and its authentication orchestration."""

import logging
import os
from datetime import UTC
from datetime import datetime
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
from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_ENROLL
from app.api.endpoints.auth.mfa_tokens import _create_mfa_token
from app.api.endpoints.auth.mfa_tokens import _is_mfa_enabled
from app.api.endpoints.auth.mfa_tokens import _is_mfa_required
from app.api.endpoints.auth.mfa_tokens import _user_can_setup_mfa
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_KEYCLOAK
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import AUTH_TYPE_PKI
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.lockout import check_and_record_attempt
from app.auth.lockout import get_lockout_info
from app.auth.password_policy import get_days_until_expiration
from app.auth.password_policy import is_password_expired
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.token_service import token_service
from app.auth.utils import local_password_allowed
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
          Empty when authentication failed.

    Raises:
        HTTPException: Only for genuinely unexpected errors. Bad credentials AND a
            disabled account both come back as a plain failure.
    """
    # TESTING enables auth shortcuts (a fabricated user, a password-free login path).
    # Gate on is_hardened as well, so the flag can never take effect in a real
    # deployment even if it leaks into the environment (issue #284 A0.8).
    testing_environment = (
        os.environ.get("TESTING", "False").lower() == "true" and not settings.is_hardened
    )

    try:
        if testing_environment:
            user_uuid_str = _authenticate_testing_user(db, username, password)
            return True, user_uuid_str, {}, AUTH_TYPE_LOCAL

        user_uuid_str, user_data, actual_auth_method = _authenticate_production_user(
            db, username, password
        )
        return True, user_uuid_str, user_data, actual_auth_method
    except HTTPException as auth_error:
        if auth_error.status_code == status.HTTP_401_UNAUTHORIZED:
            return False, "", {}, ""
        if auth_error.status_code == status.HTTP_400_BAD_REQUEST:
            # "Inactive user account". Surfacing that 400 to an anonymous caller was a
            # username-enumeration oracle (every other failure is a uniform 401), and
            # because it was raised before the lockout recorder ran it also let an
            # attacker probe a disabled account without ever tripping lockout. Treat it
            # as an ordinary failed attempt; the reason stays in the server log only.
            logger.warning("Login refused for username=%s: %s", username, auth_error.detail)
            return False, "", {}, ""
        # Re-raise genuinely unexpected errors.
        raise


def _is_exempt_from_lockout(db: Session, username: str, user_uuid_str: str) -> bool:
    """Whether this login attempt targets a lockout-exempt account.

    Resolves the account from the attempt itself, so the exemption applies to failures
    as well as successes (NIST AC-7 allows exempting emergency-access accounts, and a
    super admin that can be locked out by an attacker is the outage that exemption
    exists to prevent). Attempts are still recorded for audit — see
    ``lockout._record_attempt_audit_only``.

    Args:
        db: Database session
        username: Submitted username (email or LDAP uid)
        user_uuid_str: UUID of the authenticated user, empty when the attempt failed

    Returns:
        bool: True only for a ``super_admin`` that has local fallback enabled.
    """
    try:
        if user_uuid_str:
            user = db.query(User).filter(User.uuid == UUID(user_uuid_str)).first()
        else:
            user = (
                db.query(User)
                .filter((User.email == username) | (User.ldap_uid == username))
                .first()
            )
    except Exception:  # noqa: BLE001 - a lookup failure must never break login
        logger.debug("Could not resolve account for lockout exemption", exc_info=True)
        return False

    if not user or not getattr(user, "allow_local_fallback", False):
        return False
    return bool(user.role == ROLE_SUPER_ADMIN)


def _handle_lockout_check(
    username: str,
    auth_success: bool,
    client_ip: str,
    user_agent: str,
    exempt_from_lockout: bool = False,
    auth_method: str = "",
) -> tuple[bool, int | None]:
    """Handle lockout logic with atomic check-and-record.

    Args:
        username: Username being authenticated
        auth_success: Whether authentication succeeded
        client_ip: Client IP address
        user_agent: Client user agent
        exempt_from_lockout: If True, record attempts for audit but never lock.
            Used for super admin accounts with allow_local_fallback.
        auth_method: How the caller actually authenticated. Empty on a failure where no
            method ever succeeded, which is audited as "unknown".

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
            # The method that was actually used, not "whatever LDAP_ENABLED implies":
            # deriving it from the setting audited EVERY failure on an LDAP-enabled
            # deployment as an LDAP failure, including local-password failures.
            auth_method=auth_method or "unknown",
            lockout_count=lockout_info.get("lockout_count", 0),
        )

    return False, None


def record_successful_login(db: Session, user: User) -> None:
    """Stamp ``last_login_at`` for a caller that has just been given a session.

    **Every successful authentication path must call this** — local/LDAP login,
    PKI, Keycloak, and MFA completion. Nothing wrote the column, so the admin UI
    reported ``null`` for every user forever and no inactive-account control
    (FedRAMP AC-2(3), the usual 30/60/90-day disable) had any data to act on.

    Stamped where a full session is issued rather than where the first factor
    passes: an MFA challenge that is never answered is not a login.

    Args:
        db: Database session
        user: The authenticated user row
    """
    try:
        user.last_login_at = datetime.now(UTC)  # type: ignore[assignment]
        db.commit()
    except Exception:
        # Bookkeeping must never cost the caller the session they just earned.
        logger.exception("Could not stamp last_login_at for user_id=%s", getattr(user, "id", None))
        db.rollback()


def _apply_password_expiry(
    db: Session,
    user: User,
    auth_method: str,
    client_ip: str,
    user_agent: str,
) -> None:
    """Flag an expired local password so the dependency gate forces a change.

    ``password_policy.is_password_expired`` existed with zero call sites, so
    ``PASSWORD_MAX_AGE_DAYS`` was reported by the admin account-status page and
    enforced nowhere. Rather than invent a second mechanism, an expired password
    sets ``must_change_password`` and flows into the same
    ``get_current_active_user`` gate the admin force-change flag uses.

    Only accounts that actually hold a local password are considered:
    ``password_changed_at`` is meaningless for an LDAP/OIDC/PKI identity, and
    treating it as expired would lock those users out of an app whose password
    they cannot change here.

    Args:
        db: Database session
        user: The authenticated user row
        auth_method: The method actually used for this login ("local", "ldap", …)
        client_ip: Client IP address, for the audit record
        user_agent: Client user agent, for the audit record
    """
    if auth_method != AUTH_TYPE_LOCAL:
        # The credential just verified was not a local password.
        return

    allowed, reason = local_password_allowed(
        str(user.auth_type) if user.auth_type else None,
        bool(getattr(user, "allow_local_fallback", False)),
    )
    if not allowed:
        logger.debug("Skipping password expiry for %s: %s", user.email, reason)
        return

    password_changed_at = getattr(user, "password_changed_at", None)
    if password_changed_at is None:
        # ``is_password_expired`` treats "no recorded change time" as expired, which is
        # the right default for a policy primitive but the wrong one HERE: nothing on
        # the account-creation paths (initial_data, users.py, registration.py) ever
        # stamped the column, so almost every pre-existing local user carries NULL.
        # Forcing all of them through a password change on their next login would be a
        # self-inflicted outage, not a control. Say so loudly instead — same reasoning
        # as the password-history degradation warning in password_policy.
        logger.warning(
            "Password age unknown for user_id=%s (password_changed_at is NULL); expiry "
            "not enforced for this login. Backfill the column to enable IA-5(1).",
            user.id,
        )
        return

    if not is_password_expired(password_changed_at):
        return

    if not user.must_change_password:
        user.must_change_password = True  # type: ignore[assignment]
        db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_PASSWORD_EXPIRED,
        outcome=AuditOutcome.SUCCESS,
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        error_code="PASSWORD_EXPIRED",
        details={
            "max_age_days": settings.PASSWORD_MAX_AGE_DAYS,
            "days_until_expiration": get_days_until_expiration(password_changed_at),
        },
    )
    logger.info("Password expired for user %s; a change is now required", user.email)


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
        JSONResponse with an MFA half-token if MFA verification or enrolment is
        required, None if the caller may be issued a full session.
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

    if _is_mfa_required(db):
        # Deployment requires MFA and this user has not enrolled. Enrolment used to be
        # enforced only by the SPA, so any API/CLI client that ignored the hint received
        # a full, unrestricted session on an MFA_REQUIRED deployment.
        if not _user_can_setup_mfa(user) and actual_auth_method != AUTH_TYPE_LOCAL:
            # An external IdP owns this user's second factor and there is no local TOTP
            # to enrol in — same reasoning as the PKI/Keycloak bypass above.
            return None

        # Same short-lived, single-use half-token as the verification branch, but scoped
        # to enrolment: it carries the "mfa" type claim (so every access-token consumer
        # rejects it) and only authorizes /mfa/setup + /mfa/verify-setup.
        mfa_token = _create_mfa_token(user_uuid_str, user_role, scope=MFA_SCOPE_ENROLL)
        logger.info(f"MFA enrollment required before access for user: {str(user.email)}")
        return JSONResponse(
            content={
                "mfa_required": True,
                "mfa_enrollment_required": True,
                "mfa_token": mfa_token,
                "message": "MFA enrollment is required before access is granted",
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
    auth_method: str = "",
) -> JSONResponse:
    """Generate access and refresh tokens for successful login.

    Args:
        db: Database session
        user: User model object
        user_uuid_str: User UUID string
        user_role: User's role
        user_agent: Client user agent
        client_ip: Client IP address
        auth_method: How the user actually authenticated ("local", "ldap", …)

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

    # Stamp last_login_at now that a full session exists (FedRAMP AC-2(3)).
    record_successful_login(db, user)

    # Log successful login
    audit_logger.log_login_success(
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        # The method actually used, not one inferred from LDAP_ENABLED + auth_type: a
        # PKI/Keycloak user falling back to a local password was audited as "ldap".
        auth_method=auth_method or str(user.auth_type),
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

        # Determine if user is exempt from lockout (super admin with local fallback).
        # Resolved BEFORE the attempt is recorded and regardless of the outcome:
        # computing it only on success made the exemption useless, since it is FAILED
        # attempts that lock an account — and check_and_record_attempt short-circuits
        # for exempt callers, so it never cleared the counter on success either.
        exempt_from_lockout = _is_exempt_from_lockout(db, username, user_uuid_str)

        # Handle lockout check and recording
        is_locked, _ = _handle_lockout_check(
            username,
            auth_success,
            client_ip,
            user_agent,
            exempt_from_lockout=exempt_from_lockout,
            auth_method=actual_auth_method,
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

        # Password expiry (FedRAMP IA-5(1)). Evaluated before the MFA branch so the
        # flag is set even when the caller still owes a second factor — it is the
        # dependency gate, not this response, that confines them afterwards.
        _apply_password_expiry(db, user_db, actual_auth_method, client_ip, user_agent)

        # FedRAMP AC-10: Enforce concurrent session limit
        if settings.MAX_CONCURRENT_SESSIONS > 0:
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
        return _generate_login_tokens(
            db,
            user_db,
            user_uuid_str,
            user_role,
            user_agent,
            client_ip,
            auth_method=actual_auth_method,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error during authentication: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred during authentication",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
