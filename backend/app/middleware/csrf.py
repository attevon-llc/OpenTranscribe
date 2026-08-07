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

from app.auth.cookies import ACCESS_COOKIE
from app.auth.cookies import CSRF_COOKIE
from app.auth.cookies import REFRESH_COOKIE

logger = logging.getLogger(__name__)

# Methods that don't modify state — no CSRF check needed
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Cookies the browser attaches on its own, which is exactly what a forged
# cross-site request rides on. The refresh cookie outlives the access cookie by
# days, so a session with only the refresh cookie is a routine state, not an
# anonymous one.
_AUTH_COOKIES = (ACCESS_COOKIE, REFRESH_COOKIE)

# Paths that must be exempt from CSRF (they can't send a CSRF cookie yet).
# Prefixes only where EVERY sub-path is genuinely pre-authentication.
_EXEMPT_PREFIXES = (
    "/api/auth/password-reset/",
    "/api/auth/keycloak/",
    "/api/auth/pki/",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/health",
    # Server-to-server webhooks (cloud edition: managed IdP / billing). They carry no
    # cookies and authenticate by cryptographic signature on the raw body —
    # CSRF does not apply and would silently 403 every delivery.
    "/api/webhooks/",
)

# Exact paths, deliberately NOT prefixes: each of these sits directly above
# cookie-authenticated siblings that must stay protected.
#   "/api/auth/token"      also covered POST /api/auth/token/refresh, which mints
#                          new tokens from the refresh cookie alone.
#   "/api/auth/mfa/"       also covered /mfa/setup (overwrites the TOTP secret and
#                          wipes backup codes with no code required), /mfa/verify-setup
#                          and /mfa/disable — all cookie-authenticated.
_EXEMPT_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/token",
        "/api/auth/register",
        "/api/auth/mfa/verify",
    }
)

# WebSocket paths are upgraded before middleware runs but check just in case
_WS_PREFIXES = ("/api/ws",)


def _is_exempt(path: str) -> bool:
    """Whether a request path skips CSRF validation entirely.

    Args:
        path: Request path, e.g. ``/api/auth/mfa/setup``.

    Returns:
        True for pre-authentication, webhook, and docs paths.
    """
    normalized = path.rstrip("/") or "/"
    return normalized in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Validates CSRF double-submit token on non-safe, cookie-authenticated requests."""

    async def dispatch(self, request: Request, call_next):  # noqa: ANN201
        # Safe methods never need CSRF
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        path = request.url.path

        # Exempt paths (login, register, etc.)
        if _is_exempt(path):
            return await call_next(request)

        # WebSocket paths
        if any(path.startswith(p) for p in _WS_PREFIXES):
            return await call_next(request)

        # If the request uses Bearer authentication, skip CSRF
        # (API clients use tokens directly and aren't vulnerable to CSRF)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return await call_next(request)

        # No auth cookie of any kind — the browser attaches no credentials, so a
        # forged request has nothing to ride on.
        if not any(name in request.cookies for name in _AUTH_COOKIES):
            return await call_next(request)

        # Double-submit CSRF validation. The csrf_token cookie is minted with the
        # REFRESH token's lifetime (app/auth/cookies.py), so an idle session still
        # has one to submit when its access cookie has lapsed — no carve-out needed.
        cookie_csrf = request.cookies.get(CSRF_COOKIE, "")
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
