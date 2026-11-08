"""Refuse every write on a public demo deployment (issue #628, exit criterion E1).

Default-deny, path-agnostic: with ``DEMO_MODE`` on, any request whose method is not
``GET``/``HEAD``/``OPTIONS`` is answered 403 here, before routing, dependencies or body
parsing, unless ``"METHOD /exact/path"`` is in :data:`DEMO_WRITE_ALLOWLIST`. A route added
tomorrow is refused the moment it merges, with no author action; appearing in the allowlist
is a reviewed act with a written reason. The allowlist holds only parameter-free paths, so
the comparison is an exact string match after dropping a trailing slash: there is no template
to probe for gaps, and an unknown or unrouted path is refused like any other.

Pure ASGI rather than ``BaseHTTPMiddleware`` because Starlette's ``BaseHTTPMiddleware`` passes
every non-``http`` scope straight through, so it would never see ``/api/ws``. A WebSocket
handshake is closed before it is accepted (``WebSocketClose``); per the ASGI spec a close
sent before ``websocket.accept`` makes the server reject the handshake with HTTP 403. A
correctly inert demo has no live progress to push, and on a shared demo account the socket
would otherwise be a broadcast bus between strangers.

The setting is read per request so tests (and nothing else) can flip it; the middleware is
registered on every deployment and is a pass-through unless ``DEMO_MODE`` is on.
``tests/unit/test_demo_readonly_coverage.py`` drives every route of the real app through it.
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send
from starlette.websockets import WebSocketClose

from app.core.config import settings

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

REFUSAL_CODE = "demo_read_only"
REFUSAL_MESSAGE = "This is a read-only demo. Changes are turned off."

#: RFC 6455 "policy violation".
WEBSOCKET_REFUSAL_CODE = 1008

#: ``"METHOD /path"`` -> why a demo visitor needs it. Parameter-free paths only.
DEMO_WRITE_ALLOWLIST: dict[str, str] = {
    "POST /api/auth/login": (
        "visitors sign in with the published demo credentials; the session row it creates "
        "is destroyed by the nightly reset"
    ),
    "POST /api/auth/demo/session": (
        "passwordless sign-in as the shared demo user (exempt from password lockout, limited "
        "per IP); creates a session row that the nightly reset destroys"
    ),
    "POST /api/auth/token": (
        "OAuth2 password form used by the API docs and API clients; same effect as login"
    ),
    "POST /api/auth/token/refresh": (
        "keeps a signed-in visitor's session alive; rotates that visitor's refresh token only"
    ),
    "POST /api/auth/logout": "ends the visitor's own session",
    "POST /api/auth/logout/all": (
        "ends the demo account's sessions; on the shared account other visitors sign in again"
    ),
}


def write_key(method: str, path: str) -> str:
    """The allowlist key for a request: upper-case method, path without a trailing slash."""
    return f"{method.upper()} {path.rstrip('/') or '/'}"


def is_allowed_in_demo(method: str, path: str) -> bool:
    """Whether a demo deployment lets this HTTP request through."""
    return method.upper() in SAFE_METHODS or write_key(method, path) in DEMO_WRITE_ALLOWLIST


class DemoReadOnlyMiddleware:
    """Pure ASGI guard; a pass-through unless ``settings.DEMO_MODE`` is on."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not settings.DEMO_MODE or scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=WEBSOCKET_REFUSAL_CODE, reason=REFUSAL_CODE)(
                scope, receive, send
            )
            return
        if scope["type"] == "http" and is_allowed_in_demo(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        # Any other scope type, or a refused HTTP method: fail closed.
        logger.info("demo read-only: refused %s %s", scope.get("method"), scope.get("path"))
        response = JSONResponse(
            {"detail": {"code": REFUSAL_CODE, "message": REFUSAL_MESSAGE}}, status_code=403
        )
        await response(scope, receive, send)
