import logging
import os
import secrets
from datetime import UTC
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError
from jose import jwt
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_KEYCLOAK
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import AUTH_TYPE_PKI
from app.auth.constants import VALID_AUTH_TYPES
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.direct_auth import direct_authenticate_user
from app.auth.keycloak_auth import KeycloakConfig
from app.auth.keycloak_auth import call_keycloak_logout
from app.auth.keycloak_auth import exchange_code_for_tokens
from app.auth.keycloak_auth import get_authorization_url
from app.auth.keycloak_auth import sync_keycloak_user_to_db
from app.auth.keycloak_auth import validate_token as validate_keycloak_token
from app.auth.ldap_auth import ldap_authenticate
from app.auth.ldap_auth import sync_ldap_user_to_db
from app.auth.lockout import check_and_record_attempt
from app.auth.lockout import get_lockout_info
from app.auth.mfa import MFAService
from app.auth.password_history import add_password_to_history
from app.auth.pki_auth import pki_authenticate
from app.auth.pki_auth import sync_pki_user_to_db
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.session import OIDCStateStore
from app.auth.session import get_redis_client
from app.auth.token_service import token_service
from app.core.auth_settings import get_auth_settings
from app.core.config import settings
from app.core.security import authenticate_user
from app.core.security import get_password_hash
from app.db.base import get_db
from app.models.user import User
from app.models.user_mfa import UserMFA
from app.schemas.user import LoginBannerResponse
from app.schemas.user import MFADisableRequest
from app.schemas.user import MFASetupResponse
from app.schemas.user import MFAStatusResponse
from app.schemas.user import MFAVerifyRequest
from app.schemas.user import MFAVerifyResponse
from app.schemas.user import MFAVerifySetupRequest
from app.schemas.user import MFAVerifySetupResponse
from app.schemas.user import Token
from app.schemas.user import TokenPayload
from app.schemas.user import TokenRefreshRequest
from app.schemas.user import User as UserSchema
from app.schemas.user import UserCreate

router = APIRouter()
logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_PREFIX}/auth/token", auto_error=False)


# Pydantic request bodies for password reset endpoints
class PasswordResetRequestBody(PydanticBaseModel):
    email: str = ""


class PasswordResetConfirmBody(PydanticBaseModel):
    token: str = ""
    new_password: str = ""


def _get_client_info(request: Request) -> tuple[str, str]:
    """Extract client IP and user agent from request.

    Args:
        request: FastAPI request object

    Returns:
        Tuple of (client_ip, user_agent)
    """
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("User-Agent", "unknown")
    return client_ip, user_agent


def _authenticate_external_token(request: Request, token: str, db: Session) -> User | None:
    """Resolve a bearer token via the external-verifier seam (cloud edition).

    Returns the JIT-synced user when a registered verifier claims the token,
    or ``None`` to fall through to the local-JWT path. Sync failures — a
    refused link (unverified email match / protected account) or a DB error —
    surface as clean 401s, matching the optional-auth and WebSocket branches'
    containment.
    """
    from app.auth.provider_registry import has_verifiers

    if not has_verifiers():
        return None

    from app.auth.external_sync import sync_external_user_to_db
    from app.auth.provider_registry import verify_external_token

    external_identity = verify_external_token(token, request)
    if external_identity is None:
        return None

    try:
        external_user = sync_external_user_to_db(db, external_identity)
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except Exception:
        logger.exception("External JIT sync failed; rejecting credential")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not provision external identity",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if not external_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    # Stash for get_current_context() so org scoping doesn't re-verify.
    request.state.external_identity = external_identity
    # Cloud contract: the access log / observability layer reads these
    # off request.state. user_id is always set; org_id is the provider's
    # raw org string here and is refined to our local Organization.id in
    # deps_context.get_current_context() when org context applies. Never
    # used as a Prometheus label — access log only.
    request.state.user_id = external_user.id
    request.state.org_id = getattr(external_user, "external_org_id", None)
    return external_user


