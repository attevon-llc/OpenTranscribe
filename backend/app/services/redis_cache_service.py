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
import time
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

#: Keys per ``SCAN`` round trip, and the largest ``DELETE`` argument batch.
_SCAN_BATCH = 500

#: How long to stop dialling Redis after a failed connection attempt.
#:
#: Without this the "unavailable" verdict was never remembered: ``redis`` below
#: re-entered its ``try`` on EVERY cache call, and redis-py's default retry policy
#: sleeps with exponential backoff on each attempt. One tag merge (3 creates + a
#: merge, each busting several keys) spent **71 of its 87 seconds in
#: ``time.sleep`` inside ``redis/retry.py``** — 200 sleeps — which is how a
#: 3-tag test came to take 75 s (issue #431). In production the same shape means a
#: Redis outage adds several backoff sleeps to every request that touches the
#: cache, turning a degraded-cache incident into an apparent total outage.
#:
#: 30 s is short enough that a recovered Redis is picked up promptly and long
#: enough that a sustained outage costs one attempt per 30 s per process rather
#: than one per call.
_UNAVAILABLE_COOLDOWN_SECONDS = 30.0


class RedisCacheService:
    """Thin wrapper around Redis for API response caching.

    Lazily connects on first use. Degrades gracefully if Redis is
    unavailable — callers always fall through to the database.
    """

    def __init__(self) -> None:
        self._redis: Any = None
        #: Monotonic deadline before which no further connection is attempted.
        self._unavailable_until: float | None = None

    @property
    def redis(self) -> Any:
        """Lazy Redis connection (sync client), or ``None`` while unavailable.

        A failed attempt opens a ``_UNAVAILABLE_COOLDOWN_SECONDS`` circuit rather
        than being retried on the next call — see that constant for why.
        """
        if self._redis is not None:
            return self._redis

        if self._unavailable_until is not None and time.monotonic() < self._unavailable_until:
            return None

        try:
            import redis as sync_redis
            from redis.backoff import NoBackoff
            from redis.retry import Retry

            self._redis = sync_redis.Redis(
                host=settings.REDIS_HOST,
                port=int(settings.REDIS_PORT),
                password=settings.REDIS_PASSWORD or None,
                db=1,  # Separate DB from Celery broker (db 0)
                decode_responses=True,
                socket_timeout=2,
                socket_connect_timeout=2,
                # One attempt, no backoff. The cooldown above is this service's
                # retry policy; redis-py's default adds sleeps on top of it.
                retry=Retry(NoBackoff(), 0),
            )
            self._redis.ping()
            self._unavailable_until = None
            logger.info("Redis cache service connected (db=1)")
        except Exception as e:
            self._redis = None
            self._unavailable_until = time.monotonic() + _UNAVAILABLE_COOLDOWN_SECONDS
            logger.warning(
                "Redis cache unavailable, caching disabled for %.0fs: %s",
                _UNAVAILABLE_COOLDOWN_SECONDS,
                e,
            )
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
        """Delete every key matching a glob pattern. Returns count deleted.

        Two things this deliberately avoids:

        * **``KEYS`` on a fully-specified key.** Most callers here pass no
          wildcard at all (``cache:tags:{user_id}``, ``cache:status:{user_id}``)
          — those go straight to ``DELETE``.
        * **``KEYS`` at all.** It is O(keyspace) *and* blocks the whole
          instance, which on this deployment also carries the Celery broker, so
          a tag merge could stall task delivery. ``SCAN`` walks the keyspace in
          cursor-sized bites instead.
        """
        client = self.redis
        if client is None:
            return 0
        try:
            if not any(token in pattern for token in "*?["):
                return int(client.delete(pattern))

            deleted = 0
            batch: list[str] = []
            for key in client.scan_iter(match=pattern, count=_SCAN_BATCH):
                batch.append(key)
                if len(batch) >= _SCAN_BATCH:
                    deleted += int(client.delete(*batch))
                    batch = []
            if batch:
                deleted += int(client.delete(*batch))
            return deleted
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

        Reserved for a mutation touching a **system** tag (``user_id IS NULL``):
        that one row appears in every account's list, so renaming, merging or
        promoting it changes what every *other* user's cached list should say,
        and busting only the actor's key leaves everyone else reading the old
        name until ``TTL_TAGS`` expires.

        An owned tag needs nothing this broad — ``on_tags_changed`` busts the
        actor and the touched files' owners instead. Calling this on every tag
        write would drop the whole keyspace's tag cache on each attach; the
        ``system_scope`` flag is what keeps that to the case that earns it.

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
