import contextlib
import os
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

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
    if settings.fips_140_3_active:
        return settings.PBKDF2_ITERATIONS_V3
    return settings.PBKDF2_ITERATIONS


# ── JWT algorithm selection: ONE owner for each of the two questions ────────────
#
# Before this pair there were five copies of "which algorithm?" and they disagreed:
#
#   * ``token_service.create_refresh_token`` gated on ``FIPS_VERSION`` alone, which
#     defaults to ``"140-3"`` — so EVERY deployment, FIPS or not, signed refresh
#     tokens with HS512 while its access tokens were HS256. Nothing noticed because
#     ``verify_token_with_fallback`` happened to try both.
#   * ``token_service.create_token`` duplicated the branch inline with a different
#     (correct) gate.
#   * ``verify_token`` below accepted HS512-first with a strict mode, while the HTTP
#     verifiers in ``api/endpoints/auth/dependencies.py`` hardcoded
#     ``[settings.JWT_ALGORITHM]``. Under FIPS strict that pair means HTTP requests
#     work and WebSocket handshakes are refused.
#
# The rule these two functions encode, and which ``tests/unit/
# test_jwt_issuer_verifier_agreement.py`` pins for every token type in both FIPS
# modes:
#
#     signing_algorithm(t) ∈ accepted_algorithms(t)     — for all t, in every config.
#
# A configuration that violates it is an outage, not a hardening.

#: The algorithm every deployment signed with before any of this was configurable,
#: and still the default of ``JWT_ALGORITHM`` (asserted by
#: ``tests/unit/test_jwt_issuer_verifier_agreement.py``).
#:
#: It stays in the dual-accept set while the migration window is open, and that is
#: load-bearing rather than belt-and-braces. Without it, an operator following the
#: documented route to HS512 (``JWT_ALGORITHM=HS512``) would find that
#: ``JWT_ALGORITHM`` and ``JWT_ALGORITHM_V3`` now name the SAME algorithm, so the
#: "compatible" set collapses to one entry and every token in flight is refused on
#: restart — a migration window that closes itself at the exact moment it is needed.
HISTORICAL_ALGORITHM = "HS256"  # noqa: S105 - a JWS alg name, not a password  # nosec B105


def signing_algorithm(token_type: str = TOKEN_TYPE_ACCESS) -> str:
    """Return the algorithm that signs a *token_type* token on this deployment.

    **The answer is ``settings.JWT_ALGORITHM`` for every token type, in every FIPS
    mode.** ``token_type`` is accepted so that call sites read as an explicit
    question rather than a constant, and so a future divergence has exactly one
    place to live — but it is deliberately not a divergence today, for two reasons:

    1. **The access-token issuer cannot move.** Every production login goes through
       ``auth/direct_auth.create_access_token``, which signs with
       ``settings.JWT_ALGORITHM`` unconditionally; ``api/endpoints/auth/mfa_tokens``
       does the same for the MFA half-token. Any per-type branch here that those
       modules do not share would mint tokens their own verifiers reject.
    2. **HS256 is FIPS-approved**, so there is no compliance reason to branch. HMAC
       (FIPS 198-1) over SHA-256 (FIPS 180-4) carries 128 bits of security strength
       under SP 800-57 Pt.1 R5, above the 112-bit floor of SP 800-131A Rev.2.

    ``JWT_ALGORITHM_V3`` is therefore a **verifier** setting only — the other member
    of the dual-accept migration set — and is never consulted here. Operators who
    must run HS512 set ``JWT_ALGORITHM=HS512`` (plus a 64-byte ``JWT_SECRET_KEY``),
    which moves issuance and acceptance together because both read this function.

    Args:
        token_type: One of ``access``/``refresh``/``mfa``. Documented above.

    Returns:
        The JWS ``alg`` value to sign with.
    """
    # token_type is intentionally not branched on — see the docstring. Referenced
    # here so a future branch has an obvious seam and linters see it used.
    del token_type
    return settings.JWT_ALGORITHM


def accepted_algorithms(token_type: str = TOKEN_TYPE_ACCESS) -> list[str]:
    """Return the algorithms a *token_type* token may be signed with to be accepted.

    Every JWT verifier in this codebase that the algorithm decision reaches must
    call this — ``verify_token`` below, both request-path verifiers in
    ``api/endpoints/auth/dependencies.py``, and
    ``auth/token_service.verify_token_with_fallback``.

    The list always leads with :func:`signing_algorithm`, so what this deployment
    issues is always accepted. What follows depends on the migration window:

    * **Window open** (the default, and anything outside FIPS 140-3): also accept
      the other configured algorithm and :data:`HISTORICAL_ALGORITHM`, so changing
      what is signed does not invalidate tokens already in flight.
    * **Window closed** (``fips_140_3_active`` and ``FIPS_MIGRATION_MODE=strict``):
      accept only the signing algorithm.

    Strict used to be spelled ``[JWT_ALGORITHM_V3]`` unconditionally. That is the
    defect it looks like a hardening of: no issuer in this codebase has ever minted
    ``JWT_ALGORITHM_V3``, so a strict deployment accepted nothing it could produce —
    ``verify_token`` is the WebSocket and SAML verifier, so turning strict on
    refused every WebSocket handshake while HTTP (which decoded with a different,
    hardcoded list) carried on working. "Only what we sign" is the meaning that both
    closes the window and leaves the deployment able to authenticate: an operator
    who needs HS256 refused sets ``JWT_ALGORITHM=HS512``, and strict then accepts
    HS512 alone.

    Args:
        token_type: Passed to :func:`signing_algorithm`; see its docstring.

    Returns:
        Ordered, de-duplicated ``alg`` values, signing algorithm first.
    """
    signed = signing_algorithm(token_type)
    if settings.fips_140_3_active and settings.FIPS_MIGRATION_MODE == "strict":
        return [signed]

    accepted = [signed]
    for candidate in (
        settings.JWT_ALGORITHM,
        settings.JWT_ALGORITHM_V3,
        HISTORICAL_ALGORITHM,
    ):
        if candidate not in accepted:
            accepted.append(candidate)
    return accepted


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


