"""MFA half-token minting, verification, and single-use replay protection.

MFA tokens are single-use: the JTI is claimed in Redis once spent.

A half-token is additionally *scoped*. Both scopes are ``type: "mfa"`` — rejected
everywhere an access token is expected — but they authorize different steps:

- ``verify`` — the user has MFA enrolled and must prove possession at ``/mfa/verify``.
- ``enroll`` — the deployment requires MFA and the user has NOT enrolled yet, so the
  token authorizes ``/mfa/setup`` + ``/mfa/verify-setup`` and nothing else.

Keeping them apart is the point: ``/mfa/setup`` overwrites the TOTP secret and wipes the
backup codes, so honouring a ``verify`` token there would let somebody holding only the
password reset the second factor — the exact escalation MFA exists to stop.

This module is the primitive layer and imports no other module in the package.
:mod:`mfa_enrollment` sits above it with the enrolment dependency and session issuance;
the scope constants stay HERE because ``_create_mfa_token`` / ``_verify_mfa_token``
stamp and check them, and moving them up would make the two modules cycle.
"""

import logging
from datetime import UTC
from datetime import timedelta
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from fastapi import status
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry
from sqlalchemy.orm import Session

from app.auth.constants import AUTH_TYPE_OIDC
from app.auth.constants import AUTH_TYPE_PKI
from app.auth.constants import TOKEN_TYPE_MFA
from app.auth.constants import VALID_AUTH_TYPES
from app.auth.mfa import MFAService
from app.auth.session import get_redis_client
from app.core.auth_settings import get_auth_settings
from app.core.config import settings
from app.models.user import User
from app.models.user_mfa import UserMFA

logger = logging.getLogger(__name__)


MFA_TOKEN_BLACKLIST_PREFIX = "mfa:jti:"  # noqa: S105 # nosec B105

#: Values of the ``mfa_scope`` claim. See the module docstring for why they differ.
MFA_SCOPE_VERIFY = "verify"
MFA_SCOPE_ENROLL = "enroll"


def mfa_token_ttl_seconds() -> int:
    """Effective lifetime of an MFA half-token, in seconds.

    ``mfa_token_expire_minutes`` is admin-settable and every consumer read
    ``settings.MFA_TOKEN_EXPIRE_MINUTES`` instead, so the saved value was never
    used. This is the single resolver for both halves of the control: the ``exp``
    claim on the minted token and the TTL of the single-use record that stops it
    being replayed. Those two must not diverge — a blacklist entry that expires
    before the token does re-opens the replay window the claim exists to close.
    """
    from app.core.auth_settings import get_process_auth_settings

    return get_process_auth_settings().mfa_token_expire_minutes * 60


