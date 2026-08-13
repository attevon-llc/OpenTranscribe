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
- Dual JWT verification for FIPS 140-3 migration (HS512/HS256 fallback)
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
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.session import InMemoryStore
from app.auth.session import get_redis_client
from app.core.config import settings
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


def _session_lifetime_minutes(db: Session) -> tuple[int, int]:
    """Return ``(idle_timeout_minutes, absolute_timeout_minutes)`` for this deployment.

    DB-backed (admin UI) over ``.env`` over the coded default, like the rest of the
    auth configuration. Never raises: a configuration read must not be able to
    break token issue or verification, so an unavailable database degrades to the
    environment values.
    """
    try:
        from app.core.auth_settings import get_auth_settings

        auth_settings = get_auth_settings(db)
        return (
            auth_settings.session_idle_timeout_minutes,
            auth_settings.session_absolute_timeout_minutes,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not read session lifetime config; using .env values", exc_info=True)
        return (
            settings.SESSION_IDLE_TIMEOUT_MINUTES,
            settings.SESSION_ABSOLUTE_TIMEOUT_MINUTES,
        )


def _record_degradation(control: str, fallback: str) -> None:
    """Count a security control running without its shared state store.

    Imported lazily and never allowed to raise: metrics must not be able to break
    an authentication path. Prometheus is optional in some deployments, and this
    runs on the request path for every revocation check while Redis is down.
    """
    try:
        from app.core.metrics import security_state_degraded_total

        security_state_degraded_total.labels(control=control, fallback=fallback).inc()
    except Exception:  # pragma: no cover - metrics must never break auth
        # Swallowed on purpose, but never silently: PR #322 removed the blanket
        # S110 exemption precisely because silent handlers hide real faults. A
        # broken counter must not turn into a failed login, so this degrades to a
        # log line instead of propagating.
        logger.debug("Could not record security degradation metric", exc_info=True)


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
                _record_degradation("token_revocation", "database")
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
        Verify JWT token with algorithm fallback for FIPS 140-3 migration.

        Tries HS512 first (FIPS 140-3), falls back to HS256 (legacy).
        This allows for seamless migration from legacy tokens to FIPS 140-3
        compliant tokens without forcing all users to re-authenticate.

        Args:
            token: The JWT token string to verify

        Returns:
            The decoded token payload as a dictionary

        Raises:
            JoseError: If token verification fails with all algorithms
        """
        algorithms_to_try = []

        # FIPS 140-3 uses HS512
        if settings.FIPS_VERSION == "140-3":
            algorithms_to_try = ["HS512", "HS256"]
        else:
            algorithms_to_try = ["HS256", "HS512"]

        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        last_error = None
        for algorithm in algorithms_to_try:
            try:
                token_obj = jwt.decode(token, key, algorithms=[algorithm])
                # joserfc verifies the signature/algorithm only — exp is not
                # checked automatically (unlike python-jose), so it's validated
                # explicitly here.
                jwt.JWTClaimsRegistry(exp={"essential": True}).validate(token_obj.claims)
                return dict(token_obj.claims)
            except JoseError as e:
                last_error = e
                continue

        raise last_error or JoseError("Token verification failed")

    def create_token(
        self,
        data: dict,
        expires_delta: timedelta | None = None,
        token_type: str = "access",  # noqa: S107 - JWT type identifier, not a password  # nosec B107
    ) -> str:
        """
        Create JWT token with FIPS-compliant algorithm.

        Creates a JWT token with the provided data and appropriate claims.
        Uses HS512 algorithm when FIPS 140-3 mode is enabled, otherwise HS256.

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

        # Use FIPS 140-3 compliant algorithm. FIPS_MODE is the master switch —
        # FIPS_VERSION alone defaults to "140-3" even on non-FIPS deployments
        # (keeps token creation consistent with core/security.create_access_token).
        algorithm = (
            settings.JWT_ALGORITHM_V3
            if settings.FIPS_MODE and settings.FIPS_VERSION == "140-3"
            else "HS256"
        )

        key = OctKey.import_key(settings.JWT_SECRET_KEY)
        return jwt.encode({"alg": algorithm}, to_encode, key, algorithms=[algorithm])

    def token_needs_upgrade(self, token: str) -> bool:
        """
        Check if token was issued with legacy algorithm and needs upgrade.

        Used during migration to FIPS 140-3 to identify tokens that should
        be re-issued with the new HS512 algorithm.

        Args:
            token: The JWT token string to check

        Returns:
            True if token is legacy (HS256) and system is in FIPS 140-3 mode,
            False otherwise
        """
        try:
            # Try to decode with HS256 only - if it works, token is legacy
            key = OctKey.import_key(settings.JWT_SECRET_KEY)
            jwt.decode(token, key, algorithms=["HS256"])
            # If we're in FIPS 140-3 mode, this token needs upgrade
            return settings.FIPS_VERSION == "140-3"
        except JoseError:
            return False

    def create_refresh_token(
        self,
        db: Session,
        user_id: int,
        user_uuid: str,
        role: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
        absolute_expires_at: datetime | None = None,
    ) -> tuple[str, RefreshToken]:
        """
        Create a new refresh token for a user.

        Creates a JWT refresh token with:
        - User UUID in 'sub' claim
        - User role in 'role' claim
        - Unique JTI for revocation tracking
        - Token type claim set to 'refresh'

        Stores token hash in database for validation and revocation.

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

        Returns:
            Tuple of (token_string, RefreshToken model instance)
        """
        # Generate unique JTI
        jti = str(uuid.uuid4())

        # Calculate expiration
        now = datetime.now(UTC)
        expires_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
        expires_at = now + expires_delta

        # The session ceiling. Only computed when none was carried in, so a
        # rotated session keeps the ceiling of the login that established it.
        if absolute_expires_at is None:
            _idle_minutes, absolute_minutes = _session_lifetime_minutes(db)
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

        # Encode token with FIPS-compliant algorithm
        algorithm = settings.JWT_ALGORITHM_V3 if settings.FIPS_VERSION == "140-3" else "HS256"
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
        as an expiry. Sessions that predate ``v375`` carry NULL in both, and
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

        idle_minutes, _absolute_minutes = _session_lifetime_minutes(db)
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

    def revoke_token(self, db: Session, jti: str, expires_at: datetime | None = None) -> bool:
        """
        Revoke a token by adding its JTI to the Redis blacklist.

        Args:
            db: Database session
            jti: The JWT ID to revoke
            expires_at: Token expiration time (for TTL calculation)

        Returns:
            True if revoked successfully, False otherwise
        """
        try:
            # Calculate TTL (remaining token lifetime)
            if expires_at:
                now = datetime.now(UTC)
                ttl_seconds = max(1, int((expires_at - now).total_seconds()))
            else:
                # Default to refresh token expiry if no expiration provided
                ttl_seconds = settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

            # Add to Redis blacklist
            key = f"{REVOKED_TOKEN_PREFIX}{jti}"
            self.store.set(key, "revoked", ex=ttl_seconds)

            # Update database record if exists
            refresh_token = db.query(RefreshToken).filter(RefreshToken.jti == jti).first()
            if refresh_token:
                refresh_token.revoked_at = datetime.now(UTC)  # type: ignore[assignment]
                db.commit()

            logger.info(f"Revoked token (jti={jti[:8]}..., ttl={ttl_seconds}s)")

            # Single choke point for every revocation path (logout, logout-all,
            # admin termination, password reset, federated SAML/OIDC logout) —
            # covering it here means none of those call sites has to remember to.
            audit_logger.log(
                event_type=AuditEventType.AUTH_TOKEN_REVOKE,
                outcome=AuditOutcome.SUCCESS,
                user_id=refresh_token.user_id if refresh_token else None,
                details={"jti": jti},
            )
            return True

        except Exception as e:
            logger.error(f"Error revoking token: {e}")
            return False

    def stamp_user_revocation_epoch(self, user_uuid: str | None) -> None:
        """Invalidate every access token already issued to *user_uuid*.

        Access tokens are stateless, so there is no JTI list to blacklist. This
        records "nothing issued before now is acceptable" instead, and
        :meth:`is_token_revoked` compares each token's ``iat`` against it.

        The key only needs to outlive the longest access token, so it expires
        with a small margin over ``JWT_ACCESS_TOKEN_EXPIRE_MINUTES``. Best-effort:
        a Redis failure here degrades to the database heuristic in
        :meth:`_is_revoked_without_redis`, which reaches the same conclusion from
        the user's revoked refresh tokens.
        """
        if not user_uuid:
            return
        ttl = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60 + 60
        try:
            self.store.set(
                f"{USER_REVOCATION_EPOCH_PREFIX}{user_uuid}",
                str(int(datetime.now(UTC).timestamp())),
                ex=ttl,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not stamp revocation epoch for user %s: %s", user_uuid, exc)

    def _issued_before_user_epoch(self, user_uuid: str | None, issued_at: int | None) -> bool:
        """Whether an access token predates the user's revocation epoch."""
        if not user_uuid or issued_at is None or self._degraded:
            return False
        try:
            marker = self.store.get(f"{USER_REVOCATION_EPOCH_PREFIX}{user_uuid}")
        except Exception:
            return False
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

        Use this whenever ``db`` already holds uncommitted work. Use
        :meth:`revoke_all_user_tokens` only for a standalone revocation.

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
            # the safe direction for the inverse mistake.
            ttl_seconds = max(1, int((token.expires_at - now).total_seconds()))
            key = f"{REVOKED_TOKEN_PREFIX}{token.jti}"
            self.store.set(key, "revoked", ex=ttl_seconds)

            # Update database record
            token.revoked_at = now  # type: ignore[assignment]
            count += 1

        # Access tokens have no rows to revoke — kill them by epoch instead.
        resolved_uuid = user_uuid or self._user_uuid_for(db, user_id)
        self.stamp_user_revocation_epoch(resolved_uuid)

        # MASS revocation is audited here, not left to the callers.
        #
        # :meth:`revoke_token` calls itself "the single choke point for every
        # revocation path" and audits accordingly — but it is the SINGLE-token
        # path, and this method is not routed through it. So the bulk path, which
        # is the one admin password reset, role change, lock, MFA reset, SCIM
        # deactivation and directory sync all use, produced no audit record at
        # all (FedRAMP AU-2/AU-12).
        #
        # ``user_id``/``username`` name the TARGET, matching :meth:`revoke_token`
        # above (which stamps the token owner) — this layer has no request and
        # therefore no actor. Callers that DO know the actor emit their own
        # actor-keyed record alongside; the two are correlated by ``request_id``.
        # It fires even when ``count`` is 0: the epoch stamp above still happened
        # and still invalidated every outstanding access token, so "0 sessions"
        # is a real revocation, not a no-op.
        audit_logger.log(
            event_type=AuditEventType.AUTH_TOKEN_REVOKE,
            outcome=AuditOutcome.SUCCESS,
            user_id=user_id,
            details={
                "scope": "all_user_tokens",
                "sessions_revoked": count,
                "reason": reason,
                "user_uuid": resolved_uuid,
            },
        )

        return count

    @staticmethod
    def _user_uuid_for(db: Session, user_id: int) -> str | None:
        """Resolve a user's UUID, for callers that only have the integer id."""
        row = db.query(User.uuid).filter(User.id == user_id).first()
        return str(row[0]) if row else None

    def revoke_all_user_tokens(self, db: Session, user_id: int) -> int:
        """
        Revoke all refresh tokens for a user, committing on success.

        Best-effort: returns 0 and logs on failure rather than raising.

        Used for:
        - Logout from all devices
        - Account security concerns

        **Do not call this while ``db`` holds uncommitted work.** It commits, and
        on failure it rolls the whole session back — which silently discards the
        caller's unrelated changes. ``confirm_password_reset`` used to do exactly
        that: a failure here reverted the new password hash while the caller went
        on to report success (issue #324). Use
        :meth:`revoke_all_user_tokens_in_transaction` for that case.

        Args:
            db: Database session
            user_id: User ID whose tokens should be revoked

        Returns:
            Number of tokens revoked, or 0 if the revocation failed
        """
        try:
            count = self.revoke_all_user_tokens_in_transaction(db, user_id)
            db.commit()
            logger.info(f"Revoked {count} tokens for user {user_id}")
            return count

        except Exception:
            logger.exception(f"Error revoking user tokens for user {user_id}")
            db.rollback()
            return 0

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
            return self._is_revoked_without_redis(jti, db, user_uuid)

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
            return self._is_revoked_without_redis(jti, db, user_uuid)
        if result is not None:
            return True

        # A stateless access token has no JTI entry of its own; the per-user
        # epoch is what makes logout-all / password reset / admin termination
        # take effect on it immediately rather than at its natural expiry.
        return self._issued_before_user_epoch(user_uuid, issued_at)

    def _is_revoked_without_redis(
        self,
        jti: str,
        db: Session | None,
        user_uuid: str | None,
    ) -> bool:
        """Answer "is this token revoked?" from the database instead of Redis.

        Cache-aside: on cache failure, consult the system of record. Returning
        the per-process store's answer instead would report "not revoked" for
        everything, because that store is empty in a fresh process and is never
        shared between replicas.

        Two cases, because access tokens have no ``refresh_token`` row:

        1. **The JTI is a refresh token.** ``revoked_at`` is authoritative.
        2. **The JTI is an access token.** Nothing durable records it, so fall
           back to the user: if every one of their refresh tokens is revoked,
           their sessions were terminated (logout-all, password reset, admin
           action) and this access token goes with them. That covers the reason
           revocation is actually used. Access tokens are short-lived, so an
           unrevoked-user access token is bounded by its own expiry anyway.

        Without a session, or if the query itself fails, this **denies**. A
        wrongly-denied request costs a re-authentication; a wrongly-allowed one
        honours a token someone explicitly revoked.
        """
        if db is None:
            _record_degradation("token_revocation", "deny")
            logger.critical(
                "Cannot verify revocation for jti=%s... — Redis is down and no database "
                "session was supplied. Denying (fail closed).",
                jti[:8],
            )
            return True

        try:
            row = db.query(RefreshToken).filter(RefreshToken.jti == jti).first()
            if row is not None:
                _record_degradation("token_revocation", "database")
                return row.revoked_at is not None

            if user_uuid is None:
                _record_degradation("token_revocation", "deny")
                return True

            # Access token: no durable row of its own, so infer from the user's
            # sessions — but only on POSITIVE evidence of revocation.
            #
            # "This user has no refresh tokens" is NOT such evidence. Some auth
            # paths never mint one, so treating absence as revocation would lock
            # those users out every time Redis blinked. Deny only when the user
            # demonstrably had sessions and every one of them was revoked, which
            # is what logout-all / password reset / admin termination produce.
            # Otherwise allow, bounded by the access token's own short expiry.
            has_any = (
                db.query(RefreshToken)
                .join(User, User.id == RefreshToken.user_id)
                .filter(User.uuid == user_uuid)
                .first()
            )
            _record_degradation("token_revocation", "database")
            if has_any is None:
                return False

            live = (
                db.query(RefreshToken)
                .join(User, User.id == RefreshToken.user_id)
                .filter(
                    User.uuid == user_uuid,
                    RefreshToken.revoked_at.is_(None),
                    RefreshToken.expires_at > datetime.now(UTC),
                )
                .first()
            )
            return live is None
        except Exception:
            _record_degradation("token_revocation", "deny")
            logger.critical(
                "Database revocation fallback failed for jti=%s...; denying (fail closed).",
                jti[:8],
            )
            return True

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
                # Null on a session established before v375 — "no cap recorded",
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
        # A legacy row (pre-v375) carries NULL here; passing it through means the
        # successor gets a freshly-stamped ceiling rather than the session being
        # refused, which is how NULL is grandfathered.
        new_token, new_token_record = self.create_refresh_token(
            db=db,
            user_id=user_id,
            user_uuid=user_uuid,
            role=role,
            user_agent=user_agent,
            ip_address=ip_address,
            absolute_expires_at=_as_utc(old_token_record.absolute_expires_at),
        )

        logger.info(
            f"Issued new refresh token for user {user_id} (new_jti={new_token_record.jti[:8]}...)"
        )

        # Revoke the old token AFTER new token is created (safe if this fails - user has new token)
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
        self.revoke_token(db, old_jti, old_expires)

        logger.info(f"Rotated out old refresh token for user {user_id} (old_jti={old_jti[:8]}...)")

        return new_token, new_token_record


# Module-level singleton
token_service = TokenService()
