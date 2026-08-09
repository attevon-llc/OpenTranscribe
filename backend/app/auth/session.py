"""Redis-backed stores for the OIDC login flow, with an in-memory fallback.

Provides:
- OIDC state storage for PKCE code verifiers and OAuth state
- Fallback to in-memory storage when Redis is unavailable

Security Considerations:
- OIDC states are single-use (deleted after retrieval)
- The state store is capped, so an unauthenticated caller cannot exhaust it

**This module does not own sessions.** It used to also carry a ``SessionManager``
implementing Redis-backed idle/absolute timeouts, with zero call sites — so the
two settings it read changed nothing. Sessions are owned by the ``refresh_token``
table (``app/models/refresh_token.py``), which already carried concurrent-session
limits, rotation and revocation; the timeouts were moved there rather than
duplicated here. Two owners would enforce against different session sets the
moment Redis and Postgres diverged, and issue #324 already established that Redis
is a cache here, not the system of record. See
``plans/session-ownership-decision.md``.
"""

import builtins
import json
import logging
import threading
import time
from datetime import UTC
from datetime import datetime
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


# Redis key prefix for OIDC login states
OIDC_STATE_PREFIX = "oidc:state:"


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


# Singleton stores. Mirrors app/auth/lockout.py: the Redis client is cached rather
# than rebuilt per call, and while it is down the fallback is re-probed on a timer
# instead of latching for the process lifetime.
_redis_client = None
_in_memory_store: InMemoryStore | None = None
_store_initialized = False
_store_lock = threading.Lock()
_last_redis_probe: float = 0.0


def _record_degradation(control: str, fallback: str) -> None:
    """Count a security control running without its shared state store.

    Imported lazily and never allowed to raise: a broken metrics backend must not be
    able to break the login flow. Same contract as
    ``lockout._record_degradation`` / ``token_service._record_degradation``.

    Args:
        control: The security control that degraded.
        fallback: What it used instead (``local`` = per-process approximation).
    """
    try:
        from app.core.metrics import security_state_degraded_total

        security_state_degraded_total.labels(control=control, fallback=fallback).inc()
    except Exception:  # pragma: no cover - metrics must never break auth
        logger.debug("Could not record security degradation metric", exc_info=True)


def _get_store():
    """Get the storage backend (Redis, or the in-memory fallback while it is down).

    Two problems this replaces, both on an **unauthenticated** endpoint:

    * It called ``get_redis_client()`` on **every** access, and that function builds a
      new client and issues a ``PING`` — a connection setup and a round trip per OIDC
      login step, with the resulting pool immediately discarded.
    * There was no memory of the outcome and no signal when it failed: the fallback
      was chosen silently at ``warning`` level with no metric, so a deployment running
      OIDC login state per-replica (states stored on one replica, redeemed on another —
      i.e. logins that fail at random behind a load balancer) looked healthy.

    The re-probe policy is ``lockout.REDIS_REPROBE_SECONDS``, imported rather than
    re-declared so the two controls cannot drift apart.

    Returns:
        The Redis client when reachable, otherwise the shared ``InMemoryStore``.
    """
    global _redis_client, _in_memory_store, _store_initialized, _last_redis_probe

    from app.auth.lockout import REDIS_REPROBE_SECONDS

    with _store_lock:
        if not _store_initialized:
            _redis_client = get_redis_client()
            if _redis_client is None:
                logger.warning(
                    "Using in-memory OIDC state storage. "
                    "Login states will not be shared across replicas or survive a restart."
                )
                _in_memory_store = InMemoryStore()
            _store_initialized = True
            _last_redis_probe = time.monotonic()
        elif _redis_client is None:
            # On the fallback — retry Redis, but not on every call.
            now = time.monotonic()
            if now - _last_redis_probe >= REDIS_REPROBE_SECONDS:
                _last_redis_probe = now
                recovered = get_redis_client()
                if recovered is not None:
                    logger.info("Redis recovered — resuming shared OIDC state storage")
                    _redis_client = recovered

        if _redis_client is not None:
            return _redis_client

        if _in_memory_store is None:
            _in_memory_store = InMemoryStore()

    _record_degradation("oidc_state", "local")
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
        # Either a redis.Redis or an InMemoryStore — deliberately structural, since
        # the fallback only implements the subset of the Redis API used here.
        self._store: Any = None
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

    def _scan_keys(self, pattern: str, limit: int) -> list[str]:
        """Collect at most ``limit`` keys matching ``pattern`` without ``KEYS``.

        ``KEYS`` is O(total keyspace) and blocks the Redis event loop for the whole
        scan. Redis here is also the Celery broker and the cache, so a blocking scan
        driven from an unauthenticated endpoint stalls transcription dispatch and every
        other client — a denial-of-service primitive, not just a slow query.

        Args:
            pattern: Redis glob pattern.
            limit: Stop after this many keys.

        Returns:
            Up to ``limit`` matching keys.
        """
        scan_iter = getattr(self.store, "scan_iter", None)
        if scan_iter is None:
            # In-memory fallback: no SCAN, but the keyspace is this process's own.
            return list(self.store.keys(pattern))[:limit]

        collected: list[str] = []
        for key in scan_iter(match=pattern, count=500):
            collected.append(key if isinstance(key, str) else key.decode())
            if len(collected) >= limit:
                break
        return collected

    def _cleanup_oldest_states(self, count: int = 100) -> int:
        """Remove some states when the limit is exceeded.

        For Redis, states have TTL so this is less critical.
        For in-memory store, removes oldest entries.

        Args:
            count: Number of states to remove

        Returns:
            Number of states actually removed
        """
        keys = self._scan_keys(f"{OIDC_STATE_PREFIX}*", count)
        removed = 0
        for key in keys:
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


# Module-level singleton for convenience
oidc_state_store = OIDCStateStore()
