"""
Redis-backed session management for OIDC state storage and session tracking.

Provides:
- OIDC state storage for PKCE code verifiers and OAuth state
- Session tracking with idle and absolute timeouts (NIST compliant)
- Distributed session management for clustered deployments
- Fallback to in-memory storage when Redis is unavailable

Security Considerations:
- Session IDs are cryptographically random (256 bits)
- OIDC states are single-use (deleted after retrieval)
- Idle timeout: 15 minutes (NIST SP 800-63B moderate assurance)
- Absolute timeout: 8 hours (force re-authentication)
"""

import builtins
import json
import logging
import secrets
import threading
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.config import settings

logger = logging.getLogger(__name__)


# Session prefixes for Redis keys
OIDC_STATE_PREFIX = "oidc:state:"
SESSION_PREFIX = "session:"
USER_SESSIONS_PREFIX = "user:sessions:"

# Session ID generation: 32 bytes = 256 bits of entropy
# This provides cryptographically secure session IDs that are resistant to
# brute-force attacks. OWASP recommends at least 128 bits of entropy for
# session identifiers; 256 bits provides additional security margin.
SESSION_ID_BYTES = 32

# Base64url character set for session ID validation (RFC 4648 Section 5)
BASE64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

# Expected session ID length: secrets.token_urlsafe(32) produces ~43 chars
# (32 bytes -> 43 base64url characters without padding)
SESSION_ID_EXPECTED_LENGTH = 43


def validate_session_id_format(session_id: str) -> bool:
    """
    Validate that a session ID has the expected format.

    Session IDs are generated using secrets.token_urlsafe(32) which produces
    a 43-character base64url-encoded string (32 bytes = 43 chars without padding).

    This validation prevents:
    - Injection attacks through malformed session IDs
    - Unnecessary storage lookups for obviously invalid IDs

    Args:
        session_id: Session ID to validate

    Returns:
        True if format is valid, False otherwise
    """
    if not session_id:
        return False

    # Check length (should be exactly 43 characters for 32 bytes of entropy)
    if len(session_id) != SESSION_ID_EXPECTED_LENGTH:
        logger.debug(
            f"Session ID format invalid: length {len(session_id)} "
            f"(expected {SESSION_ID_EXPECTED_LENGTH})"
        )
        return False

    # Check that all characters are valid base64url characters
    if not all(c in BASE64URL_CHARS for c in session_id):
        logger.debug("Session ID format invalid: contains non-base64url characters")
        return False

    return True


def get_redis_client():
    """
    Get a Redis client connection.

    Returns:
        Optional[redis.Redis]: Redis client or None if unavailable.

    Note:
        Returns None if Redis is unavailable, allowing fallback to in-memory storage.
        Logs a warning on connection failure.
    """
    try:
        import redis

        client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        # Test connection
        client.ping()
        return client
    except ImportError:
        logger.warning("Redis package not available, using in-memory storage")
        return None
    except Exception as e:
        logger.warning(f"Redis connection failed, using in-memory storage: {e}")
        return None


class InMemoryStore:
    """
    Thread-safe in-memory storage for fallback when Redis is unavailable.

    Warning:
        This store does not persist across restarts and does not work
        in distributed deployments. Use Redis for production.
    """

    def __init__(self):
        self._data: dict[str, tuple[str, float | None]] = {}  # key -> (value, expire_at)
        self._lock = threading.Lock()

    def set(self, key: str, value: str, ex: int | None = None) -> None:  # noqa: A003 - intentionally mirrors Redis API
        """Set a value with optional expiration in seconds."""
        with self._lock:
            expire_at = None
            if ex:
                expire_at = datetime.now(UTC).timestamp() + ex
            self._data[key] = (value, expire_at)

    def get(self, key: str) -> str | None:
        """Get a value, returning None if expired or not found."""
        with self._lock:
            if key not in self._data:
                return None
            value, expire_at = self._data[key]
            if expire_at and datetime.now(UTC).timestamp() > expire_at:
                del self._data[key]
                return None
            return value

    def delete(self, key: str) -> int:
        """Delete a key, returning 1 if deleted, 0 if not found."""
        with self._lock:
            if key in self._data:
                del self._data[key]
                return 1
            return 0

    def keys(self, pattern: str) -> list[str]:
        """Get keys matching a pattern (simple prefix matching)."""
        # Convert Redis pattern to simple prefix (only supports prefix*)
        prefix = pattern.rstrip("*")
        with self._lock:
            now = datetime.now(UTC).timestamp()
            return [
                k
                for k, (_, expire_at) in self._data.items()
                if k.startswith(prefix) and (not expire_at or now <= expire_at)
            ]

    def sadd(self, key: str, *values: str) -> int:
        """Add values to a set."""
        with self._lock:
            existing = self._data.get(key)
            current_set = set(json.loads(existing[0])) if existing else set()
            added = len(values) - len(current_set.intersection(values))
            current_set.update(values)
            self._data[key] = (json.dumps(list(current_set)), None)
            return added

    def srem(self, key: str, *values: str) -> int:
        """Remove values from a set."""
        with self._lock:
            existing = self._data.get(key)
            if not existing:
                return 0
            current_set = set(json.loads(existing[0]))
            removed = len(current_set.intersection(values))
            current_set.difference_update(values)
            if current_set:
                self._data[key] = (json.dumps(list(current_set)), None)
            else:
                del self._data[key]
            return removed

    def smembers(self, key: str) -> builtins.set[str]:  # noqa: A003 - set type shadowed by Redis API method
        """Get all members of a set."""
        with self._lock:
            existing = self._data.get(key)
            if not existing:
                return set()  # builtin set() call is fine
            return set(json.loads(existing[0]))  # type: ignore[arg-type]


