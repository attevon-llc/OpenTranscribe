"""The HTTP answer to a database overload: 503, ``Retry-After``, one log line.

What counts as an overload is decided in ``app/core/db_overload.py``; this module is the
API-layer half (FastAPI and Starlette stay in ``app/api``, like ``hold_refusal.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response

from app.core.config import settings
from app.core.db_overload import api_overload_responses_total
from app.core.db_overload import overload_cause
from app.core.db_overload import overload_in_chain

logger = logging.getLogger(__name__)

SERVER_BUSY_CODE = "server_busy"
SERVER_BUSY_DETAIL = "Server busy, try again shortly"


def _pool_usage() -> str:
    from sqlalchemy.pool import QueuePool

    from app.db.base import engine

    pool = engine.pool
    if not isinstance(pool, QueuePool):
        return "n/a"
    capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    return f"{pool.checkedout()}/{capacity}"


def _route_template(request: Request) -> str:
    route: Any = request.scope.get("route")
    return str(getattr(route, "path", None) or "<unmatched>")


def _server_busy(request: Request, exc: BaseException, cause: str) -> JSONResponse:
    api_overload_responses_total.labels(cause=cause).inc()
    logger.warning(
        "Database overload: %s %s answered 503 (cause=%s, %s; pool %s checked out)",
        request.method,
        _route_template(request),
        cause,
        type(exc).__name__,
        _pool_usage(),
    )
    return JSONResponse(
        status_code=503,
        content={"detail": SERVER_BUSY_DETAIL, "code": SERVER_BUSY_CODE},
        headers={"Retry-After": str(max(1, int(settings.DB_OVERLOAD_RETRY_AFTER_SECONDS)))},
    )


async def handle_db_overload(request: Request, exc: Exception) -> Response:
    """Exception handler for the three overload classes; anything else stays a 500."""
    cause = overload_cause(exc)
    if cause is None:
        raise exc
    return _server_busy(request, exc, cause)


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    """FastAPI's own HTTPException handler, except a 5xx raised from an overload is a 503."""
    assert isinstance(exc, StarletteHTTPException)  # registered for that class only
    if exc.status_code >= 500:
        overload = overload_in_chain(exc)
        if overload is not None:
            cause = overload_cause(overload)
            assert cause is not None  # overload_in_chain only returns an overload
            return _server_busy(request, overload, cause)
    return await http_exception_handler(request, exc)
