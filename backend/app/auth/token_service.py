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

The refresh-token lifecycle (create, verify, session lifetime, rotation) is the
``RefreshTokenOps`` mixin in ``auth/refresh_tokens.py``; the session lifetime
configuration and concurrent-session ceiling live in ``auth/session_ceiling.py``.
"""

import hashlib
import logging
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from sqlalchemy.orm import Session

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.bulk_revocation import PendingBulkRevocation
from app.auth.degradation import record_degradation
from app.auth.refresh_tokens import RefreshTokenOps
from app.auth.revocation_error import RevocationError
from app.auth.revocation_fallback import is_revoked_from_database
from app.auth.session import InMemoryStore
from app.auth.session import get_redis_client
from app.core.auth_types import TOKEN_TYPE_ACCESS
from app.core.auth_types import TOKEN_TYPE_REFRESH
from app.core.config import settings
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


class TokenService(RefreshTokenOps):
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


# Module-level singleton
token_service = TokenService()