# Singleton in-memory store for fallback
_in_memory_store: InMemoryStore | None = None


def _get_store():
    """Get the storage backend (Redis or in-memory fallback)."""
    global _in_memory_store

    redis_client = get_redis_client()
    if redis_client:
        return redis_client

    # Fallback to in-memory
    if _in_memory_store is None:
        logger.warning(
            "Using in-memory session storage. "
            "Sessions will not persist across restarts and will not work in distributed deployments."
        )
        _in_memory_store = InMemoryStore()
    return _in_memory_store


class OIDCStateStore:
    """
    Storage for OIDC state parameters during OAuth authorization flow.

    Stores state values and associated data (like PKCE code verifiers) during
    the authorization flow. States are single-use and are deleted after retrieval.

    Thread-safe for concurrent requests.

    Security Features:
    - Maximum state count limit to prevent state exhaustion attacks
    - Automatic cleanup of expired states
    - Single-use states (deleted after retrieval)
    """

    # Maximum number of active OIDC states allowed
    # This prevents state exhaustion attacks where an attacker creates many states
    # to exhaust server memory. 10000 allows for high traffic while preventing abuse.
    MAX_STATES = 10000

    def __init__(self, max_states: int | None = None):
        """Initialize the OIDC state store.

        Args:
            max_states: Maximum number of active states allowed (default: 10000)
        """
        self._store = None
        self._max_states = max_states if max_states is not None else self.MAX_STATES

    @property
    def store(self):
        """Lazy-load storage backend."""
        if self._store is None:
            self._store = _get_store()
        return self._store

    def _count_states(self) -> int:
        """Count the number of active OIDC states, without scanning the keyspace.

        This ran ``KEYS oidc:state:*`` on **every** login attempt at an
        unauthenticated endpoint. ``KEYS`` is O(total keyspace) and blocks the
        Redis event loop for the whole scan, so on a busy instance — Redis is
        also the Celery broker and the cache here — an anonymous caller could
        stall every other client just by hitting the login route repeatedly.

        ``SCAN`` with a bound is used instead: this is a guard against state
        exhaustion, so all it needs to answer is "are we at the limit?", not the
        exact population. Counting stops one past the ceiling.

        Returns:
            Number of active states, capped at the configured limit plus one.
        """
        pattern = f"{OIDC_STATE_PREFIX}*"
        ceiling = self._max_states + 1

        scan_iter = getattr(self.store, "scan_iter", None)
        if scan_iter is None:
            # In-memory fallback store has no SCAN; its keyspace is this process's
            # own and small by construction.
            return len(self.store.keys(pattern))

        count = 0
        for _ in scan_iter(match=pattern, count=500):
            count += 1
            if count >= ceiling:
                break
        return count

    def _cleanup_oldest_states(self, count: int = 100) -> int:
        """Remove oldest states when limit is exceeded.

        For Redis, states have TTL so this is less critical.
        For in-memory store, removes oldest entries.

        Args:
            count: Number of states to remove

        Returns:
            Number of states actually removed
        """
        keys = self.store.keys(f"{OIDC_STATE_PREFIX}*")
        removed = 0
        # Remove oldest keys (first in list, assuming insertion order for in-memory)
        for key in keys[:count]:
            if self.store.delete(key):
                removed += 1
        if removed > 0:
            logger.warning(f"Cleaned up {removed} OIDC states due to limit exceeded")
        return removed

    def store_state(self, state: str, data: dict, expires_seconds: int = 600) -> bool:
        """
        Store OIDC state with associated data.

        Args:
            state: Random state parameter for CSRF protection
            data: Associated data (e.g., code_verifier for PKCE, redirect URL)
            expires_seconds: Time-to-live in seconds (default: 10 minutes)

        Returns:
            True if state was stored, False if rejected due to limit

        Raises:
            None - returns False if state limit exceeded

        Example:
            store.store_state(
                state="abc123",
                data={"code_verifier": "xyz...", "redirect_url": "/dashboard"},
                expires_seconds=600
            )

        Security:
            - Enforces maximum state count to prevent state exhaustion attacks
            - If limit is exceeded, oldest states are cleaned up before adding new one
        """
        # Check state count limit to prevent exhaustion attacks
        current_count = self._count_states()
        if current_count >= self._max_states:
            # Try to clean up expired/oldest states
            self._cleanup_oldest_states(100)
            # Re-check after cleanup
            current_count = self._count_states()
            if current_count >= self._max_states:
                logger.error(
                    f"OIDC state limit exceeded ({current_count} >= {self._max_states}). "
                    "Possible state exhaustion attack."
                )
                return False

        key = f"{OIDC_STATE_PREFIX}{state}"
        value = json.dumps(data)
        self.store.set(key, value, ex=expires_seconds)
        logger.debug(f"Stored OIDC state: {state[:8]}... (expires in {expires_seconds}s)")
        return True

    def get_state(self, state: str) -> dict | None:
        """
        Retrieve and delete OIDC state data (single-use).

        Args:
            state: State parameter to look up

        Returns:
            Associated data dict or None if not found/expired

        Note:
            State is deleted after retrieval to prevent replay attacks.
        """
        key = f"{OIDC_STATE_PREFIX}{state}"
        value = self.store.get(key)
        if value:
            # Delete after retrieval (single-use)
            self.store.delete(key)
            logger.debug(f"Retrieved and deleted OIDC state: {state[:8]}...")
            return dict(json.loads(value))  # type: ignore[arg-type]
        logger.warning(f"OIDC state not found or expired: {state[:8]}...")
        return None

    def delete_state(self, state: str) -> bool:
        """
        Explicitly delete an OIDC state.

        Args:
            state: State parameter to delete

        Returns:
            True if deleted, False if not found
        """
        key = f"{OIDC_STATE_PREFIX}{state}"
        deleted = self.store.delete(key)
        if deleted:
            logger.debug(f"Deleted OIDC state: {state[:8]}...")
        return bool(deleted)