def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Get the current user from JWT token (Bearer header or httpOnly cookie).

    Checks Bearer header first (for API clients, Swagger UI), then falls
    back to the httpOnly access_token cookie (browser frontend).

    When TOKEN_REVOCATION_ENABLED is true, also checks if the token's JTI
    is on the revocation blacklist (FedRAMP AC-12 compliance).
    """
    from app.auth.cookies import get_access_token_from_cookie

    # Try Bearer header first, then fall back to httpOnly cookie
    if not token:
        token = get_access_token_from_cookie(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Cloud-edition seam: offer the token to registered external verifiers
    # before the local-JWT path. The community edition registers none, so this
    # is a no-op there.
    external_user = _authenticate_external_token(request, token, db)
    if external_user is not None:
        return external_user

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_uuid_str: str = payload.get("sub")  # UUID string from token
        user_role: str = payload.get("role")  # Extract role from token
        token_jti: str = payload.get("jti")  # JWT ID for revocation checking
        if user_uuid_str is None:
            raise credentials_exception

        # Check token revocation blacklist (FedRAMP AC-12)
        if (
            settings.TOKEN_REVOCATION_ENABLED
            and token_jti
            and token_service.is_token_revoked(token_jti)
        ):
            logger.warning(f"Rejected revoked token (jti={token_jti[:8]}...)")
            raise credentials_exception

        # Validate UUID format
        try:
            user_uuid = UUID(user_uuid_str)
        except ValueError:
            raise credentials_exception from None

        token_data = TokenPayload(sub=user_uuid_str, jti=token_jti)
    except JWTError as e:
        raise credentials_exception from e

    try:
        # Look up user by UUID (indexed for performance)
        user = db.query(User).filter(User.uuid == user_uuid).first()
        if user is None:
            raise credentials_exception
        if not user.is_active:
            raise HTTPException(status_code=400, detail="Inactive user")

        # Database is the source of truth for roles - do NOT update DB from token
        # If role mismatch, the token may be stale (user should re-login)
        # This prevents privilege escalation via token manipulation
        if user_role and user.role != user_role:
            logger.warning(
                f"Role mismatch for user {user.id}: token has '{user_role}', "
                f"DB has '{user.role}'. Using DB role. User should re-login."
            )

        # Cloud contract: observability/access-log reads these off request.state.
        # org_id is None for self-hosted/local users; deps_context refines it to
        # our local Organization.id when org context applies. Access log only —
        # never a Prometheus label.
        request.state.user_id = user.id
        request.state.org_id = getattr(user, "external_org_id", None)
        return user  # type: ignore[no-any-return]
    except Exception as e:
        # Handle database connection errors or other issues
        logger.error(f"Error retrieving user: {e}")
        # In testing environment, we can create a mock user with the UUID from the token
        testing_environment = os.environ.get("TESTING", "False").lower() == "true"
        if testing_environment:
            logger.info(f"Creating mock user for testing with uuid {token_data.sub}")
            # For tests, create a basic user object with the UUID from the token
            user = User(
                uuid=UUID(token_data.sub),
                email="test@example.com",
                is_active=True,
                is_superuser=False,
            )
            return user  # type: ignore[no-any-return]
        # Re-raise the exception in production
        raise


def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Check if the current user is active
    """
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    """Get the current user if a valid token is provided, otherwise return None.

    Checks Bearer header first, then falls back to httpOnly cookie.
    Used for endpoints that support both authenticated and unauthenticated
    access (e.g., public files can be accessed without auth, private files require auth).

    Returns:
        User object if valid token provided, None otherwise
    """
    from app.auth.cookies import get_access_token_from_cookie

    # Check for Authorization header first
    authorization = request.headers.get("Authorization")
    token: str | None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
    else:
        # Fall back to httpOnly cookie
        token = get_access_token_from_cookie(request)

    if not token:
        return None

    # Cloud-edition seam: offer the token to registered external verifiers
    # before the local-JWT path, mirroring get_current_user. The community
    # edition registers none, so this branch is a no-op there and behavior is
    # identical. Optional semantics are preserved: an inactive or missing
    # external user returns None rather than raising.
    from app.auth.provider_registry import has_verifiers

    if has_verifiers():
        from app.auth.external_sync import sync_external_user_to_db
        from app.auth.provider_registry import verify_external_token

        try:
            external_identity = verify_external_token(token, request)
            if external_identity is not None:
                external_user = sync_external_user_to_db(db, external_identity)
                if not external_user.is_active:
                    return None
                # Stash for resolve_org_context()/get_current_context() so org
                # scoping (e.g. thumbnail org-resolution) works for external
                # users on optional-auth routes without re-verifying the token.
                request.state.external_identity = external_identity
                request.state.user_id = external_user.id
                request.state.org_id = getattr(external_user, "external_org_id", None)
                return external_user
        except Exception as e:
            # Never let an external-auth problem fail an optional-auth route;
            # fall through to local JWT (and ultimately None).
            logger.debug(f"Error in optional external auth: {e}")

    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_uuid_str: str = payload.get("sub")
        token_jti: str = payload.get("jti")

        if user_uuid_str is None:
            return None

        # Check token revocation blacklist
        if (
            settings.TOKEN_REVOCATION_ENABLED
            and token_jti
            and token_service.is_token_revoked(token_jti)
        ):
            return None

        # Validate UUID format
        try:
            user_uuid = UUID(user_uuid_str)
        except ValueError:
            return None

        # Look up user by UUID
        user = db.query(User).filter(User.uuid == user_uuid).first()
        if user is None or not user.is_active:
            return None

        return user  # type: ignore[no-any-return]

    except JWTError:
        return None
    except Exception as e:
        logger.debug(f"Error in optional auth: {e}")
        return None


def get_current_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Check if the current user is an admin or super_admin
    """
    if current_user.role not in ("admin", "super_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )
    return current_user


def get_current_active_superuser(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Check if the current user is a platform super_admin.

    role is the authorization source of truth; is_superuser is its derived
    mirror (role == super_admin). We gate on role so this stays consistent
    with the rest of the super_admin-tier endpoints.
    """
    if current_user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions - super_admin required",
        )
    return current_user


