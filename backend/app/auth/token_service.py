"""
Token service for refresh token management and token revocation.

Implements FedRAMP AC-12 compliant token management:
- Refresh token creation and validation
- Token revocation via Redis blacklist
- Session management through refresh token tracking

Security Features:
- Refresh tokens stored as SHA-512 hashes in database (FIPS 140-3 compliant)
- JTI-based revocation via Redis with TTL = remaining token lifetime
- Automatic cleanup of expired tokens
- Rate limiting on token refresh operations
- Dual JWT verification for FIPS 140-3 migration, via the single owner of that
  decision: ``core.security.accepted_algorithms``
"""

import hashlib
import logging
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from joserfc import jwt
from joserfc.errors import ExpiredTokenError
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.bulk_revocation import PendingBulkRevocation
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.constants import TOKEN_TYPE_REFRESH
from app.auth.revocation_fallback import is_revoked_from_database
from app.auth.revocation_fallback import record_degradation
from app.auth.session import InMemoryStore
from app.auth.session import get_redis_client
from app.core.config import settings
from app.core.constants import SESSION_CAP_EVICTION_BATCH_LIMIT
from app.core.security import accepted_algorithms
from app.core.security import signing_algorithm
from app.models.refresh_token import RefreshToken
from app.models.user import User

logger = logging.getLogger(__name__)

# Redis key prefix for revoked tokens (not a password)
REVOKED_TOKEN_PREFIX = "revoked:jti:"  # noqa: S105 # nosec B105

