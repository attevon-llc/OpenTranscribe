"""Shared synchronous Redis client singleton.

All synchronous code that needs a Redis connection should import
``get_redis()`` from this module instead of calling
``redis.from_url()`` directly.  The client is lazily created once
per process via ``@lru_cache`` and reused for the lifetime of the
worker / API server.

``get_probe_redis()`` is the one sanctioned second client: same URL, but with
short socket timeouts, for the readiness probe only. The shared client keeps
redis-py's unbounded defaults on purpose — blocking reads and pub/sub waits
use it, and a global socket timeout would break them.

Out of scope (they use separate Redis databases or async clients):
- ``auth/rate_limit.py`` / ``auth/lockout.py`` (Redis db != 0)
- ``redis_cache_service.py`` (db=1)
- ``youtube_rate_limiter.py`` (db=1)
- ``video_processing_service.py`` (async)
- ``api/websockets.py`` (async subscriber)
"""

from functools import lru_cache
from typing import Any

import redis

from app.core.config import settings
from app.core.constants import DEPENDENCY_PROBE_TIMEOUT_SECONDS


def redis_tls_kwargs() -> dict[str, Any]:
    """``redis.Redis(host=...)`` kwargs that honour ``REDIS_USE_TLS``.

    ``REDIS_URL`` already carries ``rediss://``, but clients built from
    host/port (the db=1 cache, its pub/sub push, the YouTube limiter) do not.
    Against a TLS-only server a plaintext PING is never answered, surfacing as
    ``Timeout reading from socket`` and a permanently disabled cache.
    """
    return {"ssl": True} if settings.REDIS_USE_TLS else {}


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    """Return a process-wide singleton Redis client (db 0)."""
    return redis.from_url(settings.REDIS_URL)


@lru_cache(maxsize=1)
def get_probe_redis() -> redis.Redis:
    """Return a Redis client (db 0) whose every operation is time-bounded, for health probes."""
    return redis.from_url(
        settings.REDIS_URL,
        socket_timeout=DEPENDENCY_PROBE_TIMEOUT_SECONDS,
        socket_connect_timeout=DEPENDENCY_PROBE_TIMEOUT_SECONDS,
    )
