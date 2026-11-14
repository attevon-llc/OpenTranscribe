"""Set each HTTP request's deadline (``app.db.request_deadline``).

Pure ASGI middleware, the shape Starlette recommends ("Pure ASGI Middleware",
https://www.starlette.io/middleware/#pure-asgi-middleware): it adds no child task, so the
context variable it sets is the one the route, its dependencies and their worker threads
inherit. ``app/main.py`` adds it FIRST, which makes it the innermost middleware (Starlette:
last added = outermost), so the deadline covers the app's own work and none of the outer
layers.

``REQUEST_DEADLINE_SECONDS`` (default 55) is below the 60 s that Locust and most clients
give up at, and far below nginx's ``proxy_read_timeout 300s`` (``frontend/nginx.conf``).
0 or less disables it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from starlette.routing import compile_path
from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from app.core.config import settings
from app.db.request_deadline import reset_request_deadline
from app.db.request_deadline import set_request_deadline


@dataclass(frozen=True)
class DeadlineExemption:
    """A route whose work legitimately outlasts a request deadline."""

    method: str
    template: str
    reason: str


_API = settings.API_PREFIX

#: Route templates that never get a deadline, each with its reason. A test checks every
#: template against ``app.routes``, so a renamed route fails it instead of silently
#: losing its exemption.
EXEMPT_ROUTES: tuple[DeadlineExemption, ...] = (
    DeadlineExemption("POST", f"{_API}/files", "upload: the body streams for as long as it takes"),
    DeadlineExemption(
        "POST",
        f"{_API}/speaker-profiles/profiles/{{profile_uuid}}/avatar",
        "upload: the body streams for as long as it takes",
    ),
    DeadlineExemption(
        "POST",
        f"{_API}/chat/conversations/{{conversation_uuid}}/messages",
        "SSE chat reply; closes the request session first and uses its own session_scope",
    ),
    DeadlineExemption(
        "POST",
        f"{_API}/chat/conversations/{{conversation_uuid}}/regenerate",
        "SSE chat reply; closes the request session first and uses its own session_scope",
    ),
    DeadlineExemption(
        "POST",
        f"{_API}/chat/conversations/{{conversation_uuid}}/messages/{{message_uuid}}/edit",
        "SSE chat reply; closes the request session first and uses its own session_scope",
    ),
    DeadlineExemption(
        "GET",
        f"{_API}/files/{{file_uuid}}/download-stream",
        "streaming download: runs as long as the transfer",
    ),
    DeadlineExemption(
        "GET",
        f"{_API}/files/bulk-export-stream",
        "streaming export: runs as long as the transfer",
    ),
    DeadlineExemption(
        "POST",
        f"{_API}/llm-settings/config/{{config_uuid}}/reasoning-probe",
        "three real generations, minutes on a slow local model; releases the request session "
        "before dialling, and is bounded by 3 x LLM_REASONING_PROBE_TIMEOUT_S",
    ),
    DeadlineExemption("GET", "/metrics", "Prometheus scrape; not user work"),
)

_EXEMPT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (route.method, compile_path(route.template)[0]) for route in EXEMPT_ROUTES
)


def is_exempt(method: str, path: str) -> bool:
    """Whether a request for ``method`` ``path`` runs without a deadline."""
    return any(m == method and pattern.match(path) for m, pattern in _EXEMPT_PATTERNS)


class RequestDeadlineMiddleware:
    """Give every non-exempt HTTP request ``REQUEST_DEADLINE_SECONDS`` of statements."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        seconds = float(settings.REQUEST_DEADLINE_SECONDS)
        if scope["type"] != "http" or seconds <= 0 or is_exempt(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        token = set_request_deadline(seconds)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_deadline(token)
