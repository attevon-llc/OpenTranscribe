"""Shared synchronous Redis client singleton.

All synchronous code that needs a Redis connection should import
``get_redis()`` from this module instead of calling
``redis.from_url()`` directly.  The client is lazily created once
per process via ``@lru_cache`` and reused for the lifetime of the
worker / API server.

``get_request_redis()`` is for calls made while serving a request (every API route,
auth, the services and task-plane helpers they reach; a guard test enforces it): same
URL, with the configurable
``REDIS_REQUEST_*_TIMEOUT_SECONDS`` bounds, so a Redis that stops answering costs a
request two seconds and a fail-open, never a thread held forever.

``get_probe_redis()`` is the other sanctioned client: same URL, but with
short socket timeouts, for the readiness probe and for the few callers that must
never block (the worker's loaded-model report, which runs on ``worker_ready`` in
the MainProcess main thread). The shared client keeps
redis-py's unbounded defaults on purpose — blocking reads and pub/sub waits
use it, and a global socket timeout would break them.

Out of scope (they use separate Redis databases or async clients):
- ``api/rate_limit.py`` / ``auth/lockout_store.py`` (Redis db != 0)
- ``redis_cache_service.py`` (db=1)
- ``youtube_rate_limiter.py`` (db=1)
- ``video_processing_service.py`` (async)
- ``api/websockets.py`` (async subscriber)

``get_async_redis()`` is the one shared ``redis.asyncio`` client for the API's SSE
streams (download progress, bulk export). Its connection pool belongs to the event loop
it was created on, so there is one per loop; a stream borrows a connection for its
pub/sub and returns it on close, and never closes the client itself.
"""

import asyncio
import weakref
from functools import lru_cache
from typing import Any

import redis
import redis.asyncio as aioredis

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
def get_request_redis() -> redis.Redis:
    """Return a Redis client (db 0) with bounded socket timeouts, for request-path calls."""
    return redis.from_url(
        settings.REDIS_URL,
        socket_timeout=settings.REDIS_REQUEST_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=settings.REDIS_REQUEST_CONNECT_TIMEOUT_SECONDS,
    )


@lru_cache(maxsize=1)
def get_probe_redis() -> redis.Redis:
    """Return a Redis client (db 0) whose every operation is time-bounded, for health probes."""
    return redis.from_url(
        settings.REDIS_URL,
        socket_timeout=DEPENDENCY_PROBE_TIMEOUT_SECONDS,
        socket_connect_timeout=DEPENDENCY_PROBE_TIMEOUT_SECONDS,
    )


_async_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis] = (
    weakref.WeakKeyDictionary()
)


def get_async_redis() -> aioredis.Redis:
    """Return the running event loop's shared async Redis client (db 0).

    One client, and so one connection pool, per loop rather than one per SSE stream: a
    pool built per stream opened and tore down its own connections for every progress
    stream a browser kept open. Must be called from inside the loop.
    """
    loop = asyncio.get_running_loop()
    client = _async_clients.get(loop)
    if client is None:
        client = aioredis.from_url(
            settings.REDIS_URL, health_check_interval=30, socket_keepalive=True
        )
        _async_clients[loop] = client
    return client
