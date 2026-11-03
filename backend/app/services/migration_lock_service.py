"""
Redis-based migration lock service.

Pauses transcription tasks while any GPU migration is running so the GPU
is fully available for migration processing. Reference-counted — multiple
migration types can activate simultaneously, transcription resumes when
ALL migrations complete.

A 4-hour TTL acts as a safety net if the backend crashes mid-migration.
"""

import logging

import redis

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_LOCK_KEY = "embedding_migration:lock"
_LOCK_TTL_SECONDS = 4 * 60 * 60  # 4 hours


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
                self._client = get_redis()
                self._client.ping()
            except Exception as e:
                logger.error(f"Migration lock: Redis unavailable: {e}")
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

    def is_active(self) -> bool:
        """Check whether any migration is holding the lock."""
        client = self._redis
        if not client:
            return False
        val = client.get(_LOCK_KEY)
        return val is not None and int(val) > 0

    def refresh_ttl(self, ttl: int | None = None) -> bool:
        """Reset the lock TTL (call periodically during long migrations)."""
        client = self._redis
        if not client:
            return False
        return bool(client.expire(_LOCK_KEY, ttl or _LOCK_TTL_SECONDS))


# Singleton instance
migration_lock = MigrationLockService()
