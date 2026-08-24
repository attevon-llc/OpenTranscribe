"""
Direct database authentication module for troubleshooting purposes.
This bypasses the SQLAlchemy ORM relationships to ensure login works.
"""

import logging
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import psycopg2
from joserfc import jwt
from joserfc.jwk import OctKey

from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.utils import local_password_allowed
from app.core.config import settings
from app.core.security import verify_and_update_password

# Database connection parameters
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "transcribe_app")


def get_db_connection():
    """Get a direct database connection."""
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )
    return conn


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token. **This is the issuer every login path uses.**

    Local, PKI, OIDC, SAML, proxy and MFA-enrolment logins all import this function
    (``api/endpoints/auth/{login,pki,oidc,saml,proxy,sessions,mfa_enrollment}.py``),
    as does ``services/account_security_service.py``. ``core.security.create_access_token``
    is a second implementation that no production caller reaches.

    **The signing algorithm is ``settings.JWT_ALGORITHM`` and is deliberately not
    FIPS-branched.** The verifiers on the request path —
    ``api/endpoints/auth/dependencies.py``'s ``get_current_user`` and
    ``get_optional_current_user`` — decode with ``algorithms=[settings.JWT_ALGORITHM]``
    and nothing else, so issuing under a different algorithm here would produce tokens
    that authenticate no request. Issuer and verifier move together only via that one
    setting; ``JWT_ALGORITHM_V3`` is read by ``core.security.verify_token``'s
    dual-accept list, never by an access-token issuer.

    HS256 is an approved FIPS algorithm (HMAC per FIPS 198-1 over SHA-256 per
    FIPS 180-4, 128-bit security strength under SP 800-57 Pt.1 R5), so the default
    configuration is FIPS-compliant. Operators wanting HS512 set ``JWT_ALGORITHM=HS512``
    plus a 64-byte ``JWT_SECRET_KEY``.

    Args:
        data: Dictionary of claims to encode in the token
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token string
    """
    import uuid

    to_encode = data.copy()
    now = datetime.now(UTC)

    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update(
        {
            "exp": expire,
            "iat": now,
            "jti": str(uuid.uuid4()),  # JWT ID for token revocation support
            # Purpose binding: consumers verify this, so an MFA half-token or a
            # refresh token can never be replayed as an access token.
            "type": TOKEN_TYPE_ACCESS,
        }
    )
    key = OctKey.import_key(settings.JWT_SECRET_KEY)
    return jwt.encode(
        {"alg": settings.JWT_ALGORITHM}, to_encode, key, algorithms=[settings.JWT_ALGORITHM]
    )


logger = logging.getLogger(__name__)


def _mask_identifier(identifier: str) -> str:
    """Mask an identifier for secure logging.

    Delegates to shared utility. Kept as module-private alias for
    backward compatibility with existing callers.
    """
    from app.auth.utils import mask_identifier

    return mask_identifier(identifier)


def _fetch_user_by_email(conn, cursor, email: str) -> tuple | None:
    """Fetch user record from database by email.

    Args:
        conn: Database connection
        cursor: Database cursor
        email: Normalized email address

    Returns:
        User tuple (id, email, hashed_password, full_name, role, is_active, is_superuser, auth_type, allow_local_fallback)
        or None if not found
    """
    cursor.execute(
        'SELECT id, email, hashed_password, full_name, role, is_active, is_superuser, auth_type, allow_local_fallback FROM "user" WHERE email = %s',
        (email,),
    )
    return cursor.fetchone()  # type: ignore[no-any-return]


def _validate_user_can_authenticate(user_tuple: tuple, masked_email: str) -> bool:
    """Check if user can authenticate with password.

    Delegates to :func:`app.auth.utils.local_password_allowed`, which is the one
    definition of this rule — the ORM path in ``core.security.authenticate_user``
    calls the same function, so the two can no longer drift.

    Args:
        user_tuple: User record tuple from database
        masked_email: Masked email for logging

    Returns:
        True if user can authenticate with password, False otherwise
    """
    auth_type = user_tuple[7]  # auth_type is at index 7
    allow_local_fallback = user_tuple[8] if len(user_tuple) > 8 else False

    allowed, reason = local_password_allowed(auth_type, allow_local_fallback)
    if not allowed:
        logger.info("Authentication failed: user %s — %s", masked_email, reason)
    return allowed


def _verify_and_upgrade_password(
    conn, cursor, user_id: str, password: str, hashed_password: str, masked_email: str
) -> bool:
    """Verify password and upgrade hash if needed for FIPS compliance.

    Args:
        conn: Database connection
        cursor: Database cursor
        user_id: User's database ID
        password: Plaintext password to verify
        hashed_password: Current password hash from database
        masked_email: Masked email for logging

    Returns:
        True if password is valid, False otherwise
    """
    is_valid, new_hash = verify_and_update_password(password, hashed_password)

    if not is_valid:
        logger.warning(f"Authentication failed: incorrect password for user {masked_email}")
        return False

    if not new_hash:
        return True

    # Upgrade password hash (FIPS compliance auto-upgrade)
    try:
        cursor.execute(
            'UPDATE "user" SET hashed_password = %s, updated_at = %s WHERE id = %s',
            (new_hash, datetime.now(UTC), user_id),
        )
        conn.commit()
        logger.info(f"Password hash upgraded for user {masked_email}")
    except Exception as upgrade_error:
        # Log but don't fail authentication if upgrade fails
        logger.warning(f"Failed to upgrade password hash: {str(upgrade_error)}")
        conn.rollback()

    return True


def direct_authenticate_user(email: str, password: str) -> dict | None:
    """
    Directly authenticate a user by email and password using a raw database connection.

    This function provides a direct database authentication mechanism that bypasses
    the SQLAlchemy ORM, which can sometimes have relationship loading issues.

    Supports automatic password hash upgrade for FIPS compliance when
    FIPS_MODE is enabled.

    Args:
        email: The user's email address
        password: The user's plaintext password

    Returns:
        dict: User data if authentication is successful
        None: If authentication fails
    """
    if not email or not password:
        logger.warning("Authentication attempt with empty email or password")
        return None

    email = email.lower().strip()
    masked_email = _mask_identifier(email)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        user = _fetch_user_by_email(conn, cursor, email)
        if not user:
            logger.info(f"Authentication failed: no user found with email {masked_email}")
            return None

        (
            user_id,
            user_email,
            hashed_password,
            full_name,
            role,
            is_active,
            is_superuser,
            auth_type,
            _allow_local_fallback,
        ) = user

        if not _validate_user_can_authenticate(user, masked_email):
            return None

        if not _verify_and_upgrade_password(
            conn, cursor, user_id, password, hashed_password, masked_email
        ):
            return None

        if not is_active:
            logger.warning(f"Authentication failed: user {masked_email} is inactive")
            return None

        logger.info(f"Authentication successful for user {masked_email}")
        return {
            "id": user_id,
            "email": user_email,
            "full_name": full_name,
            "role": role,
            "is_active": is_active,
            "is_superuser": is_superuser,
        }

    except Exception as e:
        logger.error(f"Database authentication error: {str(e)}")
        return None
    finally:
        if conn:
            try:
                cursor.close()
                conn.close()
            except Exception as e:
                logger.error(f"Error closing database connection: {str(e)}")
