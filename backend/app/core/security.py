import contextlib
import os
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from fastapi import Cookie
from fastapi import HTTPException
from fastapi import status
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jws import extract_compact
from joserfc.jwt import JWTClaimsRegistry
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.utils import local_password_allowed
from app.core.config import settings
from app.models.user import User


def _get_pbkdf2_iterations() -> int:
    """
    Get the appropriate PBKDF2 iteration count based on FIPS version.

    FIPS 140-3 (NIST SP 800-132 2024) recommends 600,000 iterations for SHA-256.
    FIPS 140-2 uses OWASP 2023 recommendation of 210,000 iterations.

    Returns:
        Number of PBKDF2 iterations to use
    """
    if (
        settings.FIPS_MODE
        and hasattr(settings, "FIPS_VERSION")
        and settings.FIPS_VERSION == "140-3"
    ):
        return settings.PBKDF2_ITERATIONS_V3
    return settings.PBKDF2_ITERATIONS


#: Production bcrypt work factor. Never lowered in a real deployment.
BCRYPT_DEFAULT_ROUNDS = 12

#: Work factor used only when the suite is running unhardened. bcrypt's minimum is 4.
BCRYPT_TEST_ROUNDS = 4


def _bcrypt_rounds() -> int:
    """Return the bcrypt work factor, lowered only for an unhardened test process.

    bcrypt at rounds=12 costs ~367 ms to hash and ~335 ms to verify on a modern core, and
    the suite pays both on nearly every authenticated test: a user fixture hashes a
    password, then the token fixture drives a real ``POST /api/auth/token`` that verifies
    it. Across the 859 tests that take a token fixture that is ~600 s of pure CPU — hidden
    locally by 20x xdist parallelism, but the dominant cost on CI's 2-core runner
    (issue #431). At rounds=4 the same pair costs ~3 ms.

    This is a work factor, not an algorithm: lowering it changes how expensive a hash is to
    compute, not which code path runs. bcrypt_sha256 is still the scheme under test, hashes
    still round-trip, and the rounds are embedded in each hash so verification is unaffected.
    The properties worth asserting about the *configured* cost are asserted directly by
    ``tests/test_fips_140_3.py``, which this cannot reach — see below.

    Two independent gates, matching the existing contract for ``TESTING`` documented at
    ``app/main.py:153-163`` and applied the same way at
    ``app/api/endpoints/auth/dependencies.py:666``:

    * ``TESTING`` must be truthy, and
    * ``settings.is_hardened`` must be False.

    So the override is inert in a real deployment even if ``TESTING`` leaks into the
    environment — ``ENVIRONMENT`` defaults to ``production`` and only the closed
    ``RELAXED_ENVIRONMENTS`` set unhardens it, so an unset or misspelled value fails closed.

    FIPS mode is deliberately untouched: ``_get_pbkdf2_iterations()`` stays at its real
    value because ``tests/test_fips_140_3.py`` reads the iteration count back out of the
    hash and asserts it equals ``settings.PBKDF2_ITERATIONS_V3`` (600,000). The FIPS gate
    phase therefore stays slow by design.
    """
    if not settings.is_hardened and os.environ.get("TESTING", "").lower() == "true":
        with contextlib.suppress(ValueError):
            return max(4, int(os.environ.get("TEST_BCRYPT_ROUNDS", BCRYPT_TEST_ROUNDS)))
    return BCRYPT_DEFAULT_ROUNDS


def _create_password_context() -> CryptContext:
    """
    Create password hashing context based on FIPS mode configuration.

    FIPS 140-3 compliant hashing uses PBKDF2-SHA256 with 600,000 iterations (NIST SP 800-132 2024).
    FIPS 140-2 compliant hashing uses PBKDF2-SHA256 with 210,000 iterations (NIST SP 800-132).
    Non-FIPS mode supports bcrypt_sha256, bcrypt, and PBKDF2 for backward compatibility.

    Returns:
        CryptContext configured for the appropriate hashing schemes
    """
    iterations = _get_pbkdf2_iterations()

    if settings.FIPS_MODE:
        # FIPS mode: Use only PBKDF2-SHA256 (NIST SP 800-132 compliant)
        # Auto-upgrade from bcrypt/bcrypt_sha256 on successful verify.
        # Rounds stay at the production value here: in FIPS mode bcrypt is verify-only
        # (deprecated), so lowering it would buy nothing, and PBKDF2 iterations are asserted
        # by the FIPS suite.
        return CryptContext(
            schemes=["pbkdf2_sha256", "bcrypt_sha256", "bcrypt"],
            default="pbkdf2_sha256",
            deprecated=["bcrypt_sha256", "bcrypt"],
            pbkdf2_sha256__rounds=iterations,
            bcrypt_sha256__default_rounds=BCRYPT_DEFAULT_ROUNDS,
            bcrypt__default_rounds=BCRYPT_DEFAULT_ROUNDS,
        )
    else:
        # Standard mode: bcrypt_sha256 for new hashes, support legacy bcrypt and PBKDF2
        # Auto-upgrade from plain bcrypt on successful verify
        rounds = _bcrypt_rounds()
        return CryptContext(
            schemes=["bcrypt_sha256", "bcrypt", "pbkdf2_sha256"],
            default="bcrypt_sha256",
            deprecated=["bcrypt"],
            bcrypt_sha256__default_rounds=rounds,
            bcrypt__default_rounds=rounds,
            pbkdf2_sha256__rounds=iterations,
        )


