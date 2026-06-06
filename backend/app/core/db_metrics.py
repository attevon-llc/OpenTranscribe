"""SQLAlchemy engine instrumentation: per-statement timing + per-request counts.

Two engine event listeners (registered from ``app.db.base`` immediately after
the engine is created) observe every SQL statement:
  - ``db_query_duration_seconds`` for each statement (no labels — cardinality);
  - a per-request counter that powers ``db_queries_per_request`` (the
    duplicate/N+1 detector), and
  - a slow-query WARNING above ``settings.SLOW_QUERY_MS``.

Per-request counter — ContextVar propagation trap (critical):
``BaseHTTPMiddleware`` runs ``call_next`` in a CHILD task; a ContextVar value
**set** inside that child does NOT propagate back to the middleware's own
context. So the ContextVar holds a **mutable dict**: the middleware sets it
ONCE; the child task and the sync-handler threadpool COPY the context and share
the same dict object, so the listeners' in-place ``["n"] += 1`` mutations are
visible to the middleware after ``call_next``. Listeners only ever MUTATE the
dict; they never ``set()`` the ContextVar.

When the ContextVar is ``None`` (Celery workers, startup tasks, scripts — all
import the same engine), the per-request counter is a no-op, but
``db_query_duration_seconds`` is still observed (desired: those queries count
toward latency).
"""

import time
from contextvars import ContextVar
from contextvars import Token
from typing import Any

from app.core.config import settings
from app.core.metrics import db_queries_per_request
from app.core.metrics import db_query_duration_seconds

# Holds a mutable dict ``{"n": <int>}`` while inside an HTTP request, else None.
_query_counter: ContextVar[dict[str, int] | None] = ContextVar("db_query_counter", default=None)

# Per-statement start time is stashed on the execution context, which is unique
# to each cursor execution — safe under threads and the async event loop.
_START_ATTR = "_obs_query_start"


def start_request_counter() -> Token:
    """Begin counting DB statements for the current request.

    Returns a token to pass to :func:`reset`. The stored object is a mutable
    dict so context copies (child task / threadpool) share the same counter.
    """
    return _query_counter.set({"n": 0})


def get_request_query_count() -> int:
    """Return the number of DB statements observed for the current request."""
    counter = _query_counter.get()
    return counter["n"] if counter is not None else 0


def reset(token: Token) -> None:
    """Detach the per-request counter (call in the middleware ``finally``)."""
    _query_counter.reset(token)


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany) -> None:
    """Stash a high-resolution start time on the execution context."""
    setattr(context, _START_ATTR, time.perf_counter())


def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany) -> None:
    """Observe duration, bump the per-request counter, emit slow-query warnings."""
    start = getattr(context, _START_ATTR, None)
    duration = time.perf_counter() - start if start is not None else 0.0

    db_query_duration_seconds.observe(duration)

    counter = _query_counter.get()
    if counter is not None:
        counter["n"] += 1

    if duration * 1000.0 >= settings.SLOW_QUERY_MS:
        _log_slow_query(statement, duration)


def _log_slow_query(statement: str, duration: float) -> None:
    """Log a slow statement — first 120 chars only, NEVER parameters (PII/secrets)."""
    # Imported lazily to avoid a circular import at module load and to keep the
    # listeners importable in worker/script contexts where audit isn't wired.
    import logging

    from app.middleware.audit import get_request_id

    logging.getLogger("app.core.db_metrics").warning(
        "Slow query (%.1f ms) request_id=%s: %s",
        duration * 1000.0,
        get_request_id() or "-",
        statement[:120].replace("\n", " "),
    )


def register_listeners(engine: Any) -> None:
    """Attach the timing/counter listeners to a SQLAlchemy engine (idempotent)."""
    from sqlalchemy import event

    if not event.contains(engine, "before_cursor_execute", _before_cursor_execute):
        event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    if not event.contains(engine, "after_cursor_execute", _after_cursor_execute):
        event.listen(engine, "after_cursor_execute", _after_cursor_execute)


def observe_request_query_count(method: str, route: str) -> None:
    """Record the finished request's statement count into the histogram."""
    db_queries_per_request.labels(method=method, route=route).observe(get_request_query_count())
