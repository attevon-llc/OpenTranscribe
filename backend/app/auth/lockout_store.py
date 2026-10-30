"""Storage backend for account lockout records (NIST AC-7).

Owns the per-process choice between a shared Redis client and the thread-safe in-memory
fallback used while Redis is unreachable, the cached state that choice lives in, and the
rate-limited re-probe that moves a replica back onto Redis once it recovers. The lockout
policy itself (``auth/lockout.py``) and the admin read/reset surface
(``auth/lockout_admin.py``) both obtain their store through :func:`_get_store`.
"""

import logging
import threading
import time
from collections.abc import Callable

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_redis_client():
    """
    Get a Redis client connection for lockout storage.

    Returns:
        Optional[redis.Redis]: Redis client or None if unavailable.
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
        logger.warning("Redis package not available for lockout storage, using in-memory")
        return None
    except Exception as e:
        logger.warning(f"Redis connection failed for lockout storage: {e}")
        return None


class InMemoryLockoutStore:
    """
    Thread-safe in-memory storage for lockout records.

    Warning:
        This store does not persist across restarts and does not work
        in distributed deployments. Use Redis for production.
    """

    def __init__(self):
        self._data: dict[str, str] = {}  # key -> JSON string
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        """Get a value."""
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Set a value (ex parameter ignored for in-memory)."""
        with self._lock:
            self._data[key] = value

    def delete(self, key: str) -> int:
        """Delete a key, returning 1 if deleted, 0 if not found."""
        with self._lock:
            if key in self._data:
                del self._data[key]
                return 1
            return 0

    def keys(self, pattern: str) -> list[str]:
        """Get keys matching a pattern (simple prefix matching)."""
        prefix = pattern.rstrip("*")
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]


# Singleton stores
_redis_client = None
_in_memory_store: InMemoryLockoutStore | None = None
_store_initialized = False
_store_lock = threading.Lock()
_last_redis_probe: float = 0.0

#: How long to stay on the in-memory fallback before re-probing Redis.
REDIS_REPROBE_SECONDS = 30.0


def _get_store(*, clock: Callable[[], float] | None = None):
    """Get the storage backend (Redis, or the in-memory fallback while it is down).

    Re-probes Redis instead of latching (issue #284 A1.16). The fallback used to be
    permanent for the process lifetime: one transient Redis failure at first use and
    that replica counted failed logins **in its own memory forever**, even after Redis
    came back. Behind a load balancer that is an auth-throttling bypass — each replica
    tracks its own counter, so an attacker gets N x the allowed attempts, and lockouts
    stop being visible across replicas at all.

    Probing is rate-limited to one attempt per ``REDIS_REPROBE_SECONDS`` so a hard Redis
    outage doesn't add a connection attempt to every single login.

    Args:
        clock: Monotonic clock for the re-probe decision; ``time.monotonic`` when None.
    """
    global _redis_client, _in_memory_store, _store_initialized, _last_redis_probe

    with _store_lock:
        if not _store_initialized:
            _redis_client = _get_redis_client()
            if _redis_client is None:
                logger.warning(
                    "Using in-memory lockout storage. "
                    "Lockout state will not persist across restarts and will not work in distributed deployments."
                )
                _in_memory_store = InMemoryLockoutStore()
            _store_initialized = True
            _last_redis_probe = time.monotonic()
        elif _redis_client is None:
            # On the fallback — retry Redis, but not on every call.
            # `clock` is a per-call seam (issue #1160). Patching `time.monotonic` instead is
            # process-wide: any other thread calling in that window reads the fake clock
            # and spends the probe, which made the boundary test flaky.
            now = (clock or time.monotonic)()
            if now - _last_redis_probe >= REDIS_REPROBE_SECONDS:
                _last_redis_probe = now
                recovered = _get_redis_client()
                if recovered is not None:
                    logger.info("Redis recovered — resuming distributed lockout storage")
                    _redis_client = recovered

        # `is not None`, never truthiness (issue #810). A redis-py client is always truthy,
        # so this worked — but it made the fallback decision depend on a client's __bool__
        # rather than on whether one exists. Any client-like object defining __len__ (a test
        # stub recording calls, a future wrapper) would read as falsy while non-None, and
        # this would silently return the IN-MEMORY store while `_redis_client` was set:
        # per-replica lockout counting, i.e. N times the configured threshold before anyone
        # is locked out, with nothing raising.
        return _redis_client if _redis_client is not None else _in_memory_store


def _get_memory_fallback_store() -> InMemoryLockoutStore:
    """Get the in-memory store, creating it if Redis was healthy until now.

    ``_get_store()`` returns the *Redis* client whenever one exists, so it must never
    be used to supply the fallback: ``_check_and_record_attempt_memory`` reaches into
    ``store._lock`` / ``store._data``, which a ``redis.Redis`` does not have. Passing
    the Redis client there raised ``AttributeError`` out of ``check_and_record_attempt``
    and turned every login into an HTTP 500 for as long as Redis was unhealthy.
    """
    global _in_memory_store

    with _store_lock:
        if _in_memory_store is None:
            _in_memory_store = InMemoryLockoutStore()
        return _in_memory_store