def _authenticate_testing_user(db: Session, username: str, password: str) -> str:
    """Authenticate user in testing environment.

    Args:
        db: Database session
        username: Username to authenticate
        password: Password to verify

    Returns:
        User UUID string

    Raises:
        HTTPException: If authentication fails
    """
    logger.info(f"Testing environment detected, using ORM auth for: {username}")
    user = authenticate_user(db, username, password)

    if not user:
        logger.warning(f"Failed login attempt for user: {username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        logger.warning(f"Login attempt for inactive user: {username}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    return str(user.uuid)  # Return UUID string for token


def _authenticate_ldap_user(db: Session, username: str, password: str) -> tuple[str, dict]:
    """Authenticate user via LDAP/Active Directory.

    Args:
        db: Database session
        username: Username to authenticate
        password: Password to verify

    Returns:
        Tuple of (user_uuid_string, user_data_dict)

    Raises:
        HTTPException: If authentication fails
    """
    # Check DB config first, fall back to .env
    from app.core.auth_settings import get_auth_settings

    auth_settings = get_auth_settings(db)
    if not auth_settings.ldap_enabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LDAP authentication is not enabled",
            headers={"WWW-Authenticate": "Bearer"},
        )

    ldap_user = ldap_authenticate(username, password, db=db)

    if not ldap_user:
        logger.warning(f"LDAP authentication failed for user: {username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.info(f"LDAP authentication successful for user: {username}")

    # Sync LDAP user to database
    user = sync_ldap_user_to_db(db, ldap_user)

    if not user.is_active:
        logger.warning(f"LDAP user account is inactive: {username}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    user_data = {
        "uuid": str(user.uuid),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "is_superuser": user.is_superuser,
    }

    return str(user.uuid), user_data


def _build_user_data(user: User) -> dict:
    """Build user data dictionary from User object.

    Args:
        user: User model object

    Returns:
        Dictionary with user data for token generation
    """
    return {
        "uuid": str(user.uuid),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "is_superuser": user.is_superuser,
    }


def _ensure_user_uuid(db: Session, user_data: dict) -> str:
    """Ensure user_data has UUID, looking up from DB if needed.

    Args:
        db: Database session
        user_data: User data dict (may have 'uuid' or 'id')

    Returns:
        User UUID string

    Raises:
        HTTPException: If user not found
    """
    if "uuid" in user_data:
        return str(user_data["uuid"])

    # Direct auth returned integer ID, look up UUID
    user = db.query(User).filter(User.id == user_data["id"]).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    user_data["uuid"] = str(user.uuid)
    return str(user.uuid)


def _check_user_active(user_data: dict, username: str) -> None:
    """Check if user is active, raise exception if not.

    Args:
        user_data: User data dict with 'is_active' field
        username: Username for logging

    Raises:
        HTTPException: If user is inactive
    """
    if not user_data.get("is_active", True):
        logger.warning(f"Login attempt for inactive local user: {username}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )


def _authenticate_local_user(db: Session, username: str, password: str) -> tuple[str, dict] | None:
    """Authenticate local user via direct auth or ORM.

    Args:
        db: Database session
        username: Username to authenticate
        password: Password to verify

    Returns:
        Tuple of (uuid_str, user_data) if successful, None otherwise

    Raises:
        HTTPException: If user is inactive
    """
    # Try direct auth first
    user_data = direct_authenticate_user(username, password)
    if user_data:
        logger.info(f"Direct authentication successful for local user: {username}")
        user_uuid_str = _ensure_user_uuid(db, user_data)
        _check_user_active(user_data, username)
        return user_uuid_str, user_data

    # Fall back to ORM-based auth
    logger.info(f"Direct auth failed, trying ORM auth for local user: {username}")
    user = authenticate_user(db, username, password)
    if not user:
        return None

    if not user.is_active:
        logger.warning(f"Login attempt for inactive local user: {username}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    return str(user.uuid), _build_user_data(user)


def _authenticate_production_user(
    db: Session, username: str, password: str
) -> tuple[str, dict, str]:
    """Authenticate user in production environment.

    Hybrid authentication:
    1. Try local authentication (database password) — includes users with allow_local_fallback
    2. If enabled, try LDAP authentication

    Args:
        db: Database session
        username: Username to authenticate
        password: Password to verify

    Returns:
        Tuple of (user_uuid_string, user_data_dict, actual_auth_method)
        actual_auth_method is "local", "ldap", etc. — describes how the user
        actually authenticated (may differ from user.auth_type for fallback logins).

    Raises:
        HTTPException: If authentication fails
    """
    # Check if user exists in database by username (ldap_uid or email)
    local_user = (
        db.query(User).filter((User.email == username) | (User.ldap_uid == username)).first()
    )

    # Determine if user can use local (password) authentication
    can_use_local_auth = local_user and (
        local_user.auth_type == AUTH_TYPE_LOCAL
        or getattr(local_user, "allow_local_fallback", False)
    )

    # If user can use local auth, try it first
    if can_use_local_auth:
        result = _authenticate_local_user(db, username, password)
        if result:
            return result[0], result[1], AUTH_TYPE_LOCAL
        # Local auth failed, try LDAP as fallback
        logger.info(f"Local auth failed for {username}, trying LDAP as fallback")
        uuid_str, data = _authenticate_ldap_user(db, username, password)
        return uuid_str, data, "ldap"

    # Try LDAP authentication
    try:
        uuid_str, data = _authenticate_ldap_user(db, username, password)
        return uuid_str, data, "ldap"
    except HTTPException:
        # LDAP failed, try local auth as fallback if user exists
        if not local_user:
            raise

        logger.info(f"LDAP failed, trying local auth as fallback for: {username}")
        result = _authenticate_local_user(db, str(local_user.email), password)
        if result:
            return result[0], result[1], AUTH_TYPE_LOCAL

        # All authentication methods failed
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


def _get_user_role(db: Session, user_uuid_str: str, user_data: dict | None = None) -> str:
    """Get user role for token generation.

    Args:
        db: Database session
        user_uuid_str: User UUID string
        user_data: Optional user data from direct auth

    Returns:
        User role string
    """
    if user_data and "role" in user_data:
        return str(user_data["role"])

    # Get role from database if not available in direct auth
    user_uuid = UUID(user_uuid_str)
    user_db = db.query(User).filter(User.uuid == user_uuid).first()
    return str(user_db.role) if user_db else ""


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
    testing_environment = os.environ.get("TESTING", "False").lower() == "true"

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


@router.post("/register", response_model=UserSchema)
def register(request: Request, user_in: UserCreate, db: Session = Depends(get_db)):
    """
    Register a new user.

    Password must meet the configured password policy requirements (FedRAMP IA-5):
    - Minimum length (default: 12 characters)
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    - Cannot contain email username or name parts

    Note: When LDAP is enabled, local registration is still allowed for admin accounts.
    Regular users should use LDAP authentication.
    """
    # Check if email already exists
    user_exists = db.query(User).filter(User.email == user_in.email).first()

    if user_exists:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Note: Password validation is performed by UserCreate schema's model_validator
    # which calls validate_password() from password_policy module

    # Hash the password
    from datetime import datetime

    password_hash = get_password_hash(user_in.password)

    # Create new user with local authentication
    db_user = User(
        email=user_in.email,
        full_name=user_in.full_name,
        hashed_password=password_hash,
        role="user",
        auth_type=AUTH_TYPE_LOCAL,
        is_active=True,
        is_superuser=False,
        password_changed_at=datetime.now(UTC),  # Track initial password time
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    # Product metric: local self-registration (API process). External/JIT
    # signups are counted in app.auth.external_sync.
    from app.core.metrics import user_signups_total

    user_signups_total.labels(method=AUTH_TYPE_LOCAL).inc()

    # Store initial password in history (FedRAMP IA-5)
    add_password_to_history(db, db_user.id, password_hash)
    db.commit()

    # Log user registration
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log_admin_action(
        event_type=AuditEventType.ADMIN_USER_CREATE,
        admin_user_id=db_user.id,  # Self-registration
        admin_username=str(db_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"registration_type": "self", "auth_type": AUTH_TYPE_LOCAL},
    )

    logger.info(
        f"New local user registered: {str(db_user.email)} (auth_type={db_user.auth_type}, role={db_user.role})"
    )
    return db_user


@router.get("/password-policy")
def get_password_policy():
    """
    Get the current password policy requirements.

    Returns the configured password policy settings so the frontend can
    display requirements to users during registration and password changes.

    This endpoint is public (no authentication required) to allow displaying
    requirements on registration forms.
    """
    from app.auth.password_policy import get_policy_requirements

    return get_policy_requirements()


# ============== Password Reset (Self-Service Recovery) ==============


@router.post("/password-reset/request")
@limiter.limit(get_auth_rate_limit())
def request_password_reset_endpoint(
    request: Request,
    body: "PasswordResetRequestBody",
    db: Session = Depends(get_db),
):
    """Request a password reset link.

    Always returns 200 regardless of whether the email exists to prevent
    email enumeration attacks. Rate limited per IP.
    """
    from app.auth.password_reset import request_password_reset

    client_ip = request.client.host if request.client else "unknown"
    request_password_reset(db, body.email, client_ip)

    return {"message": "If that email address is registered, you will receive a reset link."}


@router.post("/password-reset/confirm")
@limiter.limit(get_auth_rate_limit())
def confirm_password_reset_endpoint(
    request: Request,
    body: "PasswordResetConfirmBody",
    db: Session = Depends(get_db),
):
    """Confirm a password reset with token and new password.

    Validates the token and sets the new password. Returns 400 if the
    token is invalid or expired. Rate limited per IP.
    """
    from app.auth.password_reset import confirm_password_reset

    ok, errors = confirm_password_reset(db, body.token, body.new_password)
    if not ok:
        detail = errors[0] if errors else "Invalid or expired reset token"
        raise HTTPException(status_code=400, detail=detail)

    return {"message": "Password has been reset successfully."}


@router.get("/me", response_model=UserSchema, summary="Get current user")
def read_users_me(current_user: User = Depends(get_current_user)):
    """
    Get current user using the current_user dependency
    """
    return current_user


@router.get("/session", summary="Cookie-session status probe (never 401s)")
def session_status(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> dict:
    """Report whether the caller has a valid session — with 200 either way.

    The SPA's initAuth probes this on every page load. Anonymous visitors
    must not receive a 401 (the browser logs every 401 as a console error
    and the old /auth/me probe triggered a spurious logout cascade).

    ``refreshable`` is true when no valid access token was presented but a
    refresh_token cookie is — the client should attempt one silent refresh
    before treating the visitor as logged out.
    """
    from app.auth.cookies import get_refresh_token_from_cookie

    try:
        user = get_current_user(request, token=token, db=db)
        return {
            "authenticated": True,
            "refreshable": False,
            "user": UserSchema.model_validate(user).model_dump(mode="json"),
        }
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        return {
            "authenticated": False,
            "refreshable": get_refresh_token_from_cookie(request) is not None,
            "user": None,
        }


@router.get("/me/certificate", summary="Get current user's certificate info")
def get_user_certificate_info(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get certificate information for the current user.

    Returns certificate metadata for users who authenticated via PKI (X.509)
    or via Keycloak with X.509 certificate authentication.

    For non-PKI users, returns has_certificate: false.

    Certificate metadata includes:
    - subject_dn: Full Distinguished Name from certificate
    - serial_number: Certificate serial number (hex format)
    - issuer_dn: Certificate issuer Distinguished Name
    - organization: Organization from certificate subject
    - organizational_unit: Organizational unit from certificate subject
    - valid_from: Certificate validity start date (ISO format)
    - valid_until: Certificate validity end date (ISO format)
    - fingerprint: SHA-256 fingerprint (colon-separated hex)
    """
    # Check if user has certificate metadata stored
    has_cert_metadata = bool(
        current_user.pki_subject_dn
        or current_user.pki_fingerprint_sha256
        or current_user.pki_serial_number
    )

    if not has_cert_metadata:
        return {"has_certificate": False}

    # Format fingerprint with colons for display
    fingerprint_formatted = None
    if current_user.pki_fingerprint_sha256:
        fp = str(current_user.pki_fingerprint_sha256)
        fingerprint_formatted = ":".join(fp[i : i + 2] for i in range(0, len(fp), 2)).upper()

    return {
        "has_certificate": True,
        "subject_dn": current_user.pki_subject_dn,
        "common_name": current_user.pki_common_name,
        "serial_number": current_user.pki_serial_number,
        "issuer_dn": current_user.pki_issuer_dn,
        "organization": current_user.pki_organization,
        "organizational_unit": current_user.pki_organizational_unit,
        "valid_from": current_user.pki_not_before.isoformat()
        if current_user.pki_not_before
        else None,
        "valid_until": current_user.pki_not_after.isoformat()
        if current_user.pki_not_after
        else None,
        "fingerprint": fingerprint_formatted,
    }


# ============== OIDC/Keycloak Authentication ==============


# Redis-backed OIDC state store with max state limit (prevents state exhaustion attacks)
# Uses Redis for distributed deployments with automatic in-memory fallback
_oidc_state_store = OIDCStateStore()

# OIDC state expiry time in seconds (10 minutes)
_OIDC_STATE_EXPIRY_SECONDS = 600


@router.get("/keycloak/login")
async def keycloak_login(db: Session = Depends(get_db)):
    """
    Initiate Keycloak OIDC login flow.

    Returns an authorization URL that the frontend should redirect to.
    Supports PKCE (RFC 7636) for OAuth 2.1 compliance when enabled.

    Security:
    - Uses Redis-backed state storage for distributed deployments
    - Enforces maximum state count to prevent state exhaustion attacks
    - States expire after 10 minutes and are single-use
    """
    # Load Keycloak config from database (DB > .env > defaults)
    kc_cfg = KeycloakConfig.from_db(db)
    if not kc_cfg.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keycloak authentication is not enabled",
        )

    # Generate CSRF protection state
    state = secrets.token_urlsafe(32)

    # Generate authorization URL (with PKCE if enabled)
    authorization_url, code_verifier = get_authorization_url(state, cfg=kc_cfg)

    # Store state with PKCE verifier in Redis-backed store
    # Returns False if state limit exceeded (prevents exhaustion attack)
    state_data = {"code_verifier": code_verifier} if code_verifier else {}
    stored = _oidc_state_store.store_state(
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

    # Validate token and get user data
    keycloak_data = await validate_keycloak_token(tokens.access_token, cfg=kc_cfg)
    if not keycloak_data:
        logger.error("Invalid access token received from Keycloak")
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


# ============== PKI/X.509 Certificate Authentication ==============


@router.post("/pki/authenticate", response_model=Token)
async def pki_login(request: Request, db: Session = Depends(get_db)):
    """
    Authenticate via X.509 client certificate.

    The reverse proxy (Nginx) must be configured to pass the client
    certificate information via headers (X-Client-Cert or X-Client-Cert-DN).
    """
    # Check database settings first, then fall back to .env
    auth_settings = get_auth_settings(db)
    pki_enabled = auth_settings.pki_enabled or settings.PKI_ENABLED

    if not pki_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PKI authentication is not enabled",
        )

    client_ip, user_agent = _get_client_info(request)

    pki_data = pki_authenticate(request, admin_dns_config=auth_settings.pki_admin_dns)
    if not pki_data:
        logger.warning("PKI authentication failed - invalid or missing certificate")
        # Log PKI login failure
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_CERTIFICATE",
            auth_method="pki",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing client certificate",
            headers={"WWW-Authenticate": "Certificate"},
        )

    # Sync user to database
    user = sync_pki_user_to_db(db, pki_data)

    if not user.is_active:
        logger.warning(f"PKI user account is inactive: {pki_data['subject_dn']}")
        # Log PKI login failure for inactive user
        audit_logger.log_login_failure(
            username=pki_data.get("subject_dn", "unknown"),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INACTIVE_USER",
            auth_method="pki",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    # Generate JWT token
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": str(user.uuid), "role": user.role}
    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    # Log PKI login success
    audit_logger.log_login_success(
        user_id=user.id,
        username=user.email,
        source_ip=client_ip,
        user_agent=user_agent,
        auth_method="pki",
    )

    logger.info(f"PKI authentication successful for user: {pki_data['subject_dn']}")

    # Generate refresh token for PKI users too
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


# ============== Authentication Methods Discovery ==============


@router.get("/methods")
async def get_auth_methods(db: Session = Depends(get_db)):
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


# ============== Login Banner (FedRAMP AC-8) ==============


@router.get("/banner", response_model=LoginBannerResponse)
async def get_login_banner():
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
async def acknowledge_banner(
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


# ============== MFA/TOTP Authentication (FedRAMP IA-2) ==============


def _user_can_setup_mfa(user: User) -> bool:
    """Check if user is eligible for local TOTP MFA setup.

    Local TOTP MFA only applies to users whose identity this app owns. Users
    authenticated by an external identity provider get their MFA from that IdP:

    - PKI: smart card is already two-factor (something you have + PIN).
    - Keycloak: MFA is handled by the identity provider.
    - Any registry-based external/SSO provider (e.g. a cloud-edition IdP): MFA
      and auth are owned by the provider, so a redundant local TOTP is excluded.

    The check is generic (no provider names beyond the core IdPs): an auth_type
    that is not one of the core types this app enumerates is treated as an
    external/SSO provider and excluded from local MFA setup.

    Args:
        user: User model object

    Returns:
        bool: True if user can set up local MFA
    """
    # Explicit core IdPs whose MFA is handled externally.
    if user.auth_type in (AUTH_TYPE_PKI, AUTH_TYPE_KEYCLOAK):
        return False
    # Any auth_type not in the core set is an external/registry-based SSO
    # provider (cloud edition) — local TOTP would be redundant.
    return user.auth_type in VALID_AUTH_TYPES


def _is_mfa_enabled(db: Session) -> bool:
    """Check if MFA is enabled via database auth_config (primary) or .env fallback."""
    auth_settings = get_auth_settings(db)
    return auth_settings.mfa_enabled or settings.MFA_ENABLED


def _is_mfa_required(db: Session) -> bool:
    """Check if MFA is required via database auth_config (primary) or .env fallback."""
    auth_settings = get_auth_settings(db)
    return auth_settings.get_bool("mfa_required", settings.MFA_REQUIRED) and _is_mfa_enabled(db)


# Redis key prefix for MFA token JTI blacklist (not a password)
MFA_TOKEN_BLACKLIST_PREFIX = "mfa:jti:"  # noqa: S105 # nosec B105


def _blacklist_mfa_token(jti: str, expires_seconds: int) -> bool:
    """Add an MFA token JTI to the blacklist after successful verification.

    This ensures MFA tokens are single-use (cannot be replayed).

    Args:
        jti: JWT ID to blacklist
        expires_seconds: Time in seconds until the token naturally expires

    Returns:
        bool: True if blacklisted successfully, False otherwise

    Raises:
        HTTPException: 503 if Redis unavailable and MFA_REQUIRE_REDIS is True
    """
    try:
        redis_client = get_redis_client()
        if redis_client:
            key = f"{MFA_TOKEN_BLACKLIST_PREFIX}{jti}"
            redis_client.set(key, "1", ex=expires_seconds)
            logger.debug(f"MFA token JTI blacklisted: {jti[:8]}...")
            return True
        else:
            # No Redis available - check fail-secure mode
            if settings.MFA_REQUIRE_REDIS:
                logger.error("Redis not available for MFA token blacklisting (fail-secure mode)")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Auth service unavailable",
                )
            else:
                # Fail-open mode: log warning but allow operation
                logger.warning("Redis not available for MFA token blacklisting")
                return False
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to blacklist MFA token JTI: {e}")
        if settings.MFA_REQUIRE_REDIS:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth service unavailable",
            ) from e
        return False


def _is_mfa_token_blacklisted(jti: str) -> bool:
    """Check if an MFA token JTI is in the blacklist.

    Args:
        jti: JWT ID to check

    Returns:
        bool: True if token is blacklisted (already used), False otherwise.
               In fail-secure mode (MFA_REQUIRE_REDIS=True), returns True if Redis unavailable.
    """
    try:
        redis_client = get_redis_client()
        if redis_client:
            key = f"{MFA_TOKEN_BLACKLIST_PREFIX}{jti}"
            return bool(redis_client.exists(key) > 0)
        else:
            # No Redis available - check fail-secure mode
            if settings.MFA_REQUIRE_REDIS:
                # Fail-secure: assume token is blacklisted (deny access)
                logger.error("Redis not available for MFA token blacklist check (fail-secure mode)")
                return True
            else:
                # Fail-open mode: allow operation but log warning
                logger.warning("Redis not available for MFA token blacklist check")
                return False
    except Exception as e:
        logger.error(f"Failed to check MFA token blacklist: {e}")
        # Fail-secure when Redis required (deny access), fail-open otherwise
        return bool(settings.MFA_REQUIRE_REDIS)


def _create_mfa_token(user_uuid_str: str, user_role: str) -> str:
    """Create a short-lived MFA token for the MFA verification step.

    This token can only be used to verify MFA once (single-use via JTI blacklist).
    After successful MFA verification, the JTI is added to Redis blacklist.

    Args:
        user_uuid_str: User UUID string
        user_role: User's role

    Returns:
        str: Short-lived MFA token with unique JTI
    """
    import uuid as uuid_mod
    from datetime import datetime

    mfa_token_expires = timedelta(minutes=settings.MFA_TOKEN_EXPIRE_MINUTES)
    now = datetime.now(UTC)
    expire = now + mfa_token_expires

    mfa_token_data = {
        "sub": user_uuid_str,
        "role": user_role,
        "type": "mfa",  # Mark this as an MFA-only token
        "jti": str(uuid_mod.uuid4()),  # Unique JWT ID for single-use enforcement
        "iat": now,
        "exp": expire,
    }

    # Create token manually since we need to include jti
    encoded_jwt: str = jwt.encode(
        mfa_token_data, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )
    return encoded_jwt


def _verify_mfa_token(mfa_token: str) -> tuple[str, str, str]:
    """Verify an MFA token and extract user information.

    Checks that the token is valid, is an MFA-type token, and has not been
    previously used (via JTI blacklist check).

    Args:
        mfa_token: The MFA token to verify

    Returns:
        tuple[str, str, str]: (user_uuid_str, user_role, jti)

    Raises:
        HTTPException: If token is invalid, not an MFA token, or already used
    """
    try:
        payload = jwt.decode(
            mfa_token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )

        # Verify this is an MFA token, not a regular access token
        if payload.get("type") != "mfa":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid MFA token",
            )

        user_uuid_str = payload.get("sub")
        user_role = payload.get("role")
        jti = payload.get("jti")

        if not user_uuid_str:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid MFA token",
            )

        # Check if this MFA token has already been used (JTI blacklist)
        if jti and _is_mfa_token_blacklisted(jti):
            logger.warning(f"MFA token already used (jti={jti[:8]}...)")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA token has already been used",
            )

        return user_uuid_str, user_role, jti

    except JWTError as e:
        logger.warning(f"MFA token verification failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA token",
        ) from e


def _get_user_for_mfa(db: Session, mfa_token: str) -> tuple[User, UserMFA, str, str, str]:
    """Verify MFA token and get user with MFA record.

    Args:
        db: Database session
        mfa_token: The MFA token from login

    Returns:
        Tuple of (user, user_mfa, user_uuid_str, user_role, mfa_jti)

    Raises:
        HTTPException: If token invalid, user not found, or MFA not enabled
    """
    # Verify the MFA token (also checks JTI blacklist for replay prevention)
    user_uuid_str, user_role, mfa_jti = _verify_mfa_token(mfa_token)

    # Get user from database
    user_uuid = UUID(user_uuid_str)
    user = db.query(User).filter(User.uuid == user_uuid).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    # Get user's MFA record
    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == user.id).first()

    if not user_mfa or not user_mfa.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled for this user",
        )

    return user, user_mfa, user_uuid_str, user_role, mfa_jti


def _verify_mfa_code(
    code: str,
    decrypted_secret: str,
    backup_codes: list[str],
    db: Session,
    user_mfa: UserMFA,
    user_email: str,
) -> tuple[bool, bool]:
    """Verify TOTP or backup code.

    Args:
        code: The MFA code (TOTP or backup)
        decrypted_secret: Decrypted TOTP secret
        backup_codes: List of hashed backup codes
        db: Database session
        user_mfa: User's MFA record (for updating backup codes)
        user_email: User's email (for logging)

    Returns:
        Tuple of (is_valid, used_backup_code)
    """
    # Normalize code (remove dashes and spaces)
    normalized_code = code.replace("-", "").replace(" ", "")
    is_valid = False
    used_backup_code = False

    # Try TOTP verification first (6 digits)
    if len(normalized_code) == 6 and normalized_code.isdigit():
        is_valid = MFAService.verify_totp(decrypted_secret, normalized_code)

    # Try backup code verification (8 characters)
    if not is_valid:
        is_valid, matched_hash = MFAService.verify_backup_code(code, backup_codes)
        if is_valid and matched_hash:
            # Remove used backup code
            backup_codes_list: list[str] = list(user_mfa.backup_codes)
            user_mfa.backup_codes = [c for c in backup_codes_list if c != matched_hash]  # type: ignore[assignment]
            used_backup_code = True
            db.commit()
            logger.info(
                f"Backup code used for user: {user_email}. "
                f"{len(user_mfa.backup_codes)} codes remaining."
            )

    return is_valid, used_backup_code


def _complete_mfa_verification(
    db: Session,
    user: User,
    user_mfa: UserMFA,
    user_uuid_str: str,
    user_role: str,
    mfa_jti: str,
    used_backup_code: bool,
    client_ip: str,
    user_agent: str,
) -> JSONResponse:
    """Finalize MFA verification and generate tokens.

    Args:
        db: Database session
        user: User model object
        user_mfa: User's MFA record
        user_uuid_str: User UUID string
        user_role: User's role
        mfa_jti: MFA token JTI (for blacklisting)
        used_backup_code: Whether a backup code was used
        client_ip: Client IP address
        user_agent: Client user agent

    Returns:
        JSONResponse with access_token, refresh_token, and token metadata
    """
    from datetime import datetime as dt

    # Update last verified timestamp
    user_mfa.last_verified_at = dt.now(UTC)  # type: ignore[assignment]
    db.commit()

    # Blacklist the MFA token JTI to prevent replay attacks
    if mfa_jti:
        mfa_token_ttl_seconds = settings.MFA_TOKEN_EXPIRE_MINUTES * 60
        _blacklist_mfa_token(mfa_jti, mfa_token_ttl_seconds)
        logger.debug(f"MFA token blacklisted after successful verification (jti={mfa_jti[:8]}...)")

    # Log MFA verification success (and backup code usage if applicable)
    if used_backup_code:
        audit_logger.log_mfa_event(
            event_type=AuditEventType.AUTH_MFA_BACKUP_USED,
            outcome=AuditOutcome.SUCCESS,
            user_id=user.id,
            username=str(user.email),
            source_ip=client_ip,
            user_agent=user_agent,
        )
    audit_logger.log_mfa_event(
        event_type=AuditEventType.AUTH_MFA_VERIFY,
        outcome=AuditOutcome.SUCCESS,
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    # Generate the full access token
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": user_uuid_str, "role": user_role, "type": "access"}
    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    # Generate refresh token
    refresh_token, _ = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=user_uuid_str,
        role=user_role,
        user_agent=user_agent,
        ip_address=client_ip,
    )

    logger.info(f"MFA verification successful for user: {str(user.email)}")

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


@router.get("/mfa/status", response_model=MFAStatusResponse)
def get_mfa_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get MFA status for the current user.

    Returns whether MFA is enabled/configured for the user and
    whether the system requires MFA.
    """
    # Check if user has MFA configured
    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == current_user.id).first()

    mfa_enabled = _is_mfa_enabled(db)

    return MFAStatusResponse(
        mfa_enabled=bool(user_mfa.totp_enabled) if user_mfa else False,
        mfa_configured=bool(user_mfa.totp_enabled) if user_mfa else False,
        mfa_required=_is_mfa_required(db),
        can_setup_mfa=mfa_enabled and _user_can_setup_mfa(current_user),
    )


@router.post("/mfa/setup", response_model=MFASetupResponse)
def setup_mfa(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Initiate MFA setup for the current user.

    Returns the TOTP secret, provisioning URI, and QR code for authenticator app setup.
    The user must verify with a valid TOTP code to complete setup.

    Note: This endpoint is only available when MFA is enabled and the user
    is not using PKI or Keycloak authentication (which handle MFA separately).
    """
    if not _is_mfa_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled on this system",
        )

    if not _user_can_setup_mfa(current_user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup is not available for your authentication type",
        )

    # Check if user already has MFA enabled
    existing_mfa = db.query(UserMFA).filter(UserMFA.user_id == current_user.id).first()
    if existing_mfa and existing_mfa.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is already enabled. Disable it first to reconfigure.",
        )

    # Generate new TOTP secret
    totp_secret = MFAService.generate_totp_secret()

    # Generate provisioning URI for authenticator apps (uses plaintext secret)
    provisioning_uri = MFAService.get_provisioning_uri(
        secret=totp_secret,
        email=str(current_user.email),
    )

    # Generate QR code
    qr_code_base64 = MFAService.generate_qr_code_base64(provisioning_uri)

    # Encrypt the TOTP secret before storing (CRITICAL-1: Encrypt at rest)
    encrypted_secret = MFAService.encrypt_totp_secret(totp_secret)

    # Store or update the MFA record (not yet enabled)
    if existing_mfa:
        existing_mfa.totp_secret = encrypted_secret  # type: ignore[assignment]
        existing_mfa.totp_enabled = False  # type: ignore[assignment]
        existing_mfa.backup_codes = []  # type: ignore[assignment]
    else:
        new_mfa = UserMFA(
            user_id=current_user.id,
            totp_secret=encrypted_secret,
            totp_enabled=False,
            backup_codes=[],
        )
        db.add(new_mfa)

    db.commit()

    # Log MFA setup initiated
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log_mfa_event(
        event_type=AuditEventType.AUTH_MFA_SETUP,
        outcome=AuditOutcome.PARTIAL,  # Setup initiated but not completed
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    logger.info(f"MFA setup initiated for user: {str(current_user.email)}")

    return MFASetupResponse(
        secret=totp_secret,
        provisioning_uri=provisioning_uri,
        qr_code_base64=qr_code_base64,
    )


@router.post("/mfa/verify-setup", response_model=MFAVerifySetupResponse)
def verify_mfa_setup(
    request: Request,
    request_body: MFAVerifySetupRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Verify MFA setup with the initial TOTP code.

    This completes the MFA setup process and generates backup codes.
    Backup codes are returned only once - the user must save them securely.
    """
    if not _is_mfa_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled on this system",
        )

    # Get user's MFA record
    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == current_user.id).first()

    if not user_mfa:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup not initiated. Please start setup first.",
        )

    if user_mfa.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is already enabled.",
        )

    # Decrypt the TOTP secret for verification (stored encrypted at rest)
    try:
        decrypted_secret = MFAService.decrypt_totp_secret(str(user_mfa.totp_secret))
    except ValueError as e:
        logger.error(f"Failed to decrypt TOTP secret for user: {str(current_user.email)}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify MFA setup. Please try again.",
        ) from e

    # Verify the TOTP code
    client_ip, user_agent = _get_client_info(request)
    if not MFAService.verify_totp(decrypted_secret, request_body.code):
        logger.warning(f"MFA setup verification failed for user: {str(current_user.email)}")
        # Log MFA setup verification failure
        audit_logger.log_mfa_event(
            event_type=AuditEventType.AUTH_MFA_SETUP,
            outcome=AuditOutcome.FAILURE,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_TOTP_CODE",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification code. Please try again.",
        )

    # Generate backup codes
    backup_codes = MFAService.generate_backup_codes()
    hashed_backup_codes = MFAService.hash_backup_codes(backup_codes)

    # Enable MFA and store hashed backup codes
    user_mfa.totp_enabled = True  # type: ignore[assignment]
    user_mfa.backup_codes = hashed_backup_codes  # type: ignore[assignment]
    db.commit()

    # Log MFA setup complete
    audit_logger.log_mfa_event(
        event_type=AuditEventType.AUTH_MFA_SETUP,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    logger.info(f"MFA enabled successfully for user: {str(current_user.email)}")

    return MFAVerifySetupResponse(
        success=True,
        backup_codes=backup_codes,  # Return plaintext codes only once
        message="MFA has been enabled successfully. Save your backup codes securely.",
    )


@router.post("/mfa/verify", response_model=MFAVerifyResponse)
@limiter.limit(get_auth_rate_limit())
def verify_mfa(
    request: Request,
    request_body: MFAVerifyRequest,
    db: Session = Depends(get_db),
):
    """
    Verify MFA code during login.

    This endpoint is called after successful password authentication when
    the user has MFA enabled. It accepts either a TOTP code or a backup code.

    Rate limited to prevent brute force attacks on TOTP codes.
    """
    if not _is_mfa_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled on this system",
        )

    # Get user and verify MFA token
    user, user_mfa, user_uuid_str, user_role, mfa_jti = _get_user_for_mfa(
        db, request_body.mfa_token
    )

    # Decrypt the TOTP secret for verification (stored encrypted at rest)
    try:
        decrypted_secret = MFAService.decrypt_totp_secret(str(user_mfa.totp_secret))
    except ValueError as e:
        logger.error(f"Failed to decrypt TOTP secret for user: {str(user.email)}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify MFA. Please contact support.",
        ) from e

    # Verify TOTP or backup code
    backup_codes_list: list[str] = list(user_mfa.backup_codes)
    is_valid, used_backup_code = _verify_mfa_code(
        request_body.code,
        decrypted_secret,
        backup_codes_list,
        db,
        user_mfa,
        str(user.email),
    )

    # Get client info for audit logging
    client_ip, user_agent = _get_client_info(request)

    if not is_valid:
        logger.warning(f"MFA verification failed for user: {str(user.email)}")
        audit_logger.log_mfa_event(
            event_type=AuditEventType.AUTH_MFA_VERIFY,
            outcome=AuditOutcome.FAILURE,
            user_id=user.id,
            username=str(user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_MFA_CODE",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid MFA code",
        )

    # Complete verification and generate tokens
    return _complete_mfa_verification(
        db,
        user,
        user_mfa,
        user_uuid_str,
        user_role,
        mfa_jti,
        used_backup_code,
        client_ip,
        user_agent,
    )


@router.post("/mfa/disable")
@limiter.limit(get_auth_rate_limit())
def disable_mfa(
    request: Request,
    request_body: MFADisableRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Disable MFA for the current user.

    Requires a valid TOTP code or backup code to confirm the action.
    """
    if not _is_mfa_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled on this system",
        )

    # Get user's MFA record
    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == current_user.id).first()

    if not user_mfa or not user_mfa.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled for your account",
        )

    code = request_body.code.replace("-", "").replace(" ", "")
    is_valid = False

    # Decrypt the TOTP secret for verification (stored encrypted at rest)
    try:
        decrypted_secret = MFAService.decrypt_totp_secret(str(user_mfa.totp_secret))
    except ValueError as e:
        logger.error(f"Failed to decrypt TOTP secret for user: {str(current_user.email)}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify MFA. Please contact support.",
        ) from e

    # Try TOTP verification first
    if len(code) == 6 and code.isdigit():
        is_valid = MFAService.verify_totp(decrypted_secret, code)

    # Try backup code verification
    if not is_valid:
        backup_codes_list: list[str] = list(user_mfa.backup_codes)
        is_valid, _ = MFAService.verify_backup_code(request_body.code, backup_codes_list)

    # Get client info for audit logging
    client_ip, user_agent = _get_client_info(request)

    if not is_valid:
        logger.warning(f"MFA disable attempt failed for user: {str(current_user.email)}")
        # Log MFA disable failure
        audit_logger.log_mfa_event(
            event_type=AuditEventType.AUTH_MFA_DISABLE,
            outcome=AuditOutcome.FAILURE,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_VERIFICATION_CODE",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid verification code",
        )

    # Delete the MFA record
    db.delete(user_mfa)
    db.commit()

    # Log MFA disabled
    audit_logger.log_mfa_event(
        event_type=AuditEventType.AUTH_MFA_DISABLE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    logger.info(f"MFA disabled for user: {str(current_user.email)}")

    return JSONResponse(content={"message": "MFA has been disabled successfully"})


# ============== Token Refresh & Revocation (FedRAMP AC-12) ==============


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