# Global password context - recreated when needed
pwd_context = _create_password_context()


def _get_jwt_algorithm() -> str:
    """
    Get the appropriate JWT algorithm based on FIPS version.

    FIPS 140-3 prefers HS512 for stronger HMAC signatures.
    FIPS 140-2 and non-FIPS mode use HS256.

    Returns:
        JWT algorithm string (HS256 or HS512)
    """
    if (
        settings.FIPS_MODE
        and hasattr(settings, "FIPS_VERSION")
        and settings.FIPS_VERSION == "140-3"
    ):
        return settings.JWT_ALGORITHM_V3
    return settings.JWT_ALGORITHM


def create_access_token(
    subject: str | Any,
    expires_delta: timedelta | None = None,
    additional_claims: dict | None = None,
) -> str:
    """
    Create a JWT access token with optional additional claims.

    For FIPS 140-3 compliance, uses HS512 algorithm for stronger HMAC signatures.
    For FIPS 140-2 and non-FIPS mode, uses HS256.

    Args:
        subject: The subject (usually user UUID) to encode in the token
        expires_delta: Optional custom expiration time
        additional_claims: Optional dict of additional claims to include

    Returns:
        Encoded JWT token string
    """
    now = datetime.now(UTC)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)

    algorithm = _get_jwt_algorithm()

    to_encode = {
        "exp": expire,
        "sub": str(subject),
        "iat": now,
        "jti": str(uuid.uuid4()),  # JWT ID for token revocation support
        "alg_version": "v3" if algorithm == "HS512" else "v2",  # Track algorithm version
        # Purpose binding — see auth.constants.TOKEN_TYPE_ACCESS.
        "type": TOKEN_TYPE_ACCESS,
    }

    if additional_claims:
        to_encode.update(additional_claims)

    key = OctKey.import_key(settings.JWT_SECRET_KEY)
    return jwt.encode({"alg": algorithm}, to_encode, key, algorithms=[algorithm])


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against a hash.

    Args:
        plain_password: The plaintext password to verify
        hashed_password: The stored password hash

    Returns:
        True if password matches, False otherwise
    """
    return pwd_context.verify(plain_password, hashed_password)  # type: ignore[no-any-return]


def verify_and_update_password(
    plain_password: str, hashed_password: str
) -> tuple[bool, str | None]:
    """
    Verify a password and optionally return an upgraded hash.

    This function supports automatic hash upgrade for FIPS compliance:
    - In FIPS mode: upgrades bcrypt/bcrypt_sha256 to PBKDF2-SHA256
    - In standard mode: upgrades plain bcrypt to bcrypt_sha256

    Args:
        plain_password: The plaintext password to verify
        hashed_password: The stored password hash

    Returns:
        Tuple of (is_valid, new_hash) where new_hash is None if no upgrade needed
    """
    is_valid, new_hash = pwd_context.verify_and_update(plain_password, hashed_password)
    return is_valid, new_hash


def needs_rehash(hashed_password: str) -> bool:
    """
    Check if a password hash needs to be upgraded.

    This is useful for batch checking without requiring the plaintext password.

    Args:
        hashed_password: The stored password hash

    Returns:
        True if the hash uses a deprecated scheme and should be upgraded
    """
    return pwd_context.needs_update(hashed_password)  # type: ignore[no-any-return]


def needs_rehash_for_fips_v3(hashed_password: str) -> bool:
    """
    Check if a password hash needs to be upgraded for FIPS 140-3 compliance.

    FIPS 140-3 requires PBKDF2-SHA256 with 600,000 iterations (NIST SP 800-132 2024).
    This function checks if:
    1. The hash is not using PBKDF2-SHA256 (e.g., bcrypt)
    2. The hash is using PBKDF2-SHA256 but with fewer iterations than required

    Args:
        hashed_password: The stored password hash

    Returns:
        True if the hash needs to be upgraded for FIPS 140-3 compliance
    """
    # First check if it needs basic rehash (wrong algorithm)
    if pwd_context.needs_update(hashed_password):
        return True

    # Check if it's a PBKDF2 hash with insufficient iterations
    if hashed_password.startswith("$pbkdf2-sha256$"):
        try:
            # PBKDF2 hash format: $pbkdf2-sha256$<rounds>$<salt>$<hash>
            parts = hashed_password.split("$")
            if len(parts) >= 3:
                rounds = int(parts[2])
                # Check against FIPS 140-3 iteration requirement
                if hasattr(settings, "PBKDF2_ITERATIONS_V3"):
                    return rounds < settings.PBKDF2_ITERATIONS_V3
        except (ValueError, IndexError):
            # If we can't parse, assume it needs rehash to be safe
            return True

    return False


def get_password_hash(password: str) -> str:
    """
    Hash a password for storing
    """
    return pwd_context.hash(password)  # type: ignore[no-any-return]


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """
    Authenticate a user by email and password.

    Note: LDAP users cannot authenticate via this function - they must use
    LDAP authentication. This function is for local users only.
    """
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return None

    # One definition of the rule, shared with the raw-SQL path in
    # app.auth.direct_auth. This check used to be inlined here WITHOUT the LDAP
    # hard-block that direct_auth had, so an LDAP account with
    # allow_local_fallback set reached the password comparison below.
    allowed, _reason = local_password_allowed(
        str(user.auth_type), bool(getattr(user, "allow_local_fallback", False))
    )
    if not allowed:
        return None

    # Empty password hash means user cannot authenticate locally
    if not user.hashed_password:
        return None

    if not verify_password(password, str(user.hashed_password)):
        return None
    return user  # type: ignore[no-any-return]


def get_token_from_cookie(access_token: str | None = Cookie(None)) -> str:
    """
    Extract the JWT token from the cookie
    """
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return access_token


def verify_token(token: str, expected_type: str | None = TOKEN_TYPE_ACCESS) -> dict[str, Any]:
    """
    Verify a JWT token and return its payload.

    Args:
        token: The encoded JWT.
        expected_type: Required value of the ``type`` claim. Defaults to
            ``access`` so a caller must opt out deliberately; pass ``None`` only
            when the token's purpose is checked elsewhere. This is what stops an
            MFA half-token or a refresh token from being replayed as an access
            token — both are signed with the same key and, in non-FIPS mode, an
            algorithm this function accepts.

    Supports dual algorithm verification for FIPS 140-3 migration:
    - In compatible mode: tries HS512 first, falls back to HS256
    - In strict mode: only accepts the configured algorithm
    - In non-FIPS mode: accepts HS256 only

    This allows seamless migration from FIPS 140-2 (HS256) to FIPS 140-3 (HS512)
    without invalidating existing tokens during the transition period.

    When a token uses HS256 but FIPS 140-3 mode is active, the fallback is
    audited for compliance tracking.
    """
    # Check token header algorithm for audit logging
    token_algorithm = None
    with contextlib.suppress(JoseError):  # unparseable header is handled by decode below
        token_algorithm = extract_compact(token.encode()).headers().get("alg")

    # Determine which algorithms to accept
    is_fips_140_3 = (
        settings.FIPS_MODE
        and hasattr(settings, "FIPS_VERSION")
        and settings.FIPS_VERSION == "140-3"
    )
    if is_fips_140_3:
        migration_mode = getattr(settings, "FIPS_MIGRATION_MODE", "compatible")
        if migration_mode == "strict":
            # Strict mode: only accept HS512
            allowed_algorithms = [settings.JWT_ALGORITHM_V3]
        else:
            # Compatible mode: try HS512 first, then HS256 for migration
            allowed_algorithms = [settings.JWT_ALGORITHM_V3, settings.JWT_ALGORITHM]
    else:
        # Non-FIPS or FIPS 140-2: accept both for backward compatibility
        allowed_algorithms = [settings.JWT_ALGORITHM]
        if hasattr(settings, "JWT_ALGORITHM_V3"):
            allowed_algorithms.append(settings.JWT_ALGORITHM_V3)

    try:
        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        token_obj = jwt.decode(token, key, algorithms=allowed_algorithms)
        # joserfc verifies the signature/algorithm only — exp is not checked
        # automatically (unlike python-jose), so it's validated explicitly here.
        JWTClaimsRegistry(exp={"essential": True}).validate(token_obj.claims)
        payload: dict[str, Any] = token_obj.claims

        if expected_type is not None and payload.get("type") != expected_type:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Audit log algorithm fallback in FIPS 140-3 mode
        if token_algorithm == "HS256" and is_fips_140_3:  # noqa: S105 - JWT algorithm name, not a password  # nosec B105
            from app.auth.audit import AuditEventType
            from app.auth.audit import AuditOutcome
            from app.auth.audit import audit_logger

            audit_logger.log(
                event_type=AuditEventType.AUTH_TOKEN_VERIFY,
                outcome=AuditOutcome.SUCCESS,
                details={
                    "warning": "legacy_algorithm_fallback",
                    "used_algorithm": "HS256",
                    "required_algorithm": "HS512",
                },
            )

        return payload
    except JoseError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
