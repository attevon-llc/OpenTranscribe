"""Size the API process's worker-thread pool against its database pool (#1169).

Starlette runs every sync handler, sync dependency and ``run_in_threadpool`` call on
anyio's default thread limiter, which allows 40 threads. Requests in this app hold
their pooled database connection from authentication until the response is sent,
including while they wait for a thread for their next step.

That makes the two pools interact. With ``C`` connections (``DB_POOL_SIZE +
DB_MAX_OVERFLOW``) and ``T`` threads, a burst of ``C + T`` or more concurrent
requests can reach a state where all ``C`` connections belong to requests waiting for
a thread, and all ``T`` threads belong to requests waiting for a connection. Nothing
moves until the pool timeout (``DB_POOL_TIMEOUT_SECONDS``) fails the waiters. Fewer than ``C + T``
concurrent requests cannot get there, because at most ``concurrency - C`` threads can
be blocked on the pool.

Sizing rule (audit H2). uvicorn's ``--limit-concurrency`` ``L``
(``UVICORN_LIMIT_CONCURRENCY``) answers 503 once ``L`` connections are open, so at most
``L`` requests are ever in flight. Keeping ``L < C + T`` therefore makes the stall above
unreachable: overload is refused with a 503 instead of queueing into it. The default
sizes ``T`` to ``max(40, C, L - C + 1)``.

⚠️ uvicorn counts EVERY open connection toward ``L`` (``protocols/http/*_impl.py``:
``len(self.connections) >= limit_concurrency``), WebSockets and SSE streams included, so
``L`` cannot simply be set just under ``C + T`` (120 by default): the 121st open browser
tab would be refused. ``L`` is sized for the sockets one process should hold (512 by
default) and ``T`` follows it. Threads are created on demand and only run while there
is work, so the large ``T`` costs nothing at rest. ``DB_POOL_TIMEOUT_SECONDS`` (10 s)
bounds any residual wait. To serve more open connections, raise ``L`` (``T`` follows) or
add API processes; to serve more concurrent database work, raise the pool (and
``PG_MAX_CONNECTIONS``).
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
    at Starlette's default of 40 and raised far enough that pool + threads exceed
    uvicorn's connection limit (see the module docstring).
    """
    if settings.API_THREADPOOL_SIZE > 0:
        return settings.API_THREADPOOL_SIZE
    pool_capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    above_connection_limit = settings.UVICORN_LIMIT_CONCURRENCY - pool_capacity + 1
    return max(STARLETTE_DEFAULT_THREADS, pool_capacity, above_connection_limit)


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
    limit = settings.UVICORN_LIMIT_CONCURRENCY
    if limit > 0 and limit >= size + pool_capacity:
        logger.warning(
            "UVICORN_LIMIT_CONCURRENCY (%d) is not below DB pool capacity + threads (%d); "
            "an overload can stall on the pool timeout instead of answering 503",
            limit,
            size + pool_capacity,
        )
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
