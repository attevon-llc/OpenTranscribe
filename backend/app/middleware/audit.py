"""
Audit Middleware for Request ID Tracking.

Assigns a unique request ID to each incoming request and makes it available
throughout the request lifecycle for audit logging correlation.
"""

import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Context variable for request ID - thread-safe for async operations
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """
    Get the current request ID.

    Returns:
        The request ID for the current request context, or empty string if not set.
    """
    return _request_id_var.get()


def set_request_id(request_id: str) -> None:
    """
    Set the request ID for the current context.

    Args:
        request_id: The request ID to set
    """
    _request_id_var.set(request_id)


class AuditMiddleware(BaseHTTPMiddleware):
    """
    Middleware that assigns unique request IDs for audit log correlation.

    The request ID is:
    - Generated as a UUID4 if not provided
    - Stored in a context variable for access throughout the request
    - Added to response headers as X-Request-ID
    - Used to correlate all audit log entries for a single request
    """

    REQUEST_ID_HEADER = "X-Request-ID"

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """
        Process the request and add request ID tracking.

        Args:
            request: The incoming request
            call_next: The next middleware/handler in the chain

        Returns:
            The response with X-Request-ID header added
        """
        # Get request ID from header or generate new one
        request_id = request.headers.get(self.REQUEST_ID_HEADER)
        if not request_id:
            request_id = str(uuid.uuid4())

        # Set in context variable for audit logging
        set_request_id(request_id)

        # Also set on request state for easy access in handlers
        request.state.request_id = request_id

        # Extract client info for audit logging
        request.state.client_ip = self._get_client_ip(request)
        request.state.user_agent = request.headers.get("User-Agent", "")

        # Process request
        response = await call_next(request)

        # Add request ID to response headers
        response.headers[self.REQUEST_ID_HEADER] = request_id

        return response

    def _get_client_ip(self, request: Request) -> str:
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
