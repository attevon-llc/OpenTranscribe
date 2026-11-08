"""Request and task concurrency limits: threads, lock leases, WebSocket admission.

Split out of ``app.core.config`` (at its size cap) as one cohesive concern, like
``settings_deployment``. ``Settings`` inherits these fields; the env source,
``env_ignore_empty`` and ``extra="ignore"`` come from ``Settings``' own ``model_config``,
and this class is never instantiated on its own. Floors are applied by the validators in
``config.py``.
"""

from pydantic_settings import BaseSettings

#: Shipped default for :attr:`Settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS`.
#: A module constant rather than a literal in the field so a test can pin the
#: DEFAULT independently of whatever an operator set in the running environment.
DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS = 300_000


class ConcurrencySettings(BaseSettings):
    # Worker threads for the API's sync handlers, sync dependencies and
    # run_in_threadpool calls (Starlette's default is 40). 0 = size it to the DB
    # pool capacity (DB_POOL_SIZE + DB_MAX_OVERFLOW, never below 40). A request
    # holds its pooled connection from authentication until it ends, so with fewer
    # threads than connections a burst can leave every thread blocked waiting for a
    # connection while the requests holding them wait for a thread (#1169). See
    # app/core/threadpool.py.
    API_THREADPOOL_SIZE: int = 0

    # Server-side backstop for the "transaction held open across slow work"
    # bug class (issue #440). Postgres terminates a backend that has an OPEN
    # transaction and is running NO query for this long. It cannot interrupt a
    # slow *query* — only an idle one — so a legitimately long-running statement
    # is never affected; the only thing it kills is a connection sitting on
    # ACCESS SHARE locks and pinning the VACUUM horizon while the process does
    # something else (HTTP call, model inference, file I/O).
    #
    # This is defence in depth, NOT the fix: the 35 real leaks were fixed in the
    # code and `scripts/audit-session-lifetime.py` keeps the idiom from
    # returning. 5 minutes is ~10x the slowest legitimate transaction here and
    # ~1/10 of the 48-minute leak that motivated it. Set to 0 to disable.
    #
    # Applies to the shared app engine only. `db/migrations.py` builds its own
    # engines, so a long `ALTER TABLE` and the advisory-lock holder are outside
    # this timeout by construction.
    DB_IDLE_IN_TRANSACTION_TIMEOUT_MS: int = DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS

    # TTL of a task lock's lease (app/utils/task_lock.py). The holder renews it every
    # third of this, so a live run keeps its lock indefinitely and a crashed one frees
    # it within this many seconds. Lower = faster recovery, more renewal traffic.
    TASK_LOCK_LEASE_SECONDS: int = 60

    # WebSocket admission (app/services/ws_admission.py). The /ws route accepts before it
    # authenticates, so these bound what one client can hold open. 0 disables a limit.
    # Handshakes per client IP per minute, counted in Redis across every API process.
    WS_HANDSHAKES_PER_IP_PER_MINUTE: int = 60
    # Open sockets per user in one API process (each browser tab holds one).
    WS_MAX_SOCKETS_PER_USER: int = 20
    # Open sockets in one API process.
    WS_MAX_SOCKETS_PER_PROCESS: int = 2000
    # Frames queued for one socket before it is treated as too slow and dropped.
    WS_SEND_QUEUE_SIZE: int = 100
