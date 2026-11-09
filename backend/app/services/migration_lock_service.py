"""
Redis-based migration lock service.

Pauses transcription tasks while any GPU migration is running so the GPU
is fully available for migration processing. Reference-counted — multiple
migration types can activate simultaneously, transcription resumes when
ALL migrations complete.

A 4-hour TTL acts as a safety net if the backend crashes mid-migration.
"""

import enum
import logging

import redis
from redis.exceptions import RedisError

from app.core.redis import get_request_redis

logger = logging.getLogger(__name__)

_LOCK_KEY = "embedding_migration:lock"
_LOCK_TTL_SECONDS = 4 * 60 * 60  # 4 hours


class LockState(enum.Enum):
    """What an observer can know about the lock.

    ``UNKNOWN`` is its own state because "Redis could not be read" is not evidence that no
    migration is running: reading it as released would let a second migration start.
    """

    RELEASED = "released"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class MigrationLockService:
    """Redis-based lock that pauses transcription during GPU migrations.

    Reference-counted: multiple migrations can call activate() and
    transcription stays paused until the last one calls deactivate().
    """

    def __init__(self) -> None:
        self._client: redis.Redis | None = None

    @property
    def _redis(self) -> redis.Redis | None:
        if self._client is None:
            try:
                self._client = get_request_redis()
                self._client.ping()
            except RedisError as e:
                logger.error(f"Migration lock: Redis unavailable: {e}", exc_info=True)
                self._client = None
        return self._client

    def activate(self) -> bool:
        """Increment the lock reference count. Always succeeds.

        Returns:
            True if activated, False only if Redis unavailable.
        """
        client = self._redis
        if not client:
            return False

        count = client.incr(_LOCK_KEY)
        client.expire(_LOCK_KEY, _LOCK_TTL_SECONDS)
        logger.info("Migration lock activated (ref_count=%d)", count)
        return True

    def deactivate(self) -> bool:
        """Decrement the lock reference count. Removes key when zero.

        Returns:
            True if lock was fully released (ref_count reached 0).
        """
        client = self._redis
        if not client:
            return False

        count = client.decr(_LOCK_KEY)
        if count <= 0:
            client.delete(_LOCK_KEY)
            logger.info("Migration lock released (all migrations complete)")
            return True

        logger.info("Migration lock decremented (ref_count=%d)", count)
        return False

    def state(self) -> LockState:
        """Read the lock: held, released, or unobservable (Redis down)."""
        client = self._redis
        if not client:
            return LockState.UNKNOWN
        try:
            val = client.get(_LOCK_KEY)
        except RedisError:
            logger.error("Migration lock: could not read the lock state", exc_info=True)
            self._client = None
            return LockState.UNKNOWN
        return LockState.ACTIVE if val is not None and int(val) > 0 else LockState.RELEASED

    def is_active(self) -> bool:
        """Whether to behave as if a migration holds the lock.

        Fails closed: an unobservable lock (``LockState.UNKNOWN``) counts as active.
        """
        return self.state() is not LockState.RELEASED


# Singleton instance
migration_lock = MigrationLockService()
