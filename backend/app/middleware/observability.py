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
  - **SSE / StreamingResponse**: the status is captured at ``http.response.start``, so the
    recorded duration is time-to-first-byte, not the stream's lifetime. Accepted.
  - **WebSockets** and lifespan scopes pass straight through (http scopes only).
  - ``/metrics`` and ``/metrics/queues`` scrapes are skipped to avoid feedback
    noise (and per-poll cost when an autoscaler polls the queue page).
No blocking I/O happens in ``__call__``.
"""

import logging
import time

from starlette.requests import Request
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from app.core.db_metrics import observe_request_connection_hold
from app.core.db_metrics import observe_request_query_count
from app.core.db_metrics import reset
from app.core.db_metrics import start_request_counter
from app.core.metrics import http_request_duration_seconds
from app.core.metrics import http_requests_in_flight
from app.core.metrics import http_requests_total
from app.core.route_template import route_label

access_logger = logging.getLogger("access")

_SCRAPE_PATHS = frozenset({"/metrics", "/metrics/queues"})


class ObservabilityMiddleware:
    """Pure ASGI, outermost middleware: per-request metrics and a structured access log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Skip self-scrape noise entirely; do not even count the counter.
        if scope["type"] != "http" or scope["path"] in _SCRAPE_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        token = start_request_counter()
        http_requests_in_flight.inc()
        start = time.perf_counter()
        status = 500
        # When the response headers left, as opposed to when the request finished: a route that
        # queues FastAPI BackgroundTasks only completes after them, so ``duration`` includes
        # post-response work the client never waits for.
        response_start: float | None = None

        async def capture_status(message: Message) -> None:
            nonlocal status, response_start
            if message["type"] == "http.response.start":
                status = message["status"]
                response_start = time.perf_counter()
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            duration = time.perf_counter() - start
            method = request.method
            route = route_label(getattr(scope.get("route"), "path", None))
            status_str = str(status)

            http_request_duration_seconds.labels(
                method=method, route=route, status=status_str
            ).observe(duration)
            http_requests_total.labels(method=method, route=route, status=status_str).inc()
            observe_request_query_count(method, route)
            observe_request_connection_hold(route)

            db_query_count = self._safe_query_count()
            ttfb = None if response_start is None else response_start - start
            self._access_log(request, method, route, status, duration, db_query_count, ttfb)

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
        ttfb: float | None = None,
    ) -> None:
        """Emit one access-log line: human message + structured ``extra`` fields."""
        user_id = getattr(request.state, "user_id", None)
        org_id = getattr(request.state, "org_id", None)
        request_id = getattr(request.state, "request_id", "")
        client_ip = getattr(request.state, "client_ip", "unknown")
        duration_ms = duration * 1000.0
        ttfb_ms = None if ttfb is None else ttfb * 1000.0

        access_logger.info(
            "%s %s %s %.1fms user=%s dbq=%s ttfb=%s",
            method,
            route,
            status,
            duration_ms,
            user_id,
            db_query_count,
            "-" if ttfb_ms is None else f"{ttfb_ms:.1f}ms",
            extra={
                "user_id": user_id,
                "org_id": org_id,
                "request_id": request_id,
                "route": route,
                "method": method,
                "status": status,
                "duration_ms": round(duration_ms, 1),
                "db_query_count": db_query_count,
                "ttfb_ms": None if ttfb_ms is None else round(ttfb_ms, 1),
                "client_ip": client_ip,
            },
        )
