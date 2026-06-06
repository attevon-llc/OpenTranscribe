"""Request-level observability middleware (metrics + structured access log).

``ObservabilityMiddleware`` wraps every HTTP request and, in a ``finally`` block
(so it records even when the handler raises), emits:
  - ``http_request_duration_seconds`` / ``http_requests_total`` / the in-flight
    gauge, labelled by method, route TEMPLATE, and status;
  - ``db_queries_per_request`` for the request's SQL statement count;
  - one access-log line on the ``"access"`` logger carrying ``user_id``,
    ``org_id``, ``request_id``, ``route``, ``method``, ``status``,
    ``duration_ms``, ``db_query_count``, ``client_ip`` (both a human-readable
    message AND the same fields via ``extra`` → readable in text mode,
    structured in JSON mode).

This middleware is added LAST in ``app.main`` and therefore runs OUTERMOST
(Starlette ``add_middleware`` inserts at index 0; the stack wraps reversed —
verified 0.48.0). It must re-raise unhandled exceptions: swallowing one would
break the ``OpenTranscribeError`` / slowapi handlers that live inside the app.

Edge cases:
  - **Route template** (``request.scope["route"].path``) is populated only
    AFTER routing, so it is read post-``call_next``; 404/unrouted → ``"unknown"``.
    The ``/api`` prefix is intentionally kept (bounded cardinality).
  - **CSRF-rejected / pre-routing requests** never had ``request_id``/``user_id``
    set by inner layers, so those fields are absent/empty — correct.
  - **SSE / StreamingResponse**: ``call_next`` returns at response START, so the
    recorded duration is time-to-first-byte, not the stream's lifetime. Accepted.
  - **WebSockets** bypass BaseHTTPMiddleware entirely (http scopes only).
  - ``/metrics`` self-scrapes are skipped to avoid feedback noise.
No blocking I/O happens in ``dispatch``.
"""

import logging
import time
from collections.abc import Awaitable
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.db_metrics import observe_request_query_count
from app.core.db_metrics import reset
from app.core.db_metrics import start_request_counter
from app.core.metrics import http_request_duration_seconds
from app.core.metrics import http_requests_in_flight
from app.core.metrics import http_requests_total
from app.core.route_template import route_label

access_logger = logging.getLogger("access")

_METRICS_PATH = "/metrics"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Outermost middleware: per-request metrics and a structured access log."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Skip self-scrape noise entirely; do not even count the counter.
        if request.url.path == _METRICS_PATH:
            return await call_next(request)

        token = start_request_counter()
        http_requests_in_flight.inc()
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration = time.perf_counter() - start
            method = request.method
            route = route_label(getattr(request.scope.get("route"), "path", None))
            status_str = str(status)

            http_request_duration_seconds.labels(
                method=method, route=route, status=status_str
            ).observe(duration)
            http_requests_total.labels(method=method, route=route, status=status_str).inc()
            observe_request_query_count(method, route)

            db_query_count = self._safe_query_count()
            self._access_log(request, method, route, status, duration, db_query_count)

            http_requests_in_flight.dec()
            reset(token)

    @staticmethod
    def _safe_query_count() -> int:
        from app.core.db_metrics import get_request_query_count

        return get_request_query_count()

    @staticmethod
    def _access_log(
        request: Request,
        method: str,
        route: str,
        status: int,
        duration: float,
        db_query_count: int,
    ) -> None:
        """Emit one access-log line: human message + structured ``extra`` fields."""
        user_id = getattr(request.state, "user_id", None)
        org_id = getattr(request.state, "org_id", None)
        request_id = getattr(request.state, "request_id", "")
        client_ip = getattr(request.state, "client_ip", "unknown")
        duration_ms = duration * 1000.0

        access_logger.info(
            "%s %s %s %.1fms user=%s dbq=%s",
            method,
            route,
            status,
            duration_ms,
            user_id,
            db_query_count,
            extra={
                "user_id": user_id,
                "org_id": org_id,
                "request_id": request_id,
                "route": route,
                "method": method,
                "status": status,
                "duration_ms": round(duration_ms, 1),
                "db_query_count": db_query_count,
                "client_ip": client_ip,
            },
        )
