"""Per-auth-method credential verification used by the login flow.

One function per ``User.auth_type`` (local / LDAP / testing) plus the
shared user-record shaping helpers.
"""

import logging
from uuid import UUID

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.direct_auth import direct_authenticate_user
from app.auth.ldap_auth import ldap_authenticate
from app.auth.ldap_auth import sync_ldap_user_to_db
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.utils import mask_identifier
from app.core.auth_settings import get_auth_settings
from app.core.security import authenticate_user
from app.models.user import User

logger = logging.getLogger(__name__)


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


def _local_auth_permitted(db: Session, user: User | None) -> bool:
    """Whether *user* may authenticate with a local password on this deployment.

    ``local_enabled`` is the deployment-level switch (DB > env > default true).
    An active ``super_admin`` is always permitted regardless — see the call site
    for why that exemption has to exist.

    Args:
        db: Database session, used to resolve the DB-backed setting.
        user: The resolved account, or None when the identifier matched nobody.

    Returns:
        True when a local password may be accepted for this account.
    """
    if get_auth_settings(db).local_enabled:
        return True
    return bool(user is not None and user.is_active and user.role == ROLE_SUPER_ADMIN)


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

    # Deployment-level identity-source policy. There was NO such check: /token
    # always accepted a local password, so an LDAP- or OIDC-owned deployment could
    # not actually turn local authentication off — the intended auth method was
    # advisory only. LDAP is unaffected because it authenticates below, through
    # the same form.
    #
    # An active super_admin is always exempt. That is the documented break-glass
    # account (docs/AUTH_DEPLOYMENT_GUIDE.md): auth configuration is super_admin
    # -gated, so without the exemption a deployment that disabled local auth while
    # its IdP was misconfigured would have no way back in.
    if can_use_local_auth and not _local_auth_permitted(db, local_user):
        logger.info(
            "Local password authentication is disabled for %s; deferring to the "
            "configured identity provider",
            mask_identifier(username),
        )
        can_use_local_auth = False

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
