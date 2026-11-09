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

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

from app.core.cookie_names import ACCESS_COOKIE
from app.core.cookie_names import CSRF_COOKIE
from app.core.cookie_names import REFRESH_COOKIE

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
    "/api/auth/oidc/",
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


def _csrf_failure(request: Request, path: str, reason: str) -> JSONResponse:
    logger.warning(f"CSRF token {reason} on {request.method} {path}")
    return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)


class CSRFMiddleware:
    """Pure ASGI: validates the CSRF double-submit token on non-safe, cookie-authenticated requests.

    Only headers and cookies are read (via ``Request(scope)``); the body is never consumed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        rejection = self._check(request)
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _check(request: Request) -> JSONResponse | None:
        """The 403 to send, or None when the request may proceed."""
        # Safe methods never need CSRF
        if request.method in _SAFE_METHODS:
            return None

        path = request.url.path

        # Exempt paths (login, register, etc.)
        if _is_exempt(path):
            return None

        # WebSocket paths
        if any(path.startswith(p) for p in _WS_PREFIXES):
            return None

        # Bearer authentication: API clients use tokens directly, not vulnerable to CSRF
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return None

        # No auth cookie of any kind: a forged request has nothing to ride on.
        if not any(name in request.cookies for name in _AUTH_COOKIES):
            return None

        # Double-submit CSRF validation. The csrf_token cookie is minted with the
        # REFRESH token's lifetime (app/api/cookies.py), so an idle session still
        # has one to submit when its access cookie has lapsed — no carve-out needed.
        cookie_csrf = request.cookies.get(CSRF_COOKIE, "")
        header_csrf = request.headers.get("x-csrf-token", "")

        if not cookie_csrf or not header_csrf:
            return _csrf_failure(request, path, "missing")

        if not secrets.compare_digest(cookie_csrf, header_csrf):
            return _csrf_failure(request, path, "mismatch")

        return None
