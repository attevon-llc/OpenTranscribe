"""
Audit Middleware for Request ID Tracking.

Assigns a unique request ID to each incoming request and makes it available
throughout the request lifecycle for audit logging correlation.
"""

import uuid

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.requests import Request
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

# The correlation slot lives in app.core.request_context and is shared with
# app.auth.audit. This module used to declare its OWN ContextVar("request_id"),
# which looked identical to the audit logger's but was a different object — so
# nothing the middleware set was ever visible to an audit event. Re-exported here
# because `app.middleware.get_request_id` is the established import path
# (logging_config, db_metrics, the Celery pre-run hook, tests).
from app.core.request_context import get_request_id
from app.core.request_context import set_request_id

__all__ = [
    "AuditMiddleware",
    "get_request_context",
    "get_request_id",
    "set_request_id",
]


class AuditMiddleware:
    """
    Pure ASGI middleware that assigns unique request IDs for audit log correlation.

    The request ID is:
    - Generated as a UUID4 if not provided
    - Stored in a context variable for access throughout the request
    - Added to response headers as X-Request-ID
    - Used to correlate all audit log entries for a single request
    """

    REQUEST_ID_HEADER = "X-Request-ID"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = request.headers.get(self.REQUEST_ID_HEADER)
        if not request_id:
            request_id = str(uuid.uuid4())

        set_request_id(request_id)

        # scope["state"] is shared with every inner layer and handler.
        request.state.request_id = request_id
        request.state.client_ip = self._get_client_ip(request)
        request.state.user_agent = request.headers.get("User-Agent", "")

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[self.REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)

    def _get_client_ip(self, request: HTTPConnection) -> str:
        """
        Extract the client IP address from the request.

        Delegates to the shared trusted-proxy resolver. This method previously trusted
        ``X-Forwarded-For`` and ``X-Real-IP`` **unconditionally**, so any client could
        forge the address recorded against it in the audit log simply by sending the
        header — in a security audit trail, of all places (issue #284 A0.5).

        Args:
            request: The incoming request

        Returns:
            The client IP address
        """
        from app.utils.client_ip import resolve_client_ip

        return resolve_client_ip(request)


def get_request_context(request: Request) -> dict:
    """
    Get audit context from a request.

    Args:
        request: The FastAPI/Starlette request

    Returns:
        Dictionary with request_id, client_ip, and user_agent
    """
    return {
        "request_id": getattr(request.state, "request_id", get_request_id()),
        "source_ip": getattr(request.state, "client_ip", "unknown"),
        "user_agent": getattr(request.state, "user_agent", ""),
    }
