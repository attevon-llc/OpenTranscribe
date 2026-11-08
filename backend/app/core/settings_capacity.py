"""How much concurrent work one API process accepts, and how long it waits for a resource.

Split out of ``app.core.config`` (at its size cap) as one cohesive concern: the database
pool, the worker threads that share it, and the bounds on the request path's Redis calls.
``Settings`` inherits these fields; the env source, ``env_ignore_empty`` and
``extra="ignore"`` come from ``Settings``' own ``model_config``, and this class is never
instantiated on its own. The sizing rule that ties them together is in
``app/core/threadpool.py``.
"""

from pydantic_settings import BaseSettings


class CapacitySettings(BaseSettings):
    # SQLAlchemy connection pool for the FastAPI backend. Celery workers build
    # their own engines, so these sizes mainly control API concurrency.
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 40
    # Worker threads for the API's sync handlers, sync dependencies and
    # run_in_threadpool calls (Starlette's default is 40). 0 = size it to the DB
    # pool capacity (DB_POOL_SIZE + DB_MAX_OVERFLOW, never below 40). A request
    # holds its pooled connection from authentication until it ends, so with fewer
    # threads than connections a burst can leave every thread blocked waiting for a
    # connection while the requests holding them wait for a thread (#1169). See
    # app/core/threadpool.py.
    API_THREADPOOL_SIZE: int = 0
    # Seconds a request waits for a pooled connection before it fails (SQLAlchemy's
    # default is 30). A burst that exhausts the pool then answers an error quickly
    # instead of piling up requests that each hold a thread for half a minute.
    DB_POOL_TIMEOUT_SECONDS: float = 10.0
    # Seconds /health/ready serves its last dependency snapshot before one caller
    # re-probes. Probes run on their own thread limiter and one-connection engine
    # (app/api/health.py), so a busy replica still answers. 0 probes on every call.
    READINESS_CACHE_SECONDS: float = 2.0
    # Open connections (including WebSockets and SSE streams) one uvicorn process
    # accepts before answering 503. uvicorn reads the same variable itself
    # (--limit-concurrency); the app reads it to size the threadpool so that
    # DB pool + threads always exceed it (app/core/threadpool.py). 0 = no limit.
    UVICORN_LIMIT_CONCURRENCY: int = 512

    # Bounds on every Redis call a request makes (chat limits and Stop flags, the
    # retrieval cache, the rate limiter). Without them a Redis that accepts the
    # connection but never answers blocks the calling thread, or the event loop for
    # slowapi's check on an async route, indefinitely. See app/core/redis.py.
    REDIS_REQUEST_SOCKET_TIMEOUT_SECONDS: float = 2.0
    REDIS_REQUEST_CONNECT_TIMEOUT_SECONDS: float = 2.0

    # Worker threads chat may use for its blocking steps (retrieval, masking, persistence,
    # a Bedrock stream). A separate limiter from API_THREADPOOL_SIZE, so open chats can
    # never take every thread the rest of the API needs. 0 = a quarter of the DB pool
    # capacity, at least 4 (app/core/threadpool.py).
    CHAT_THREADPOOL_SIZE: int = 0
    # Chat replies streaming at once across ALL API processes (Redis-backed), on top of
    # the per-user cap in Settings -> Chat. A new message past it is refused with 429
    # and a Retry-After. 0 = no global cap.
    CHAT_MAX_CONCURRENT_STREAMS_GLOBAL: int = 64
