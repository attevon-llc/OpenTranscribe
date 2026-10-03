"""Size the API process's worker-thread pool against its database pool (#1169).

Starlette runs every sync handler, sync dependency and ``run_in_threadpool`` call on
anyio's default thread limiter, which allows 40 threads. Requests in this app hold
their pooled database connection from authentication until the response is sent,
including while they wait for a thread for their next step.

That makes the two pools interact. With ``C`` connections (``DB_POOL_SIZE +
DB_MAX_OVERFLOW``) and ``T`` threads, a burst of ``C + T`` or more concurrent
requests can reach a state where all ``C`` connections belong to requests waiting for
a thread, and all ``T`` threads belong to requests waiting for a connection. Nothing
moves until the pool timeout (30 s) fails the waiters. Fewer than ``C + T``
concurrent requests cannot get there, because at most ``concurrency - C`` threads can
be blocked on the pool.

The default here sizes ``T`` to at least ``C``, so a single API process stays clear
of that state up to twice its pool capacity of concurrent requests (120 with the
default 20 + 40 pool). Beyond that, raise the pool (and ``PG_MAX_CONNECTIONS``) or
add API processes rather than threads.
"""

from __future__ import annotations

import logging

import anyio.to_thread

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Starlette/anyio's own default; the API never runs with fewer threads than this.
STARLETTE_DEFAULT_THREADS = 40


def api_threadpool_size() -> int:
    """Threads the API process should run with.

    ``API_THREADPOOL_SIZE`` when set (> 0); otherwise the DB pool capacity, floored
    at Starlette's default of 40.
    """
    if settings.API_THREADPOOL_SIZE > 0:
        return settings.API_THREADPOOL_SIZE
    return max(STARLETTE_DEFAULT_THREADS, settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW)


def configure_api_threadpool() -> int:
    """Apply :func:`api_threadpool_size` to the running event loop's thread limiter.

    Must be called from inside the event loop (the limiter is per loop), which is
    why the lifespan calls it rather than module import.

    Returns:
        The number of threads now allowed.
    """
    size = api_threadpool_size()
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = size
    pool_capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    if size < pool_capacity:
        logger.warning(
            "API threadpool (%d) is smaller than the DB pool capacity (%d); a burst of "
            "%d+ concurrent requests can wait on the pool timeout",
            size,
            pool_capacity,
            size + pool_capacity,
        )
    else:
        logger.info("API threadpool: %d threads (DB pool capacity %d)", size, pool_capacity)
    return size
