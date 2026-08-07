"""httpOnly cookie helpers for secure token storage.

Tokens are set as httpOnly cookies to prevent XSS access. A separate
non-httpOnly CSRF cookie is used for double-submit CSRF protection.
"""

import secrets

from fastapi import Request
from starlette.responses import Response

from app.core.config import settings

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"

# Cookie max-age mirrors JWT expiration settings
ACCESS_MAX_AGE = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
REFRESH_MAX_AGE = settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600

# Only set Secure flag when not in dev (allows HTTP cookies in development)
_IS_DEV = not settings.is_hardened
_SECURE = not _IS_DEV


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """Set authentication cookies on a response.

    Sets httpOnly access_token and refresh_token cookies plus a
    non-httpOnly csrf_token cookie for double-submit CSRF protection.
    """
    csrf = secrets.token_hex(32)

    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        httponly=True,
        secure=_SECURE,
        samesite="lax",
        max_age=ACCESS_MAX_AGE,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        httponly=True,
        secure=_SECURE,
        # Strict, not Lax: this cookie is only ever used by same-site XHR to
        # /api/auth/token/refresh, so it never needs to survive a cross-site
        # top-level navigation — and not sending it on one removes the CSRF
        # surface for the single most powerful cookie in the app.
        samesite="strict",
        max_age=REFRESH_MAX_AGE,
        path="/api/auth",  # Only sent to auth endpoints
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,  # Readable by JavaScript for double-submit pattern
        secure=_SECURE,
        samesite="lax",
        # Must outlive the ACCESS cookie, not match it. The double-submit token
        # is what authorises a token refresh, and a refresh happens precisely
        # when the access cookie has already lapsed — pinning it to
        # ACCESS_MAX_AGE meant every idle session lost the ability to refresh
        # and got logged out instead.
        max_age=REFRESH_MAX_AGE,
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    """Remove all authentication cookies."""
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    response.delete_cookie(CSRF_COOKIE, path="/")


def get_access_token_from_cookie(request: Request) -> str | None:
    """Read the access token from the httpOnly cookie."""
    token: str | None = request.cookies.get(ACCESS_COOKIE)
    return token


def get_refresh_token_from_cookie(request: Request) -> str | None:
    """Read the refresh token from the httpOnly cookie."""
    token: str | None = request.cookies.get(REFRESH_COOKIE)
    return token