def _user_can_setup_mfa(user: User) -> bool:
    """Check if user is eligible for local TOTP MFA setup.

    Local TOTP MFA only applies to users whose identity this app owns. Users
    authenticated by an external identity provider get their MFA from that IdP:

    - PKI: smart card is already two-factor (something you have + PIN).
    - OIDC: MFA is handled by the identity provider.
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
    if user.auth_type in (AUTH_TYPE_PKI, AUTH_TYPE_OIDC):
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


def _blacklist_mfa_token(jti: str, expires_seconds: int) -> bool:
    """Unconditionally add an MFA token JTI to the blacklist.

    Burns a jti regardless of who wrote it first. The login path does NOT use this —
    it needs the write and the "already used?" read to be one operation, which is
    :func:`_claim_mfa_token`.

    Args:
        jti: JWT ID to blacklist
        expires_seconds: Time in seconds until the token naturally expires

    Returns:
        bool: True if blacklisted successfully, False otherwise

    Raises:
        HTTPException: 503 if Redis unavailable and MFA_REQUIRE_REDIS is True
    """
    # Same floor as _claim_mfa_token: the record must outlive the token.
    expires_seconds = max(expires_seconds, mfa_token_ttl_seconds())

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
        logger.exception(f"Failed to blacklist MFA token JTI: {e}")
        if settings.MFA_REQUIRE_REDIS:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth service unavailable",
            ) from e
        return False


def _claim_mfa_token(jti: str, expires_seconds: int) -> bool:
    """Atomically claim an MFA token JTI, returning False if it was already claimed.

    Single-use enforcement has to be ONE operation. Reading the blacklist in
    ``_verify_mfa_token`` and writing it only after the code check leaves a window in
    which two concurrent ``/mfa/verify`` calls carrying the same jti both pass the read
    and both mint a session — one half-token, two sessions. ``SET NX EX`` collapses the
    read and the write into a single round trip, the same idiom
    ``MFAService._consume_totp_code`` uses for TOTP codes.

    Args:
        jti: JWT ID to claim
        expires_seconds: Time in seconds until the token naturally expires

    Returns:
        bool: True if this caller won the claim and may proceed, False if the token was
            already used.

    Raises:
        HTTPException: 503 if Redis is unavailable and MFA_REQUIRE_REDIS is True
    """
    # Never let the claim record expire before the token it burns. Callers compute
    # ``expires_seconds`` themselves and one of them (``mfa_enrollment``) still
    # derives it from ``settings.MFA_TOKEN_EXPIRE_MINUTES``; if an admin raises
    # ``mfa_token_expire_minutes`` above the .env value, that caller's TTL would
    # leave the token valid but no longer claimed — a replay window opened by a
    # configuration change. Clamping here closes it for every caller at once.
    expires_seconds = max(expires_seconds, mfa_token_ttl_seconds())

    key = f"{MFA_TOKEN_BLACKLIST_PREFIX}{jti}"
    try:
        redis_client = get_redis_client()
        if redis_client:
            claimed = bool(redis_client.set(key, "1", nx=True, ex=expires_seconds))
            if not claimed:
                logger.warning(f"MFA token replay rejected (jti={jti[:8]}...)")
            return claimed
    except Exception as e:
        logger.exception(f"Failed to claim MFA token JTI: {e}")
        if settings.MFA_REQUIRE_REDIS:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth service unavailable",
            ) from e

    if settings.MFA_REQUIRE_REDIS:
        logger.error("Redis not available for MFA token claim (fail-secure mode)")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service unavailable",
        )

    # No atomic client. Fall back to the non-atomic blacklist pair so a Redis-less
    # deployment behaves exactly as it did before the claim existed (fail-open, which is
    # what MFA_REQUIRE_REDIS=false explicitly asks for) instead of silently accepting
    # without even consulting the blacklist.
    logger.warning("Redis not available for MFA token claim; replay protection degraded")
    if _is_mfa_token_blacklisted(jti):
        return False
    _blacklist_mfa_token(jti, expires_seconds)
    return True


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
        logger.exception(f"Failed to check MFA token blacklist: {e}")
        # Fail-secure when Redis required (deny access), fail-open otherwise
        return bool(settings.MFA_REQUIRE_REDIS)


def _create_mfa_token(user_uuid_str: str, user_role: str, scope: str = MFA_SCOPE_VERIFY) -> str:
    """Create a short-lived, single-use MFA half-token.

    Args:
        user_uuid_str: User UUID string
        user_role: User's role
        scope: ``MFA_SCOPE_VERIFY`` (prove possession at /mfa/verify) or
            ``MFA_SCOPE_ENROLL`` (enrol at /mfa/setup + /mfa/verify-setup). The two are
            not interchangeable — see the module docstring.

    Returns:
        str: Short-lived MFA token with unique JTI
    """
    import uuid as uuid_mod
    from datetime import datetime

    mfa_token_expires = timedelta(seconds=mfa_token_ttl_seconds())
    now = datetime.now(UTC)
    expire = now + mfa_token_expires

    mfa_token_data = {
        "sub": user_uuid_str,
        "role": user_role,
        # Purpose binding: rejected everywhere an access token is expected.
        "type": TOKEN_TYPE_MFA,
        "mfa_scope": scope,
        "jti": str(uuid_mod.uuid4()),  # Unique JWT ID for single-use enforcement
        "iat": now,
        "exp": expire,
    }

    # Create token manually since we need to include jti
    key = OctKey.import_key(settings.JWT_SECRET_KEY)
    return jwt.encode(
        {"alg": settings.JWT_ALGORITHM}, mfa_token_data, key, algorithms=[settings.JWT_ALGORITHM]
    )


def _verify_mfa_token(
    mfa_token: str, expected_scope: str = MFA_SCOPE_VERIFY
) -> tuple[str, str, str]:
    """Verify an MFA token and extract user information.

    Checks that the token is valid, is an MFA-type token of the expected scope, and has
    not been previously used (via JTI blacklist check).

    Args:
        mfa_token: The MFA token to verify
        expected_scope: Scope this call site accepts. A token minted before scopes
            existed carries no claim and is treated as ``verify``, its historical
            meaning, so half-tokens in flight across a deploy still work.

    Returns:
        tuple[str, str, str]: (user_uuid_str, user_role, jti)

    Raises:
        HTTPException: If the token is invalid, not an MFA token, out of scope, or used
    """
    try:
        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        token_obj = jwt.decode(mfa_token, key, algorithms=[settings.JWT_ALGORITHM])
        # joserfc verifies the signature/algorithm only — exp is not checked
        # automatically (unlike python-jose), so it's validated explicitly here.
        JWTClaimsRegistry(exp={"essential": True}).validate(token_obj.claims)
        payload = token_obj.claims

        # Verify this is an MFA token, not a regular access token
        if payload.get("type") != TOKEN_TYPE_MFA:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid MFA token",
            )

        token_scope = payload.get("mfa_scope", MFA_SCOPE_VERIFY)
        if token_scope != expected_scope:
            logger.warning(
                "Rejected MFA token with scope=%r on a %r path", token_scope, expected_scope
            )
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

        # Both are always set by _create_mfa_token; joserfc's real dict typing
        # (unlike python-jose's untyped stubs) surfaces the theoretical
        # Optional-ness that the mint side never actually produces.
        return cast(str, user_uuid_str), cast(str, user_role), cast(str, jti)

    except JoseError as e:
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
        HTTPException: If token invalid, user not found, deactivated, or MFA not enabled
    """
    # Verify the MFA token (also checks JTI blacklist for replay prevention). Only a
    # verify-scoped token gets here: an enrolment token must not complete a login.
    user_uuid_str, user_role, mfa_jti = _verify_mfa_token(
        mfa_token, expected_scope=MFA_SCOPE_VERIFY
    )

    # Get user from database
    user_uuid = UUID(user_uuid_str)
    user = db.query(User).filter(User.uuid == user_uuid).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    # An account deactivated between password auth and MFA verification must not be able
    # to finish the login: /mfa/verify persists a RefreshToken row, so skipping this check
    # handed a disabled account a durable session. login.py and sessions.py both gate here.
    if not user.is_active:
        logger.warning(f"MFA verification refused for inactive user: {str(user.email)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Inactive user account",
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
        is_valid = MFAService.verify_totp(
            decrypted_secret, normalized_code, user_id=user_mfa.user_id
        )

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
