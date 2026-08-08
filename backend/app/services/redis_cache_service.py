"""
Redis cache service for API response caching with push-based invalidation.

Provides a cache-aside pattern where backend endpoints check Redis before
querying PostgreSQL. Cache invalidation is triggered on writes and pushed
to the frontend via the existing WebSocket pub/sub channel so clients
always see fresh data.

Cache key conventions:
    cache:tags:{user_id}            - Tag list for a user
    cache:speakers:{user_id}        - Speaker list for a user
    cache:metadata:{user_id}        - Metadata filter ranges for a user
    cache:files:{user_id}:{hash}    - Paginated file listings
    cache:status:{user_id}          - User file status summary
    cache:collections:{user_id}     - Collection list for a user
"""

import json
import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


def _record(result: str) -> None:
    """Record a Redis cache hit/miss on the Prometheus counter (best-effort)."""
    try:
        from app.core.metrics import cache_operations_total

        cache_operations_total.labels(cache="redis", result=result).inc()
    except Exception:  # noqa: S110  # nosec B110 - metrics must never break caching
        pass  # pragma: no cover


# Default TTLs in seconds
TTL_TAGS = 300  # 5 minutes
TTL_SPEAKERS = 300
TTL_METADATA = 300
TTL_FILES = 120  # 2 minutes
TTL_STATUS = 60  # 1 minute
TTL_COLLECTIONS = 300


class RedisCacheService:
    """Thin wrapper around Redis for API response caching.

    Lazily connects on first use. Degrades gracefully if Redis is
    unavailable — callers always fall through to the database.
    """

    def __init__(self) -> None:
        self._redis: Any = None

    @property
    def redis(self) -> Any:
        """Lazy Redis connection (sync client)."""
        if self._redis is None:
            try:
                import redis as sync_redis

                self._redis = sync_redis.Redis(
                    host=settings.REDIS_HOST,
                    port=int(settings.REDIS_PORT),
                    password=settings.REDIS_PASSWORD or None,
                    db=1,  # Separate DB from Celery broker (db 0)
                    decode_responses=True,
                    socket_timeout=2,
                    socket_connect_timeout=2,
                )
                self._redis.ping()
                logger.info("Redis cache service connected (db=1)")
            except Exception as e:
                logger.warning(f"Redis cache unavailable, caching disabled: {e}")
                self._redis = None
        return self._redis

    # ------------------------------------------------------------------
    # Core cache operations
    # ------------------------------------------------------------------

    def get(self, key: str) -> Any | None:
        """Retrieve a cached value. Returns None on miss or error."""
        client = self.redis
        if client is None:
            return None
        try:
            raw = client.get(key)
            if raw is not None:
                _record("hit")
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"Cache GET error for {key}: {e}")
        _record("miss")
        return None

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        """Store a value with a TTL (seconds)."""
        client = self.redis
        if client is None:
            return
        try:
            client.setex(key, ttl, json.dumps(value, default=str))
        except Exception as e:
            logger.debug(f"Cache SET error for {key}: {e}")

    def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching a glob pattern. Returns count deleted."""
        client = self.redis
        if client is None:
            return 0
        try:
            keys = client.keys(pattern)
            if keys:
                return int(client.delete(*keys))
        except Exception as e:
            logger.debug(f"Cache DELETE error for {pattern}: {e}")
        return 0

    # ------------------------------------------------------------------
    # Domain-specific invalidation helpers
    # ------------------------------------------------------------------

    def invalidate_user_files(self, user_id: int) -> None:
        """Invalidate all file listing caches for a user."""
        self.delete_pattern(f"cache:files:{user_id}:*")
        self.delete_pattern(f"cache:status:{user_id}")
        self._push_invalidation(user_id, "files")

    def invalidate_tags(self, user_id: int) -> None:
        """Invalidate tag caches for a user."""
        self.delete_pattern(f"cache:tags:{user_id}")
        self._push_invalidation(user_id, "tags")

    def invalidate_tags_global(self) -> int:
        """Invalidate **every** user's cached tag list.

        A ``tag`` row is global — one row is shared by all users, and
        ``cache:tags:{user_id}`` only records how that user's files use it. So
        renaming or merging a tag changes what every *other* user's cached list
        should say, and busting only the actor's key leaves everyone else
        reading the old name until ``TTL_TAGS`` expires.

        No WebSocket push accompanies this: ``_push_invalidation`` addresses one
        user and there is no broadcast channel. Other sessions see the change on
        their next read, which is now guaranteed to be a miss.

        Returns:
            Number of cache keys deleted.
        """
        return self.delete_pattern("cache:tags:*")

    def invalidate_tags_for_file(self, db: Any, file_id: int) -> None:
        """Invalidate the owning user's tag + file caches for a file.

        Used by back-door tag-mutation paths (upload helpers, auto-labeling)
        that operate on ``file_id`` and don't carry ``current_user`` — resolves
        the file owner so the read-through tag cache never goes stale.
        Best-effort: a lookup failure must not break the mutation.
        """
        try:
            from app.models.media import MediaFile

            owner_id = db.query(MediaFile.user_id).filter(MediaFile.id == file_id).scalar()
            if owner_id is not None:
                self.invalidate_tags(int(owner_id))
                self.invalidate_user_files(int(owner_id))
        except Exception as e:
            logger.debug(f"invalidate_tags_for_file failed (non-critical): {e}")

    def invalidate_speakers(self, user_id: int) -> None:
        """Invalidate speaker caches for a user."""
        self.delete_pattern(f"cache:speakers:{user_id}")
        self._push_invalidation(user_id, "speakers")

    def invalidate_metadata(self, user_id: int) -> None:
        """Invalidate metadata filter caches for a user."""
        self.delete_pattern(f"cache:metadata:{user_id}")
        self._push_invalidation(user_id, "metadata")

    def invalidate_collections(self, user_id: int) -> None:
        """Invalidate collection caches for a user."""
        self.delete_pattern(f"cache:collections:{user_id}")
        self._push_invalidation(user_id, "collections")

    def invalidate_all_for_user(self, user_id: int) -> None:
        """Nuclear option — clear every cache entry for a user."""
        self.delete_pattern(f"cache:*:{user_id}*")
        self._push_invalidation(user_id, "all")

    # ------------------------------------------------------------------
    # Push invalidation to frontend via WebSocket
    # ------------------------------------------------------------------

    def _push_invalidation(self, user_id: int, scope: str) -> None:
        """Push a cache invalidation notification through the existing
        Redis pub/sub channel so the frontend can refresh stale data.

        Uses the same ``websocket_notifications`` channel that the
        WebSocket subscriber in ``app.api.websockets`` listens on.
        """
        client = self.redis
        if client is None:
            return
        try:
            import redis as sync_redis

            # Use db=0 (the pub/sub channel) for notifications
            notify_client = sync_redis.Redis(
                host=settings.REDIS_HOST,
                port=int(settings.REDIS_PORT),
                password=settings.REDIS_PASSWORD or None,
                db=0,
                decode_responses=True,
                socket_timeout=2,
            )
            notification = json.dumps(
                {
                    "user_id": user_id,
                    "type": "cache_invalidate",
                    "data": {"scope": scope},
                }
            )
            notify_client.publish("websocket_notifications", notification)
            notify_client.close()
        except Exception as e:
            logger.debug(f"Cache invalidation push error: {e}")


# Module-level singleton
redis_cache = RedisCacheService()
