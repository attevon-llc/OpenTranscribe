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

from jose import JWTError
from jose import jwt
from sqlalchemy.orm import Session

from app.auth.session import InMemoryStore
from app.auth.session import get_redis_client
from app.core.config import settings
from app.models.refresh_token import RefreshToken

logger = logging.getLogger(__name__)

# Redis key prefix for revoked tokens (not a password)
REVOKED_TOKEN_PREFIX = "revoked:jti:"  # noqa: S105 # nosec B105


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

    @property
    def store(self):
        """Lazy-load storage backend (Redis or in-memory fallback)."""
        if self._store is None:
            redis_client = get_redis_client()
            if redis_client:
                self._store = redis_client
            else:
                logger.warning(
                    "Redis unavailable for token revocation. "
                    "Using in-memory store (not recommended for production)."
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
            JWTError: If token verification fails with all algorithms
        """
        algorithms_to_try = []

        # FIPS 140-3 uses HS512
        if settings.FIPS_VERSION == "140-3":
            algorithms_to_try = ["HS512", "HS256"]
        else:
            algorithms_to_try = ["HS256", "HS512"]

        last_error = None
        for algorithm in algorithms_to_try:
            try:
                payload = jwt.decode(
                    token,
                    settings.JWT_SECRET_KEY,
                    algorithms=[algorithm],
                )
                return dict(payload)
            except JWTError as e:
                last_error = e
                continue

        raise last_error or JWTError("Token verification failed")

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

        return str(
            jwt.encode(
                to_encode,
                settings.JWT_SECRET_KEY,
                algorithm=algorithm,
            )
        )

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
            jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=["HS256"])
            # If we're in FIPS 140-3 mode, this token needs upgrade
            return settings.FIPS_VERSION == "140-3"
        except JWTError:
            return False

    def create_refresh_token(
        self,
        db: Session,
        user_id: int,
        user_uuid: str,
        role: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
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

        Returns:
            Tuple of (token_string, RefreshToken model instance)
        """
        # Generate unique JTI
        jti = str(uuid.uuid4())

        # Calculate expiration
        now = datetime.now(UTC)
        expires_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
        expires_at = now + expires_delta

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
        token = jwt.encode(
            token_data,
            settings.JWT_SECRET_KEY,
            algorithm=algorithm,
        )

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
        )

        db.add(refresh_token)
        db.commit()
        db.refresh(refresh_token)

        logger.info(
            f"Created refresh token for user {user_id} "
            f"(jti={jti[:8]}..., expires={expires_at.isoformat()})"
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
            if self.is_token_revoked(jti):
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
                return None, None

            logger.debug(f"Refresh token verified successfully (jti={jti[:8]}...)")
            return payload, refresh_token

        except jwt.ExpiredSignatureError:
            logger.warning("Token verification failed: token expired")
            return None, None
        except jwt.JWTError as e:
            logger.warning(f"Token verification failed: JWT error - {e}")
            return None, None
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            return None, None

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
            return True

        except Exception as e:
            logger.error(f"Error revoking token: {e}")
            return False

    def revoke_all_user_tokens(self, db: Session, user_id: int) -> int:
        """
        Revoke all refresh tokens for a user.

        Used for:
        - Logout from all devices
        - Password change
        - Account security concerns

        Args:
            db: Database session
            user_id: User ID whose tokens should be revoked

        Returns:
            Number of tokens revoked
        """
        try:
            # Get all active refresh tokens for user
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
                # Add to Redis blacklist
                ttl_seconds = max(1, int((token.expires_at - now).total_seconds()))
                key = f"{REVOKED_TOKEN_PREFIX}{token.jti}"
                self.store.set(key, "revoked", ex=ttl_seconds)

                # Update database record
                token.revoked_at = now  # type: ignore[assignment]
                count += 1

            db.commit()
            logger.info(f"Revoked {count} tokens for user {user_id}")
            return count

        except Exception as e:
            logger.error(f"Error revoking user tokens: {e}")
            db.rollback()
            return 0

    def is_token_revoked(self, jti: str) -> bool:
        """
        Check if a token JTI is on the revocation blacklist.

        Args:
            jti: The JWT ID to check

        Returns:
            True if revoked, False if valid
        """
        if not settings.TOKEN_REVOCATION_ENABLED:
            return False

        key = f"{REVOKED_TOKEN_PREFIX}{jti}"
        result = self.store.get(key)
        return result is not None

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
        new_token, new_token_record = self.create_refresh_token(
            db=db,
            user_id=user_id,
            user_uuid=user_uuid,
            role=role,
            user_agent=user_agent,
            ip_address=ip_address,
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