#: Per-user revocation epoch. Access tokens are stateless — nothing durable
#: records their JTIs — so revoking "all of a user's tokens" could only ever
#: blacklist their *refresh* tokens, leaving the current access token valid for
#: up to its full lifetime. That made ``/auth/logout/all`` strictly weaker than
#: ``/auth/logout``, which does blacklist the access JTI. Stamping a timestamp
#: per user and rejecting any access token issued before it closes the gap for
#: every revocation path at once.
USER_REVOCATION_EPOCH_PREFIX = "revoked:user:"  # noqa: S105 # nosec B105


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a timestamp read back from the database to an aware UTC datetime.

    The session columns are ``TIMESTAMP WITH TIME ZONE``, but a row constructed in
    memory (or read through a driver configured otherwise) can still carry a naive
    value, and comparing naive to aware raises ``TypeError`` — inside an auth path
    that would surface as a 500 rather than a denial.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _session_lifetime_minutes(db: Session) -> tuple[int, int, int]:
    """Return ``(idle_timeout_minutes, absolute_timeout_minutes, max_concurrent_sessions)``.

    DB-backed (admin UI) over ``.env`` over the coded default, like the rest of the
    auth configuration. Never raises: a configuration read must not be able to
    break token issue or verification, so an unavailable database degrades to the
    environment values.

    The concurrent-session cap rides along with the two timeouts precisely so that
    :meth:`TokenService.create_refresh_token` — the one place that needs all three —
    resolves ``get_auth_settings(db)`` exactly once per call, not twice.
    """
    try:
        from app.core.auth_settings import get_auth_settings

        auth_settings = get_auth_settings(db)
        return (
            auth_settings.session_idle_timeout_minutes,
            auth_settings.session_absolute_timeout_minutes,
            auth_settings.max_concurrent_sessions,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not read session lifetime config; using .env values", exc_info=True)
        return (
            settings.SESSION_IDLE_TIMEOUT_MINUTES,
            settings.SESSION_ABSOLUTE_TIMEOUT_MINUTES,
            settings.MAX_CONCURRENT_SESSIONS,
        )


def enforce_session_ceiling(
    db: Session, user_id: int, limit: int, *, batch_limit: int | None = None
) -> list[str]:
    """Revoke every active session of *user_id* beyond the newest *limit*.

    Expressed as a **post-condition** ("keep the newest ``limit``, revoke
    everything else"), not as an action ("evict one"). That distinction is the
    fix: the previous mechanism loaded the active rows in Python, picked the
    single oldest with ``min()``, and revoked it — an action that is a
    conservation law once the count already sits at or above the cap (evict
    exactly one, mint exactly one nets to zero change), and that has no gap lock,
    so two concurrent logins can each see room for one more and both mint,
    netting +1. A single ``UPDATE ... WHERE id IN (SELECT ... OFFSET :limit)``
    has neither problem: any concurrent evaluation of it drives the active count
    to at most ``limit``, and it does the eviction and the accounting in one
    round trip with no Python-side row loading.

    ``ORDER BY created_at DESC, id DESC`` — the ``id DESC`` tiebreaker is
    load-bearing, not tidiness. ``created_at`` is ``server_default=func.now()``,
    i.e. transaction start time, so rows created in the same transaction or the
    same clock tick are exact ties; without a deterministic tiebreaker the row
    that was JUST inserted (in the same call, by
    :meth:`TokenService.create_refresh_token`) could land in the evicted set,
    logging a caller out with the very token they were just handed. ``id`` is a
    serial primary key, so higher id is strictly newer regardless of clock
    resolution.

    Args:
        db: Database session. The caller owns the transaction; this does not
            commit (matches :meth:`revoke_all_user_tokens_in_transaction`'s
            contract — the caller decides when the eviction becomes durable).
        user_id: Whose sessions to cap.
        limit: How many of the newest active sessions to keep. A caller wanting
            "make room for one more" passes ``max_concurrent_sessions - 1``.
        batch_limit: Caps how many rows a single call may revoke, bounding the
            per-call cost against a large pre-existing backlog. ``None`` means
            no cap (revoke everything beyond ``limit`` in one statement).

    Returns:
        The JTIs of the sessions this call revoked (empty if none were over the
        limit, or if ``limit`` is negative — the caller should treat a negative
        limit as "keep none", but this function does not clamp it; it is passed
        straight into ``OFFSET``, which raises on a negative value, so a caller
        must clamp to zero itself).
    """
    # One static query, always parameterized — never build SQL by interpolating a
    # clause string (that pattern trips S608 even when, as here, the interpolated
    # value only ever comes from a fixed pair of literals, and the fix that
    # actually addresses the finding is not having a second query shape at all).
    # ``LIMIT NULL`` is Postgres for "no limit", so passing ``batch_limit=None``
    # straight through as a bind parameter covers the unbounded case with the
    # exact same statement.
    query = text(
        """
        UPDATE refresh_token
           SET revoked_at = now()
         WHERE revoked_at IS NULL
           AND id IN (
               SELECT id
                 FROM refresh_token
                WHERE user_id = :user_id
                  AND revoked_at IS NULL
                  AND expires_at > now()
                ORDER BY created_at DESC, id DESC
               OFFSET :limit
                LIMIT :batch_limit
           )
        RETURNING jti
        """
    )

    result = db.execute(
        query, {"user_id": user_id, "limit": max(limit, 0), "batch_limit": batch_limit}
    )
    revoked_jtis = [row[0] for row in result.fetchall()]

    if revoked_jtis:
        logger.info(
            "Session ceiling enforcement revoked %d session(s) for user %s (limit=%d)",
            len(revoked_jtis),
            user_id,
            limit,
        )

    return revoked_jtis


class RevocationError(Exception):
    """A token revocation could not be recorded durably.

    Carries no cause text: callers answer with a fixed code and the original
    exception is chained for the server log only.
    """


class TokenService:
    """
    Service for managing refresh tokens and token revocation.

    Provides methods for:
    - Creating refresh tokens with database persistence
    - Verifying refresh tokens
    - Revoking individual tokens or all user tokens
    - Checking token revocation status via Redis

    Uses Redis for fast token blacklist lookups with automatic TTL expiration.
    Falls back to in-memory storage when Redis is unavailable.
    """

    def __init__(self):
        """Initialize the token service."""
        self._store = None
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """Whether the revocation blacklist is running without shared state.

        True means Redis was unreachable and this process fell back to a
        per-process store. The blacklist is then neither shared across replicas
        nor durable across restarts, so it must not be trusted as the answer to
        "is this token revoked?" — see :meth:`is_token_revoked`.
        """
        return self._degraded

    @property
    def store(self):
        """Lazy-load storage backend (Redis or in-memory fallback)."""
        if self._store is None:
            redis_client = get_redis_client()
            if redis_client:
                self._store = redis_client
                self._degraded = False
            else:
                self._degraded = True
                # Not a warning. Without shared state, revocation stops working
                # across replicas: a token revoked on one replica still passes on
                # every other, so "log out all devices" and password-reset
                # revocation silently mean nothing (issue #324). This used to log
                # at warning level and scrolled past unnoticed.
                record_degradation("token_revocation", "database")
                logger.critical(
                    "Redis unavailable for token revocation — falling back to the "
                    "database for refresh-token revocation checks. Revocation is "
                    "NOT shared across replicas while this persists. Restore Redis; "
                    "on AWS use ElastiCache with Multi-AZ and automatic failover."
                )
                self._store = InMemoryStore()
        return self._store

    @staticmethod
    def _hash_token(token: str) -> str:
        """
        Hash token using SHA-512 for FIPS 140-3 compliance.

        Args:
            token: The token string to hash

        Returns:
            128-character hex string of the SHA-512 hash
        """
        return hashlib.sha512(token.encode()).hexdigest()

    def verify_token_with_fallback(self, token: str) -> dict:
        """
        Verify a refresh token's signature, accepting the dual-algorithm set.

        The accepted algorithms come from ``core.security.accepted_algorithms`` —
        the single owner of that question, shared with ``core.security.verify_token``
        and both request-path verifiers in ``api/endpoints/auth/dependencies.py``.

        This method used to hardcode its own ordered pair gated on ``FIPS_VERSION``
        alone. Because ``FIPS_VERSION`` defaults to ``"140-3"``, that pair happened
        to accept both algorithms on every deployment — which is the only reason
        nobody noticed that :meth:`create_refresh_token` had been signing HS512 on
        non-FIPS installs. It also meant this verifier ignored
        ``FIPS_MIGRATION_MODE`` entirely, so a deployment that had closed its
        migration window still accepted legacy refresh tokens forever.

        Args:
            token: The JWT token string to verify

        Returns:
            The decoded token payload as a dictionary

        Raises:
            JoseError: If token verification fails (including an expired token,
                which raises ``ExpiredTokenError``, a ``JoseError`` subclass).
        """
        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        token_obj = jwt.decode(token, key, algorithms=accepted_algorithms(TOKEN_TYPE_REFRESH))
        # joserfc verifies the signature/algorithm only — exp is not checked
        # automatically (unlike python-jose), so it's validated explicitly here.
        jwt.JWTClaimsRegistry(exp={"essential": True}).validate(token_obj.claims)
        return dict(token_obj.claims)

    def create_token(
        self,
        data: dict,
        expires_delta: timedelta | None = None,
        token_type: str = "access",  # noqa: S107 - JWT type identifier, not a password  # nosec B107
    ) -> str:
        """
        Create a JWT token, signed with ``core.security.signing_algorithm``.

        Creates a JWT token with the provided data and appropriate claims.

        **No production caller reaches this method** — every login path mints its
        access token through ``auth.direct_auth.create_access_token``. It is kept
        because the FIPS suite exercises it, and it is held to the same single-owner
        rule so it cannot become the template for re-introducing a divergent copy.

        Args:
            data: Dictionary of claims to include in the token
            expires_delta: Optional expiration time delta (default: 15 minutes)
            token_type: Type of token ('access' or 'refresh')

        Returns:
            The encoded JWT token string
        """
        to_encode = data.copy()

        if expires_delta:
            expire = datetime.now(UTC) + expires_delta
        else:
            expire = datetime.now(UTC) + timedelta(minutes=15)

        to_encode.update(
            {
                "exp": expire,
                "iat": datetime.now(UTC),
                "jti": str(uuid.uuid4()),
                "type": token_type,
            }
        )

        algorithm = signing_algorithm(token_type)

        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        return jwt.encode({"alg": algorithm}, to_encode, key, algorithms=[algorithm])

    def token_needs_upgrade(self, token: str) -> bool:
        """
        Check whether a token was signed with something other than the current
        algorithm, and so should be re-issued.

        Gated on ``settings.fips_140_3_active`` — i.e. on ``FIPS_MODE``, not on
        ``FIPS_VERSION`` alone. ``FIPS_VERSION`` defaults to ``"140-3"`` on every
        deployment, so the old form reported "needs upgrade" for perfectly current
        tokens on ordinary non-FIPS installs. Same defect shape as
        ``auth/mfa.py:needs_backup_code_regeneration``, fixed the same way.

        Args:
            token: The JWT token string to check

        Returns:
            True if the token is verifiable but was not signed with
            :func:`~app.core.security.signing_algorithm`, on a FIPS 140-3
            deployment. False otherwise.
        """
        if not settings.fips_140_3_active:
            return False
        try:
            key = OctKey.import_key(settings.JWT_SECRET_KEY)
            token_obj = jwt.decode(token, key, algorithms=accepted_algorithms(TOKEN_TYPE_ACCESS))
        except JoseError:
            return False
        return bool(token_obj.header.get("alg") != signing_algorithm(TOKEN_TYPE_ACCESS))

    def create_refresh_token(
        self,
        db: Session,
        user_id: int,
        user_uuid: str,
        role: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
        absolute_expires_at: datetime | None = None,
        *,
        enforce_concurrent_limit: bool = True,
    ) -> tuple[str, RefreshToken]:
        """
        Create a new refresh token for a user.

        Creates a JWT refresh token with:
        - User UUID in 'sub' claim
        - User role in 'role' claim
        - Unique JTI for revocation tracking
        - Token type claim set to 'refresh'

        Stores token hash in database for validation and revocation.

        **This is the universal backstop for the concurrent-session ceiling
        (issue #632).** Every refresh_token row is created here, whatever the
        auth method — local/LDAP login, OIDC, SAML, PKI, the proxy method, MFA
        enrollment, and `account_security_service.reissue_current_session` — so
        this is the one place that has to enforce the cap for all of them to be
        covered. Before this, only `login.py`'s own duplicated check enforced it,
        which meant every other minting path was completely uncapped.

        Args:
            db: Database session
            user_id: User's database ID
            user_uuid: User's UUID string
            role: User's role (for inclusion in token)
            user_agent: Optional user agent for session tracking
            ip_address: Optional IP address for session tracking
            absolute_expires_at: The session's existing hard ceiling, carried
                forward by :meth:`rotate_refresh_token`. ``None`` establishes a
                NEW session and stamps a fresh ceiling — passing the predecessor's
                value is what stops rotation from renewing a session forever.
            enforce_concurrent_limit: Whether this call should enforce
                ``max_concurrent_sessions`` after inserting the new row.
                :meth:`rotate_refresh_token` passes ``False`` explicitly — a
                rotation is not a new session, and capping there would let one
                busy client evict OTHER devices' sessions purely by refreshing
                its own. Every other caller keeps the default ``True``.

        Returns:
            Tuple of (token_string, RefreshToken model instance)
        """
        # Generate unique JTI
        jti = str(uuid.uuid4())

        # Calculate expiration
        now = datetime.now(UTC)
        expires_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
        expires_at = now + expires_delta

        # The session ceiling, and — riding along on the SAME get_auth_settings(db)
        # resolution — the concurrent-session cap this call will enforce below.
        # Only computed when no absolute_expires_at was carried in: that is exactly
        # the "this is a new session, not a rotation" case, which is also the only
        # case enforcement ever applies (rotate_refresh_token always passes
        # enforce_concurrent_limit=False alongside its carried-forward ceiling).
        max_concurrent_sessions = 0
        if absolute_expires_at is None:
            _idle_minutes, absolute_minutes, max_concurrent_sessions = _session_lifetime_minutes(db)
            absolute_expires_at = now + timedelta(minutes=absolute_minutes)

        # Create token payload
        token_data = {
            "sub": user_uuid,
            "role": role,
            "jti": jti,
            "iat": now,
            "exp": expires_at,
            "type": "refresh",
        }

        # One owner for "which algorithm signs this", shared with every other issuer.
        #
        # This line read `JWT_ALGORITHM_V3 if FIPS_VERSION == "140-3" else "HS256"`,
        # gated on FIPS_VERSION — which defaults to "140-3". So EVERY deployment,
        # FIPS or not, signed its refresh tokens with HS512 while its access tokens
        # were HS256. It was invisible because the only refresh verifier tried both
        # algorithms; the visible symptom was that `POST /auth/logout` presented with
        # a refresh token (which sessions.py explicitly supports — "logging out with a
        # refresh token is still a logout") decoded with `[settings.JWT_ALGORITHM]`
        # and therefore never revoked anything.
        algorithm = signing_algorithm(TOKEN_TYPE_REFRESH)
        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        token = jwt.encode({"alg": algorithm}, token_data, key, algorithms=[algorithm])

        # Hash token for storage
        token_hash = self._hash_token(token)

        # Create database record
        refresh_token = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            jti=jti,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
            last_activity_at=now,
            absolute_expires_at=absolute_expires_at,
        )

        db.add(refresh_token)
        # Flush (not commit) first: the new row needs an id and to be visible to the
        # ceiling UPDATE below, but the insert and the eviction must land in ONE
        # transaction — flush-then-enforce-then-commit is what closes the window a
        # separate commit would leave between "session created" and "cap enforced".
        db.flush()

        # Deliberate: this enforces a ceiling regardless of
        # `concurrent_session_policy` ("terminate_oldest" vs "reject"). That
        # policy is `login.py`'s alone to interpret — it decides whether an
        # interactive login at the cap gets a 429 or an eviction. Every OTHER
        # minting path reaching this backstop (OIDC, SAML, PKI, proxy, MFA
        # enrollment, `reissue_current_session`) enforced NOTHING before #632, so
        # for them the choice is "ceiling enforcement" vs "no enforcement at
        # all", not "ceiling enforcement" vs "reject" — and leaving them
        # uncapped under a deployment's "reject" policy is worse than capping
        # them. `login.py` never reaches this branch with a full session in the
        # reject case, since it raises 429 before calling `_generate_login_tokens`.
        if enforce_concurrent_limit and max_concurrent_sessions > 0:
            enforce_session_ceiling(
                db,
                user_id,
                max_concurrent_sessions,
                batch_limit=SESSION_CAP_EVICTION_BATCH_LIMIT,
            )

        db.commit()
        db.refresh(refresh_token)

        logger.info(
            f"Created refresh token for user {user_id} "
            f"(jti={jti[:8]}..., expires={expires_at.isoformat()})"
        )

        # Single choke point: every session (refresh_token row) is created here,
        # whatever the auth method or whether this is a fresh login or a rotation
        # — so this is the one place that needs to emit it, not every call site.
        audit_logger.log(
            event_type=AuditEventType.AUTH_SESSION_CREATED,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            source_ip=ip_address,
            user_agent=user_agent,
            details={"jti": jti},
        )

        return token, refresh_token

    def verify_refresh_token(
        self, db: Session, token: str
    ) -> tuple[dict | None, RefreshToken | None]:
        """
        Verify a refresh token and return its payload.

        Validation steps:
        1. Decode and verify JWT signature
        2. Check token type is 'refresh'
        3. Check token hash exists in database
        4. Check token is not revoked in database
        5. Check token is not on Redis blacklist
        6. Check token is not expired
        7. Check the SESSION is still within its idle and absolute caps

        Step 7 is what makes ``session_idle_timeout_minutes`` and
        ``session_absolute_timeout_minutes`` real. It fires once per access-token
        lifetime rather than per request, deliberately: per-request activity
        tracking would be refreshed continuously by polling endpoints (progress,
        notifications, task status) and WebSocket keepalives, so idle timeout
        would appear to work and never actually fire — worse than not having it,
        because it reads as a satisfied control.

        Args:
            db: Database session
            token: The refresh token string to verify

        Returns:
            Tuple of (payload_dict, RefreshToken model) if valid, (None, None) if invalid
        """
        try:
            # Decode token with algorithm fallback for FIPS 140-3 migration
            payload = self.verify_token_with_fallback(token)

            # Verify token type
            if payload.get("type") != "refresh":
                logger.warning("Token verification failed: not a refresh token")
                return None, None

            jti = payload.get("jti")
            if not jti:
                logger.warning("Token verification failed: missing JTI")
                return None, None

            # Check Redis blacklist first (fast path)
            if self.is_token_revoked(jti, db=db):
                logger.warning(f"Token verification failed: JTI {jti[:8]}... is revoked")
                return None, None

            # Find token in database
            token_hash = self._hash_token(token)
            refresh_token = (
                db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
            )

            if not refresh_token:
                logger.warning("Token verification failed: token not found in database")
                return None, None

            # Check database revocation status
            if refresh_token.is_revoked:
                # is_revoked is True iff revoked_at is not None (see model property)
                assert refresh_token.revoked_at is not None
                logger.warning(
                    f"Token verification failed: token revoked at "
                    f"{refresh_token.revoked_at.isoformat()}"
                )
                return None, None

            # Check expiration (should be caught by JWT decode, but double-check)
            if refresh_token.is_expired:
                logger.warning("Token verification failed: token expired")
                audit_logger.log(
                    event_type=AuditEventType.AUTH_SESSION_EXPIRED,
                    outcome=AuditOutcome.FAILURE,
                    user_id=refresh_token.user_id,
                    details={"jti": jti},
                )
                return None, None

            if not self._session_within_lifetime(db, refresh_token):
                return None, None

            logger.debug(f"Refresh token verified successfully (jti={jti[:8]}...)")
            return payload, refresh_token

        except ExpiredTokenError:
            logger.warning("Token verification failed: token expired")
            return None, None
        except JoseError as e:
            logger.warning(f"Token verification failed: JWT error - {e}")
            return None, None
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            return None, None

    @staticmethod
    def _session_within_lifetime(db: Session, refresh_token: RefreshToken) -> bool:
        """Whether *refresh_token*'s session is still inside its idle and absolute caps.

        Both columns are nullable and a NULL is treated as "no cap recorded", not
        as an expiry. Sessions that predate ``v377`` carry NULL in both, and
        invalidating them on upgrade would sign every user out for a second time
        in the same release (the token-type change already does it once). The next
        rotation stamps real values on the successor row.

        Args:
            db: Database session, used to resolve the DB-backed timeouts.
            refresh_token: The stored session row being verified.

        Returns:
            True when the session may continue.
        """
        now = datetime.now(UTC)

        absolute_expires_at = _as_utc(refresh_token.absolute_expires_at)
        if absolute_expires_at is not None and now > absolute_expires_at:
            logger.info(
                "Session refused: absolute timeout reached at %s (jti=%s...)",
                absolute_expires_at.isoformat(),
                str(refresh_token.jti)[:8],
            )
            return False

        idle_minutes, _absolute_minutes, _max_concurrent_sessions = _session_lifetime_minutes(db)
        last_activity_at = _as_utc(refresh_token.last_activity_at)
        # 0 disables the idle cap. The admin UI's schema bounds it at 1, but
        # SESSION_IDLE_TIMEOUT_MINUTES is a plain .env integer with no bound, and
        # `now - last_activity > 0` would refuse every session on sight.
        idle_exceeded = (
            idle_minutes > 0
            and last_activity_at is not None
            and now - last_activity_at > timedelta(minutes=idle_minutes)
        )
        if idle_exceeded:
            logger.info(
                "Session refused: idle for more than %s minutes (last activity %s, jti=%s...)",
                idle_minutes,
                last_activity_at.isoformat() if last_activity_at else "unknown",
                str(refresh_token.jti)[:8],
            )
            return False

        return True

    def revoke_token(self, db: Session, jti: str, expires_at: datetime | None = None) -> None:
        """
        Revoke a token, recording it in the database first and the cache second.

        ``refresh_token.revoked_at`` is the durable record and Redis is a cache of
        it (readers fall back to the row while Redis is unreachable), so the row is
        written and committed first. A cache failure afterwards is logged and
        metered but does not undo the revocation. A JTI with no row (an access
        token) has nothing durable to write, so for it the cache write is the
        record and its failure is reported.

        Args:
            db: Database session
            jti: The JWT ID to revoke
            expires_at: Token expiration time (for TTL calculation)

        Raises:
            RevocationError: If the revocation could not be recorded.
        """
        if expires_at:
            ttl_seconds = max(1, int((expires_at - datetime.now(UTC)).total_seconds()))
        else:
            # Default to refresh token expiry if no expiration provided
            ttl_seconds = settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

        try:
            refresh_token = db.query(RefreshToken).filter(RefreshToken.jti == jti).first()
            owner_id = None
            if refresh_token:
                owner_id = refresh_token.user_id
                refresh_token.revoked_at = datetime.now(UTC)  # type: ignore[assignment]
                db.commit()
        except Exception as exc:
            logger.error("Could not record revocation for token (jti=%s...)", jti[:8])
            try:
                db.rollback()
            except Exception:
                logger.debug("Rollback after failed revocation also failed", exc_info=True)
            raise RevocationError("revocation could not be recorded") from exc

        try:
            self.store.set(f"{REVOKED_TOKEN_PREFIX}{jti}", "revoked", ex=ttl_seconds)
        except Exception as exc:
            record_degradation("token_revocation", "database")
            if refresh_token is None:
                logger.critical(
                    "Could not record revocation for token (jti=%s...): cache unreachable "
                    "and no database record exists.",
                    jti[:8],
                )
                raise RevocationError("revocation could not be recorded") from exc
            logger.critical(
                "Cache write failed for revoked token (jti=%s...); the database record "
                "stands and readers fall back to it.",
                jti[:8],
            )

        logger.info(f"Revoked token (jti={jti[:8]}..., ttl={ttl_seconds}s)")

        # Single choke point for every revocation path (logout, logout-all,
        # admin termination, password reset, federated SAML/OIDC logout) —
        # covering it here means none of those call sites has to remember to.
        audit_logger.log(
            event_type=AuditEventType.AUTH_TOKEN_REVOKE,
            outcome=AuditOutcome.SUCCESS,
            user_id=owner_id,
            details={"jti": jti},
        )

    def stamp_user_revocation_epoch(self, user_uuid: str | None) -> bool:
        """Invalidate every access token already issued to *user_uuid*.

        Access tokens are stateless, so there is no JTI list to blacklist. This
        records "nothing issued before now is acceptable" instead, and
        :meth:`is_token_revoked` compares each token's ``iat`` against it.

        The key only needs to outlive the longest access token, so it expires
        with a small margin over ``JWT_ACCESS_TOKEN_EXPIRE_MINUTES``. Best-effort:
        a Redis failure here degrades to the database check
        (``is_revoked_from_database``), which reaches the same conclusion from
        the user's revoked refresh tokens.

        Returns:
            True when the epoch was recorded.
        """
        if not user_uuid:
            return False
        ttl = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60 + 60
        try:
            self.store.set(
                f"{USER_REVOCATION_EPOCH_PREFIX}{user_uuid}",
                str(int(datetime.now(UTC).timestamp())),
                ex=ttl,
            )
        except Exception as exc:
            record_degradation("token_revocation", "database")
            logger.warning("Could not stamp revocation epoch for user %s: %s", user_uuid, exc)
            return False
        return True

    def _issued_before_user_epoch(
        self,
        jti: str,
        db: Session | None,
        user_uuid: str | None,
        issued_at: int | None,
    ) -> bool:
        """Whether an access token predates the user's revocation epoch.

        When the epoch cannot be read, the answer comes from the database
        (``is_revoked_from_database``), which refuses only on positive
        evidence that every session of the user was revoked.
        """
        if not user_uuid or issued_at is None or self._degraded:
            return False
        try:
            marker = self.store.get(f"{USER_REVOCATION_EPOCH_PREFIX}{user_uuid}")
        except Exception:
            logger.critical(
                "Redis lookup failed for the revocation epoch (jti=%s...); falling back "
                "to the database.",
                jti[:8],
            )
            return is_revoked_from_database(db, jti, user_uuid)
        if marker is None:
            return False
        try:
            epoch = int(marker.decode() if isinstance(marker, bytes) else marker)
        except (TypeError, ValueError):
            return False
        # `<` not `<=`: a token minted in the same second as the epoch is the one
        # a re-authentication just issued, and must not be killed by its own stamp.
        return issued_at < epoch

    def revoke_all_user_tokens_in_transaction(
        self, db: Session, user_id: int, user_uuid: str | None = None, reason: str | None = None
    ) -> int:
        """
        Revoke all refresh tokens for a user **without** committing.

        The caller owns the transaction and MUST commit. Exceptions propagate
        rather than being swallowed, so a caller that is changing credentials in
        the same transaction can abort the whole unit of work instead of
        reporting success for changes that were rolled back underneath it.

        The database rows are the record of the revocation; the cache writes
        (per-token keys and the user epoch) are best-effort, and a cache failure
        is logged and metered rather than aborting the revocation.

        Args:
            db: Database session (transaction owned by the caller)
            user_id: User ID whose tokens should be revoked
            user_uuid: The user's UUID, resolved from *user_id* when omitted. It
                is what the revocation epoch is keyed on.
            reason: Free-text cause, carried into the audit record. The callers
                that have one (``account_security_service.revoke_all_sessions``
                and everything above it — admin password reset, role change,
                lock, MFA reset, SCIM deactivation, directory sync) already
                phrase it for their log line; passing it here is what turns
                "sessions were revoked" into "sessions were revoked *because*".

        Returns:
            Number of tokens revoked
        """
        pending = self.revoke_all_user_tokens_pending_audit(db, user_id, user_uuid)
        pending.record_audit(reason)
        return pending.count

    def revoke_all_user_tokens_pending_audit(
        self, db: Session, user_id: int, user_uuid: str | None = None
    ) -> PendingBulkRevocation:
        """Like :meth:`revoke_all_user_tokens_in_transaction`, but the audit is the caller's.

        For a caller that wants the ``AUTH_TOKEN_REVOKE`` SUCCESS record written
        only once its own commit has succeeded: call
        :meth:`PendingBulkRevocation.record_audit` after the commit.
        """
        tokens = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .all()
        )

        now = datetime.now(UTC)
        count = 0

        for token in tokens:
            # Add to Redis blacklist. Not transactional — if the commit later
            # fails, these keys stay set. Revoking a token that turns out to
            # still be valid fails closed (the user re-authenticates), which is
            # the safe direction for the inverse mistake. A cache failure does not
            # stop the row update below: readers fall back to the row.
            ttl_seconds = max(1, int((token.expires_at - now).total_seconds()))
            key = f"{REVOKED_TOKEN_PREFIX}{token.jti}"
            try:
                self.store.set(key, "revoked", ex=ttl_seconds)
            except Exception:
                record_degradation("token_revocation", "database")
                logger.critical(
                    "Cache write failed for revoked token (jti=%s...); the database record stands.",
                    str(token.jti)[:8],
                )

            # Update database record
            token.revoked_at = now  # type: ignore[assignment]
            count += 1

        # Access tokens have no rows to revoke — kill them by epoch instead.
        resolved_uuid = user_uuid or self._user_uuid_for(db, user_id)
        epoch_stamped = self.stamp_user_revocation_epoch(resolved_uuid)

        return PendingBulkRevocation(
            user_id=user_id, count=count, user_uuid=resolved_uuid, epoch_stamped=epoch_stamped
        )

    @staticmethod
    def _user_uuid_for(db: Session, user_id: int) -> str | None:
        """Resolve a user's UUID, for callers that only have the integer id."""
        row = db.query(User.uuid).filter(User.id == user_id).first()
        return str(row[0]) if row else None

    def is_token_revoked(
        self,
        jti: str,
        db: Session | None = None,
        user_uuid: str | None = None,
        issued_at: int | None = None,
    ) -> bool:
        """
        Check if a token JTI is on the revocation blacklist.

        Redis is a **cache** here, not the system of record: ``refresh_token``
        carries a durable ``revoked_at``. So when Redis is unreachable this does
        NOT trust the per-process fallback store (which is empty on a fresh
        process and unshared between replicas, i.e. it answers "not revoked" to
        everything). It consults the database instead — authoritative, durable,
        and shared by every replica — and denies when it cannot (issue #324).

        Args:
            jti: The JWT ID to check
            db: Session used for the authoritative fallback. Without it, a
                degraded check fails **closed**.
            user_uuid: Subject of the token. Lets the fallback answer for
                *access* tokens, which have no ``refresh_token`` row of their own.

        Returns:
            True if revoked (or if revocation cannot be verified), False if valid
        """
        if not settings.TOKEN_REVOCATION_ENABLED:
            return False

        key = f"{REVOKED_TOKEN_PREFIX}{jti}"

        # Touch `store` first: it sets `_degraded` on the first failed connect.
        store = self.store
        if self._degraded:
            return is_revoked_from_database(db, jti, user_uuid)

        try:
            result = store.get(key)
        except Exception:
            # Redis was reachable at startup and died since.
            self._degraded = True
            logger.critical(
                "Redis lookup failed for token revocation (jti=%s...); falling back "
                "to the database. Revocation is NOT shared across replicas while "
                "this persists.",
                jti[:8],
            )
            return is_revoked_from_database(db, jti, user_uuid)
        if result is not None:
            return True

        # A stateless access token has no JTI entry of its own; the per-user
        # epoch is what makes logout-all / password reset / admin termination
        # take effect on it immediately rather than at its natural expiry.
        return self._issued_before_user_epoch(jti, db, user_uuid, issued_at)

    def cleanup_expired_tokens(self, db: Session) -> int:
        """
        Remove expired refresh tokens from database.

        Should be called periodically (e.g., via Celery beat) to clean up
        expired tokens that are no longer needed.

        Args:
            db: Database session

        Returns:
            Number of tokens deleted
        """
        try:
            now = datetime.now(UTC)
            result = (
                db.query(RefreshToken)
                .filter(RefreshToken.expires_at < now)
                .delete(synchronize_session=False)
            )
            db.commit()
            logger.info(f"Cleaned up {result} expired refresh tokens")
            return result  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Error cleaning up expired tokens: {e}")
            db.rollback()
            return 0

    def get_user_active_sessions(self, db: Session, user_id: int) -> list[dict]:
        """
        Get all active (non-revoked, non-expired) sessions for a user.

        Useful for displaying active sessions in user settings.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            List of session info dicts with created_at, user_agent, ip_address
        """
        now = datetime.now(UTC)
        tokens = (
            db.query(RefreshToken)
            .filter(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .order_by(RefreshToken.created_at.desc())
            .all()
        )

        return [
            {
                "jti": token.jti,
                "created_at": token.created_at.isoformat(),
                "expires_at": token.expires_at.isoformat(),
                # Null on a session established before v377 — "no cap recorded",
                # not "never expires"; the next rotation stamps both.
                "last_activity_at": (
                    token.last_activity_at.isoformat() if token.last_activity_at else None
                ),
                "absolute_expires_at": (
                    token.absolute_expires_at.isoformat() if token.absolute_expires_at else None
                ),
                "user_agent": token.user_agent,
                "ip_address": token.ip_address,
            }
            for token in tokens
        ]

    def rotate_refresh_token(
        self,
        db: Session,
        old_token: str,
        old_token_record: RefreshToken,
        user_id: int,
        user_uuid: str,
        role: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[str, RefreshToken]:
        """
        Rotate a refresh token by revoking the old one and creating a new one.

        This implements OAuth 2.1 refresh token rotation best practice:
        - Revokes the old refresh token immediately
        - Creates a new refresh token with fresh expiration
        - Limits the impact of stolen refresh tokens

        The successor row inherits ``absolute_expires_at`` **unchanged**, so the
        session's ceiling is fixed by the login that established it. Recomputing
        it here would mean a client that refreshes before every expiry never has
        to re-authenticate, which is exactly the gap this column closes.
        ``last_activity_at``, by contrast, is stamped fresh on the new row — that
        is what "activity" means for the idle cap.

        Args:
            db: Database session
            old_token: The old refresh token string
            old_token_record: The RefreshToken model instance to revoke
            user_id: User's database ID
            user_uuid: User's UUID string
            role: User's role (for inclusion in token)
            user_agent: Optional user agent for session tracking
            ip_address: Optional IP address for session tracking

        Returns:
            Tuple of (new_token_string, new_RefreshToken model instance)
        """
        # Create new refresh token FIRST (atomic safety: if this fails, user keeps old token)
        # A legacy row (pre-v377) carries NULL here; passing it through means the
        # successor gets a freshly-stamped ceiling rather than the session being
        # refused, which is how NULL is grandfathered.
        #
        # enforce_concurrent_limit=False: a rotation is not a new session, it is the
        # SAME session continuing. Enforcing the cap here would let one busy client
        # evict OTHER devices' sessions purely by refreshing its own token — the cap
        # is a login-time and backstop-time control, not a per-refresh one (issue #632).
        new_token, new_token_record = self.create_refresh_token(
            db=db,
            user_id=user_id,
            user_uuid=user_uuid,
            role=role,
            user_agent=user_agent,
            ip_address=ip_address,
            absolute_expires_at=_as_utc(old_token_record.absolute_expires_at),
            enforce_concurrent_limit=False,
        )

        # Read now: a failed revocation below rolls the session back and expires
        # this object, and a lazy reload could itself fail.
        new_jti = str(new_token_record.jti)
        new_expires = new_token_record.expires_at
        logger.info(f"Issued new refresh token for user {user_id} (new_jti={new_jti[:8]}...)")

        # Revoke the old token AFTER the new one exists; if that fails the successor
        # is withdrawn again below and the caller answers with an error.
        old_jti = str(old_token_record.jti)
        old_expires = datetime(
            old_token_record.expires_at.year,
            old_token_record.expires_at.month,
            old_token_record.expires_at.day,
            old_token_record.expires_at.hour,
            old_token_record.expires_at.minute,
            old_token_record.expires_at.second,
            old_token_record.expires_at.microsecond,
            old_token_record.expires_at.tzinfo,
        )
        try:
            self.revoke_token(db, old_jti, old_expires)
        except RevocationError:
            # The old token is still valid, so the successor must not outlive this
            # call: the caller answers with an error and the client retries with
            # the token it still holds.
            try:
                self.revoke_token(db, new_jti, new_expires)
            except RevocationError:
                logger.error("Could not withdraw the successor of a rotation that failed")
            raise

        logger.info(f"Rotated out old refresh token for user {user_id} (old_jti={old_jti[:8]}...)")

        return new_token, new_token_record


# Module-level singleton
token_service = TokenService()
