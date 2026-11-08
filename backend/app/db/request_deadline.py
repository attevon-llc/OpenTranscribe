"""A per-request deadline, checked before every SQL statement.

No library in the stack bounds a request: uvicorn and Starlette have no request timeout,
and a sync handler running on a worker thread cannot be cancelled from the event loop.
The documented server-side bounds cover one statement (``statement_timeout``) or one
transaction (``transaction_timeout``), not one request made of many short transactions,
which is what #1337 M2 #5 measured: API work of 120-292 s of short statements, long after
the client had given up at 60 s.

So the deadline is a context variable, set by ``app.middleware.request_deadline`` for each
HTTP request, and checked by a ``before_cursor_execute`` listener on the engine. anyio
copies the context into worker threads ("Context propagation",
https://anyio.readthedocs.io/en/stable/threads.html), so sync handlers and
``run_in_threadpool`` calls see the request's deadline. Past it, the next statement raises
:class:`RequestDeadlineExceededError` before it reaches Postgres; the Session rolls back on
close as usual and the API answers 503 (``app.core.db_overload``).

It is not a cancellation: a handler busy in Python with no statements runs on.

Outside a request (Celery workers, startup work, scripts) the variable is ``None`` and the
listener does nothing.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from contextvars import Token
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

#: ``time.monotonic()`` value after which this request issues no more statements.
_deadline: ContextVar[float | None] = ContextVar("request_deadline", default=None)


class RequestDeadlineExceededError(Exception):
    """The request ran past ``REQUEST_DEADLINE_SECONDS``; its next statement was refused."""


def set_request_deadline(seconds: float) -> Token[float | None]:
    """Start the deadline for the current context, ``seconds`` from now."""
    return _deadline.set(time.monotonic() + seconds)


def reset_request_deadline(token: Token[float | None]) -> None:
    """Restore the deadline that was in effect before :func:`set_request_deadline`."""
    _deadline.reset(token)


def _refuse_statement_past_deadline(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise RequestDeadlineExceededError("request deadline passed; statement not run")


def install_request_deadline(engine: Engine) -> Engine:
    """Check the current request's deadline before every statement ``engine`` runs."""
    if not event.contains(engine, "before_cursor_execute", _refuse_statement_past_deadline):
        event.listen(engine, "before_cursor_execute", _refuse_statement_past_deadline)
    return engine
