"""Size the API process's worker-thread pool against its database pool (#1169).

Every number here is PER API PROCESS: with ``UVICORN_WORKERS`` workers the deployment holds that
many pools, thread pools and ``UVICORN_LIMIT_CONCURRENCY`` socket allowances.

Starlette runs every sync handler, sync dependency and ``run_in_threadpool`` call on
anyio's default thread limiter, which allows 40 threads. Authentication returns its
pooled connection as soon as it has decided (``app.db.session_utils.end_read_transaction``);
the handler then takes one at its first query and holds it until the response is sent,
including while it waits for a thread for its next step.

That makes the two pools interact. With ``C`` connections (``DB_POOL_SIZE +
DB_MAX_OVERFLOW``) and ``T`` threads, a burst of ``C + T`` or more concurrent
requests can reach a state where all ``C`` connections belong to requests waiting for
a thread, and all ``T`` threads belong to requests waiting for a connection. Nothing
moves until the pool timeout (``DB_POOL_TIMEOUT_SECONDS``) answers the waiters 503.
Fewer than ``C + T`` concurrent requests cannot get there, because at most
``concurrency - C`` threads can be blocked on the pool.

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
is work, so the large ``T`` costs nothing at rest. ``DB_POOL_TIMEOUT_SECONDS`` (5 s)
bounds any residual wait, and the waiter is answered 503 with ``Retry-After``
(``app/api/overload_response.py``), not 500. To serve more open connections, raise ``L``
(``T`` follows) or add API processes; to serve more concurrent database work, raise the
pool (and ``PG_MAX_CONNECTIONS``).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import weakref
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterator
from typing import cast

import anyio
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


#: Floor for the chat limiter, so a tiny pool still lets a few turns prepare at once.
CHAT_MIN_THREADS = 4

_chat_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = (
    weakref.WeakKeyDictionary()
)


def chat_threadpool_size() -> int:
    """Threads chat may use at once: ``CHAT_THREADPOOL_SIZE``, or a quarter of the DB pool."""
    if settings.CHAT_THREADPOOL_SIZE > 0:
        return settings.CHAT_THREADPOOL_SIZE
    return max(CHAT_MIN_THREADS, (settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW) // 4)


def chat_thread_limiter() -> anyio.CapacityLimiter:
    """The running loop's chat limiter (anyio limiters belong to one event loop).

    Chat's blocking steps run under this instead of the default limiter, so however many
    chats are open they hold at most :func:`chat_threadpool_size` threads, and never one
    of the threads the rest of the API needs (audit H1).
    """
    loop = asyncio.get_running_loop()
    limiter = _chat_limiters.get(loop)
    if limiter is None:
        limiter = anyio.CapacityLimiter(chat_threadpool_size())
        _chat_limiters[loop] = limiter
    return limiter


async def run_in_chat_thread[T](func: Callable[..., T], *args: object, **kwargs: object) -> T:
    """``run_in_threadpool`` for chat: the same contextvar-copying hop, on the chat limiter."""
    return await anyio.to_thread.run_sync(
        functools.partial(func, *args, **kwargs), limiter=chat_thread_limiter()
    )


_EXHAUSTED = object()


def _next_or_exhausted[T](iterator: Iterator[T]) -> object:
    return next(iterator, _EXHAUSTED)


async def iterate_in_chat_thread[T](iterator: Iterator[T]) -> AsyncIterator[T]:
    """Drive a blocking iterator one item per chat-thread hop (``iterate_in_threadpool``'s twin).

    Only for an SDK that offers no async API (Bedrock's boto3 stream). Between items no
    thread is held.
    """
    while True:
        item = await run_in_chat_thread(_next_or_exhausted, iterator)
        if item is _EXHAUSTED:
            return
        yield cast(T, item)
