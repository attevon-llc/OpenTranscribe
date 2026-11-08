"""
Direct database authentication: the local-password check on a Core connection.
This bypasses the SQLAlchemy ORM relationships to ensure login works.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

from joserfc import jwt
from joserfc.jwk import OctKey
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.auth.utils import local_password_allowed
from app.auth.utils import mask_identifier
from app.core.auth_types import TOKEN_TYPE_ACCESS
from app.core.config import settings
from app.core.security import signing_algorithm
from app.core.security import verify_and_update_password

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def get_db_connection() -> Connection:
    """A Core connection from the runtime engine's pool; leaving its ``with`` returns it.

    Core rather than a raw DBAPI connection so a failure arrives as SQLAlchemy's own
    exception (pool timeout, ``OperationalError`` wrapping a cancelled statement, a
    disconnect flagged ``connection_invalidated``), the classes ``core/db_overload.py``
    recognises.

    Runtime role, never ``POSTGRES_USER``: with the database role split the API holds no owner
    credentials at all, and logins must not run as the table owner. The engine also carries
    the SSL mode and the idle-in-transaction backstop.
    """
    from app.db.base import engine

    return engine.connect()


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token. **This is the issuer every login path uses.**

    Local, PKI, OIDC, SAML, proxy and MFA-enrolment logins all import this function
    (``api/endpoints/auth/{login,pki,oidc,saml,proxy,sessions,mfa_enrollment}.py``),
    as does ``services/account_security_service.py``. It is the only access-token issuer.

    **The signing algorithm is ``core.security.signing_algorithm`` — the one owner of
    that decision — and every verifier accepts via ``core.security.accepted_algorithms``,**
    so issuer and verifiers move together. ``tests/unit/test_jwt_algorithm_single_owner.py``
    fails if this module builds its own algorithm list again.

    HS256 is an approved FIPS algorithm (HMAC per FIPS 198-1 over SHA-256 per
    FIPS 180-4, 128-bit security strength under SP 800-57 Pt.1 R5), so the default
    configuration is FIPS-compliant. Operators wanting HS512 set ``JWT_ALGORITHM=HS512``
    plus a 64-byte ``JWT_SECRET_KEY``.

    Args:
        data: Claims to encode: ``sub`` plus any extra claims (this is the documented way
            for an extension to add its own). ``exp``, ``iat``, ``jti`` and ``type`` are
            always stamped here and replace any value in *data*.
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
    algorithm = signing_algorithm(TOKEN_TYPE_ACCESS)
    return jwt.encode({"alg": algorithm}, to_encode, key, algorithms=[algorithm])


logger = logging.getLogger(__name__)


_FETCH_USER_SQL = text(
    "SELECT id, email, hashed_password, full_name, role, is_active, is_superuser, auth_type, "
    'allow_local_fallback FROM "user" WHERE email = :email'
)


def _fetch_user_by_email(email: str) -> tuple | None:
    """The login row for ``email``, read on a connection that is back in the pool on return.

    Args:
        email: Normalized email address

    Returns:
        User tuple (id, email, hashed_password, full_name, role, is_active, is_superuser,
        auth_type, allow_local_fallback), or None if not found.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: the database could not answer (pool timeout,
            statement timeout, connection lost). Never read as "no such user".
    """
    with get_db_connection() as conn:
        row = conn.execute(_FETCH_USER_SQL, {"email": email}).first()
    return tuple(row) if row is not None else None


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


def _store_upgraded_hash(user_id: int, new_hash: str, masked_email: str) -> None:
    """Write the re-hashed password (FIPS auto-upgrade) on a connection of its own.

    Best effort: the password was verified, so a failed upgrade is logged and the login
    goes on; the next login tries again.
    """
    try:
        with get_db_connection() as conn:
            conn.execute(
                text('UPDATE "user" SET hashed_password = :h, updated_at = :t WHERE id = :i'),
                {"h": new_hash, "t": datetime.now(UTC), "i": user_id},
            )
            conn.commit()
        logger.info("Password hash upgraded for user %s", masked_email)
    except SQLAlchemyError as upgrade_error:
        logger.warning("Failed to upgrade password hash: %s", upgrade_error)


def _verify_and_upgrade_password(
    user_id: int, password: str, hashed_password: str, masked_email: str
) -> bool:
    """Verify password and upgrade hash if needed for FIPS compliance.

    Runs with no pooled connection held: PBKDF2 at 600,000 iterations is the slowest step
    of a login, and a connection held idle in a transaction across it is one the rest of
    the API waits for (scale_foundations_plan.md 5.6).

    Args:
        user_id: User's database ID
        password: Plaintext password to verify
        hashed_password: Current password hash from database
        masked_email: Masked email for logging

    Returns:
        True if password is valid, False otherwise
    """
    is_valid, new_hash = verify_and_update_password(password, hashed_password)

    if not is_valid:
        logger.warning("Authentication failed: incorrect password for user %s", masked_email)
        return False

    if new_hash:
        _store_upgraded_hash(user_id, new_hash, masked_email)
    return True


def direct_authenticate_user(email: str, password: str) -> dict | None:
    """
    Directly authenticate a user by email and password using a Core connection.

    This function provides a direct database authentication mechanism that bypasses
    the SQLAlchemy ORM, which can sometimes have relationship loading issues.

    Supports automatic password hash upgrade for FIPS compliance when
    FIPS_MODE is enabled.

    ``None`` means exactly "no such user", "this account cannot use a local password",
    "wrong password" or "inactive". A database that cannot answer raises instead: reading
    a pool timeout as bad credentials answered 401 under load, the SPA signed in again,
    and the retries became a 401 storm (#1337 M2). The login endpoint answers the
    raise with 503 and records no lockout attempt, since no password was checked.

    Args:
        email: The user's email address
        password: The user's plaintext password

    Returns:
        dict: User data if authentication is successful
        None: If authentication fails

    Raises:
        sqlalchemy.exc.SQLAlchemyError: the user row could not be read.
    """
    if not email or not password:
        logger.warning("Authentication attempt with empty email or password")
        return None

    email = email.lower().strip()
    masked_email = mask_identifier(email)

    user = _fetch_user_by_email(email)
    if not user:
        logger.info("Authentication failed: no user found with email %s", masked_email)
        return None

    (
        user_id,
        user_email,
        hashed_password,
        full_name,
        role,
        is_active,
        is_superuser,
        _auth_type,
        _allow_local_fallback,
    ) = user

    if not _validate_user_can_authenticate(user, masked_email):
        return None

    if not _verify_and_upgrade_password(user_id, password, hashed_password, masked_email):
        return None

    if not is_active:
        logger.warning("Authentication failed: user %s is inactive", masked_email)
        return None

    logger.info("Authentication successful for user %s", masked_email)
    return {
        "id": user_id,
        "email": user_email,
        "full_name": full_name,
        "role": role,
        "is_active": is_active,
        "is_superuser": is_superuser,
    }