def create_access_token(
    subject: str | Any,
    expires_delta: timedelta | None = None,
    additional_claims: dict | None = None,
) -> str:
    """
    Create a JWT access token with optional additional claims.

    **The algorithm comes from :func:`signing_algorithm` — the one owner of that
    question.** There used to be a ``_get_jwt_algorithm()`` here that returned
    ``JWT_ALGORITHM_V3`` (HS512) under ``FIPS_MODE`` + ``FIPS_VERSION="140-3"``. It was
    deleted rather than wired into the real login path, because the access-token
    *verifiers* on the request path (``api/endpoints/auth/dependencies.py`` —
    ``get_current_user`` and ``get_optional_current_user``) decoded with
    ``algorithms=[settings.JWT_ALGORITHM]`` and nothing else. An issuer that
    FIPS-branched while those did not would mint tokens no authenticated request
    could verify. Those verifiers now call :func:`accepted_algorithms`, so the pair
    can no longer drift apart silently.

    **HS256 is FIPS-approved.** HMAC-SHA-256 is an approved algorithm under FIPS 198-1
    with SHS (FIPS 180-4); NIST SP 800-57 Pt.1 R5 puts it at 128 bits of security, above
    the 112-bit floor SP 800-131A Rev.2 requires. Running HS256 is therefore a
    compliant configuration, not a violation — the defect this replaced was a false
    documentation claim and a dead branch, not weak crypto.

    To actually run HS512, set ``JWT_ALGORITHM=HS512`` (and a ``JWT_SECRET_KEY`` of at
    least 64 bytes — ``config.py`` warns otherwise). That single knob moves issuance
    **and** verification together, because both read the same setting.

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

    algorithm = signing_algorithm(TOKEN_TYPE_ACCESS)

    to_encode = {
        "exp": expire,
        "sub": str(subject),
        "iat": now,
        "jti": str(uuid.uuid4()),  # JWT ID for token revocation support
        # Track algorithm version. Derived from the algorithm actually used, so it
        # stays correct when an operator sets JWT_ALGORITHM=HS512; it is "v2" on a
        # default deployment because HS256 is the default.
        "alg_version": "v3" if algorithm == "HS512" else "v2",
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

    Email is normalised the same way ``app.auth.direct_auth`` normalises it
    (``.lower().strip()``). Login tries the raw-SQL path first and falls back here, and the
    two disagreed: ``direct_auth`` lowercased the supplied address while this function did an
    exact, case-sensitive match. So whether ``Foo@Example.com`` could log in depended on
    which path answered — case-insensitive in production, case-sensitive whenever the
    fallback ran. That divergence is the same class of bug the LDAP comment below was written
    to close, and the test meant to catch it asserted ``status_code in (200, 401)``, which
    accepted either outcome (issue #431).
    """
    email = email.lower().strip()
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

    The accepted algorithms come from :func:`accepted_algorithms` — the one owner of
    that question, shared with both request-path verifiers in
    ``api/endpoints/auth/dependencies.py`` and with
    ``auth/token_service.verify_token_with_fallback``. This function is the
    WebSocket and SAML verifier; when it kept its own list, a FIPS-strict deployment
    refused every WebSocket handshake while HTTP kept working.

    When a token uses the legacy algorithm but FIPS 140-3 mode is active, the
    fallback is audited for compliance tracking — that record is how an operator
    knows the migration window can be closed.
    """
    # Check token header algorithm for audit logging
    token_algorithm = None
    with contextlib.suppress(JoseError):  # unparseable header is handled by decode below
        token_algorithm = extract_compact(token.encode()).headers().get("alg")

    is_fips_140_3 = settings.fips_140_3_active
    token_purpose = expected_type or TOKEN_TYPE_ACCESS
    allowed_algorithms = accepted_algorithms(token_purpose)
    current_algorithm = signing_algorithm(token_purpose)

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

        # Audit the dual-accept fallback in FIPS 140-3 mode: a token signed with
        # something OTHER than what this deployment currently signs with is the only
        # thing keeping the migration window open, so it is the thing worth counting.
        #
        # This used to compare against the literal "HS256". On a FIPS deployment left
        # at the default JWT_ALGORITHM=HS256 that names the CURRENT algorithm, so it
        # fired on every single verification — an audit stream that says "legacy
        # fallback" about every request tells an operator nothing about when the
        # window can close, which is the one question it exists to answer.
        if is_fips_140_3 and token_algorithm and token_algorithm != current_algorithm:
            from app.auth.audit import AuditEventType
            from app.auth.audit import AuditOutcome
            from app.auth.audit import audit_logger

            audit_logger.log(
                event_type=AuditEventType.AUTH_TOKEN_VERIFY,
                outcome=AuditOutcome.SUCCESS,
                details={
                    "warning": "legacy_algorithm_fallback",
                    "used_algorithm": token_algorithm,
                    "required_algorithm": current_algorithm,
                },
            )

        return payload
    except JoseError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