class SessionManager:
    """
    Session management with idle and absolute timeouts.

    Tracks user sessions across requests, enforcing:
    - Idle timeout: Session expires after inactivity (default: 15 minutes)
    - Absolute timeout: Session expires regardless of activity (default: 8 hours)

    Session data structure:
        {
            "session_id": str,
            "user_id": str,
            "created_at": ISO datetime string,
            "last_activity": ISO datetime string,
            "metadata": dict  # IP, user-agent, etc.
        }

    Thread-safe for concurrent requests.
    """

    def __init__(
        self,
        idle_timeout_minutes: int | None = None,
        absolute_timeout_minutes: int | None = None,
    ):
        """
        Initialize the session manager.

        Args:
            idle_timeout_minutes: Override idle timeout (default from settings)
            absolute_timeout_minutes: Override absolute timeout (default from settings)
        """
        self._store = None
        self.idle_timeout = timedelta(
            minutes=idle_timeout_minutes or settings.SESSION_IDLE_TIMEOUT_MINUTES
        )
        self.absolute_timeout = timedelta(
            minutes=absolute_timeout_minutes or settings.SESSION_ABSOLUTE_TIMEOUT_MINUTES
        )
        logger.debug(
            f"SessionManager initialized: idle={self.idle_timeout}, "
            f"absolute={self.absolute_timeout}"
        )

    @property
    def store(self):
        """Lazy-load storage backend."""
        if self._store is None:
            self._store = _get_store()
        return self._store

    def _generate_session_id(self) -> str:
        """Generate a cryptographically secure session ID (256 bits)."""
        return secrets.token_urlsafe(SESSION_ID_BYTES)

    def _session_key(self, session_id: str) -> str:
        """Get the Redis key for a session."""
        return f"{SESSION_PREFIX}{session_id}"

    def _user_sessions_key(self, user_id: str) -> str:
        """Get the Redis key for a user's session set."""
        return f"{USER_SESSIONS_PREFIX}{user_id}"

    def create_session(self, user_id: str, token: str, metadata: dict | None = None) -> str:
        """
        Create a new session for a user.

        Args:
            user_id: User identifier
            token: JWT or session token associated with this session
            metadata: Optional session metadata (IP, user-agent, etc.)

        Returns:
            session_id: Unique session identifier
        """
        session_id = self._generate_session_id()
        now = datetime.now(UTC)

        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "token": token,
            "created_at": now.isoformat(),
            "last_activity": now.isoformat(),
            "metadata": metadata or {},
        }

        # Store session with absolute timeout as TTL
        key = self._session_key(session_id)
        ttl_seconds = int(self.absolute_timeout.total_seconds())
        self.store.set(key, json.dumps(session_data), ex=ttl_seconds)

        # Track session in user's session set
        user_key = self._user_sessions_key(user_id)
        self.store.sadd(user_key, session_id)

        logger.info(f"Created session {session_id[:8]}... for user {user_id}")
        return session_id

    def validate_session(self, session_id: str) -> dict | None:
        """
        Validate a session and update last activity time.

        Args:
            session_id: Session identifier to validate

        Returns:
            Session data dict if valid, None if invalid/expired

        Checks:
            1. Session ID format is valid (base64url, expected length)
            2. Session exists in storage
            3. Idle timeout not exceeded
            4. Absolute timeout not exceeded (implicit via Redis TTL)
        """
        # Validate session ID format before querying storage
        # This prevents injection attacks and unnecessary storage lookups
        if not validate_session_id_format(session_id):
            logger.warning(
                f"Session validation rejected: malformed session ID "
                f"(length={len(session_id) if session_id else 0})"
            )
            return None

        key = self._session_key(session_id)
        data = self.store.get(key)

        if not data:
            logger.debug(f"Session not found: {session_id[:8]}...")
            return None

        session_data = json.loads(data)
        now = datetime.now(UTC)

        # Check idle timeout
        last_activity = datetime.fromisoformat(session_data["last_activity"])
        if now - last_activity > self.idle_timeout:
            logger.info(
                f"Session {session_id[:8]}... expired due to idle timeout "
                f"(last activity: {last_activity.isoformat()})"
            )
            self.invalidate_session(session_id)
            return None

        # Update last activity
        session_data["last_activity"] = now.isoformat()

        # Calculate remaining TTL based on absolute timeout
        created_at = datetime.fromisoformat(session_data["created_at"])
        remaining = self.absolute_timeout - (now - created_at)
        remaining_seconds = max(1, int(remaining.total_seconds()))

        self.store.set(key, json.dumps(session_data), ex=remaining_seconds)

        logger.debug(f"Session {session_id[:8]}... validated, remaining TTL: {remaining_seconds}s")
        return dict(session_data)  # type: ignore[arg-type]

    def invalidate_session(self, session_id: str) -> bool:
        """
        Invalidate (logout) a single session.

        Args:
            session_id: Session identifier to invalidate

        Returns:
            True if session was invalidated, False if not found
        """
        key = self._session_key(session_id)
        data = self.store.get(key)

        if data:
            session_data = json.loads(data)
            user_id = session_data.get("user_id")

            # Remove from user's session set
            if user_id:
                user_key = self._user_sessions_key(user_id)
                self.store.srem(user_key, session_id)

        deleted = self.store.delete(key)
        if deleted:
            logger.info(f"Invalidated session: {session_id[:8]}...")
        return bool(deleted)

    def get_user_sessions(self, user_id: str) -> list[dict]:
        """
        Get all active sessions for a user.

        Args:
            user_id: User identifier

        Returns:
            List of session data dicts for the user

        Note:
            Cleans up expired sessions from the user's session set.
        """
        user_key = self._user_sessions_key(user_id)
        session_ids = self.store.smembers(user_key)

        sessions = []
        expired_ids = []

        for session_id in session_ids:
            key = self._session_key(session_id)
            data = self.store.get(key)
            if data:
                session_data = json.loads(data)
                # Check idle timeout
                last_activity = datetime.fromisoformat(session_data["last_activity"])
                now = datetime.now(UTC)
                if now - last_activity <= self.idle_timeout:
                    # Don't include token in listing for security
                    safe_data = {k: v for k, v in session_data.items() if k != "token"}
                    sessions.append(safe_data)
                else:
                    expired_ids.append(session_id)
            else:
                expired_ids.append(session_id)

        # Clean up expired session references
        for session_id in expired_ids:
            self.store.srem(user_key, session_id)

        logger.debug(f"Found {len(sessions)} active sessions for user {user_id}")
        return sessions

    def invalidate_all_user_sessions(self, user_id: str) -> int:
        """
        Invalidate all sessions for a user (logout all devices).

        Args:
            user_id: User identifier

        Returns:
            Number of sessions invalidated
        """
        user_key = self._user_sessions_key(user_id)
        session_ids = self.store.smembers(user_key)

        count = 0
        for session_id in session_ids:
            key = self._session_key(session_id)
            if self.store.delete(key):
                count += 1

        # Clear the user's session set
        self.store.delete(user_key)

        logger.info(f"Invalidated {count} sessions for user {user_id}")
        return count


# Module-level singletons for convenience
oidc_state_store = OIDCStateStore()
session_manager = SessionManager()
