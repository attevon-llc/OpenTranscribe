"""CSRF protection middleware using double-submit cookie pattern.

When the frontend authenticates via httpOnly cookies (no Authorization
header), the CSRF middleware requires a matching X-CSRF-Token header on
all mutating requests (POST/PUT/PATCH/DELETE). The token value must
match the csrf_token cookie.

Requests that use Bearer token authentication are exempt — API clients
and Swagger UI don't use cookies and therefore aren't vulnerable to CSRF.
"""

import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Methods that don't modify state — no CSRF check needed
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths that must be exempt from CSRF (they can't send a CSRF cookie yet)
_EXEMPT_PREFIXES = (
    "/api/auth/login",
    "/api/auth/token",
    "/api/auth/register",
    "/api/auth/password-reset/",
    "/api/auth/keycloak/",
    "/api/auth/pki/",
    "/api/auth/mfa/",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/health",
    # Server-to-server webhooks (cloud edition: managed IdP / billing). They carry no
    # cookies and authenticate by cryptographic signature on the raw body —
    # CSRF does not apply and would silently 403 every delivery.
    "/api/webhooks/",
)

# WebSocket paths are upgraded before middleware runs but check just in case
_WS_PREFIXES = ("/api/ws",)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Validates CSRF double-submit token on non-safe, cookie-authenticated requests."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN201
        # Safe methods never need CSRF
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        path = request.url.path

        # Exempt paths (login, register, etc.)
        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        # WebSocket paths
        if any(path.startswith(p) for p in _WS_PREFIXES):
            return await call_next(request)

        # If the request uses Bearer authentication, skip CSRF
        # (API clients use tokens directly and aren't vulnerable to CSRF)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return await call_next(request)

        # If no access_token cookie, this isn't a cookie-auth request — skip
        if "access_token" not in request.cookies:
            return await call_next(request)

        # Double-submit CSRF validation
        cookie_csrf = request.cookies.get("csrf_token", "")
        header_csrf = request.headers.get("x-csrf-token", "")

        if not cookie_csrf or not header_csrf:
            logger.warning(f"CSRF token missing on {request.method} {path}")
            return JSONResponse(
                {"detail": "CSRF token missing or invalid"},
                status_code=403,
            )

        if not secrets.compare_digest(cookie_csrf, header_csrf):
            logger.warning(f"CSRF token mismatch on {request.method} {path}")
            return JSONResponse(
                {"detail": "CSRF token missing or invalid"},
                status_code=403,
            )

        return await call_next(request)
