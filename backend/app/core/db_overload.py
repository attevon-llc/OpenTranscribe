"""Recognise a database overload, so the API can answer it with 503 and ``Retry-After``.

Three exceptions mean "the database side is saturated or this request has run too long",
not "the code is wrong":

* ``sqlalchemy.exc.TimeoutError``: no pooled connection within ``DB_POOL_TIMEOUT_SECONDS``
  (SQLAlchemy, *Connection Pooling*: a checkout beyond capacity blocks up to
  ``pool_timeout`` and then raises it);
* ``sqlalchemy.exc.OperationalError`` wrapping ``psycopg2.errors.QueryCanceled``
  (SQLSTATE 57014): the statement ran past ``statement_timeout``;
* :class:`app.db.request_deadline.RequestDeadlineExceededError`: the request ran past
  ``REQUEST_DEADLINE_SECONDS``.

They used to reach Starlette's ``ServerErrorMiddleware`` as a 500, travelling through the
four ``BaseHTTPMiddleware`` layers and logging a ~200-line chained traceback each, which
under load cost CPU the saturated process did not have (#1337 M2 #3, #7: 718,590 log
lines in one run). RFC 9110 15.6.4 defines 503 as a temporary overload and 10.2.3
``Retry-After`` as when to try again. The HTTP answer lives in
``app/api/overload_response.py`` (FastAPI stays in the API layer); registered through the
exception-handler mechanism, the exception is answered inside ``ExceptionMiddleware`` and
never reaches the outer middleware as an exception.

A broad ``except Exception`` on the request path must not turn one of these into a 401,
an anonymous caller or an empty result: it calls :func:`reraise_db_overload` first
(``tests/unit/test_overload_classes_not_swallowed.py`` enforces it). A handler that maps
any failure to a 5xx ``HTTPException`` is covered by the API's HTTPException handler
instead: :func:`overload_in_chain` finds the overload on the ``__cause__``/``__context__``
chain and the answer is the same 503.
"""

from __future__ import annotations

from prometheus_client import Counter
from psycopg2 import errors as pg_errors
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.db.request_deadline import RequestDeadlineExceededError

CAUSE_POOL_TIMEOUT = "pool_timeout"
CAUSE_DEADLINE = "deadline"
CAUSE_STATEMENT_TIMEOUT = "statement_timeout"

#: How far down a ``__cause__``/``__context__`` chain to look for an overload.
_MAX_CHAIN = 8

api_overload_responses_total = Counter(
    "api_overload_responses_total",
    "API requests answered 503 because the database side was overloaded. cause is "
    "pool_timeout (no connection within DB_POOL_TIMEOUT_SECONDS), statement_timeout "
    "(DB_STATEMENT_TIMEOUT_MS) or deadline (REQUEST_DEADLINE_SECONDS).",
    ["cause"],
)


def overload_cause(exc: BaseException) -> str | None:
    """The overload ``exc`` signals (a bounded label), or ``None`` for any other error."""
    if isinstance(exc, PoolTimeoutError):
        return CAUSE_POOL_TIMEOUT
    if isinstance(exc, RequestDeadlineExceededError):
        return CAUSE_DEADLINE
    if isinstance(exc, OperationalError) and isinstance(exc.orig, pg_errors.QueryCanceled):
        return CAUSE_STATEMENT_TIMEOUT
    return None


def reraise_db_overload(exc: BaseException) -> None:
    """Re-raise ``exc`` if it is a database overload; otherwise return.

    Call it first in a broad ``except`` on the request path, so the overload is answered
    503 by :func:`handle_db_overload` instead of whatever the handler maps failures to.
    """
    if overload_cause(exc) is not None:
        raise exc


def overload_in_chain(exc: BaseException) -> BaseException | None:
    """The overload ``exc`` was raised from (``__cause__``/``__context__``), if any."""
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < _MAX_CHAIN and current not in seen:
        if overload_cause(current) is not None:
            return current
        seen.append(current)
        current = current.__cause__ or current.__context__
    return None


#: Exception classes ``app/main.py`` registers the overload handler for.
OVERLOAD_EXCEPTION_CLASSES: tuple[type[Exception], ...] = (
    PoolTimeoutError,
    OperationalError,
    RequestDeadlineExceededError,
)
