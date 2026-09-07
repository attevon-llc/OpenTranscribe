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
#: Binds an in-flight OIDC login to the browser that started it — see
#: :func:`set_oidc_state_binding`.
OIDC_STATE_COOKIE = "oidc_state_binding"

# Cookie max-age mirrors JWT expiration settings
ACCESS_MAX_AGE = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
REFRESH_MAX_AGE = settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600

# Only set Secure flag when not in dev (allows HTTP cookies in development).
# `ALLOW_INSECURE_COOKIES` is the explicit, narrowly-scoped opt-out for a hardened
# deployment running on plain HTTP -- a LAN-only homelab/small-business install with
# no TLS-terminating reverse proxy in front. It touches only this one attribute;
# every other hardened-mode control (secrets, rate limits, lockout, DEBUG) is
# unaffected. See the setting's docstring in `core/config.py` for the full rationale.
_HARDENED = settings.is_hardened
# Gated on _HARDENED so the flag reads as inert (not just "true but unused") on an
# already-relaxed deployment, where dev/test mode never sets Secure anyway.
_ALLOW_INSECURE = _HARDENED and settings.ALLOW_INSECURE_COOKIES
# The Secure decision when no request is available to check its scheme against
# (there should be none left after `_secure_for_request` below, but this is the
# fallback and it is what the module-level tests exercise directly). Equal to
# `_secure_for_request` in every case EXCEPT "hardened + override active + the
# request is actually HTTPS" -- see that function for why that case needs a
# per-request answer rather than a single process-wide constant.
_SECURE = _HARDENED and not _ALLOW_INSECURE

# The operator-visible warning for _ALLOW_INSECURE lives in
# `main.py:_validate_production_secrets`, not here — this module is imported
# (transitively, via the auth routers) before `configure_logging()` runs, so a
# warning logged at import time here would be the one boot line NOT in JSON under
# LOG_FORMAT=json, and a structured log collector could drop or mangle it.


def _secure_for_request(request: Request) -> bool:
    """Per-response Secure decision — closes the process-wide footgun in `_SECURE`.

    `ALLOW_INSECURE_COOKIES` used to strip `Secure` from EVERY cookie the process
    ever set, including one served over genuine HTTPS on the SAME process — e.g. a
    LAN deployment later put behind a TLS reverse proxy, or a deployment reachable
    over both a plain-HTTP LAN port and an HTTPS one. That loses `Secure`
    protection on the very connection that never needed the override at all, and a
    browser then attaches the resulting non-`Secure` cookie to a plain-HTTP request
    to the same host too — exactly what `Secure` exists to prevent.

    `Dockerfile.prod` runs uvicorn with `--proxy-headers --forwarded-allow-ips
    <RFC1918 ranges>`, so `request.url.scheme` already reflects the real
    client-facing scheme: `https` when a configured, trusted reverse proxy vouches
    for it via `X-Forwarded-Proto`, the raw connection scheme otherwise. No new
    trust boundary is introduced here — this reads a value Starlette already
    resolved through the existing trusted-proxy configuration.

    Net effect: Secure on an actually-HTTPS request even with the override on;
    insecure only on an actually-plain-HTTP one — exactly the LAN case the
    override exists for, and nothing broader.
    """
    if not _HARDENED:
        return False
    if not _ALLOW_INSECURE:
        return True
    return bool(request.url.scheme == "https")


def set_auth_cookies(
    response: Response, access_token: str, refresh_token: str, request: Request
) -> None:
    """Set authentication cookies on a response.

    Sets httpOnly access_token and refresh_token cookies plus a
    non-httpOnly csrf_token cookie for double-submit CSRF protection.

    ``request`` is required, not optional: it is what lets
    :func:`_secure_for_request` tell an actually-HTTPS request apart from an
    actually-plain-HTTP one when ``ALLOW_INSECURE_COOKIES`` is active. A caller
    with no request in scope is a sign something upstream should be threading one
    through, not a case to silently default away.
    """
    secure = _secure_for_request(request)
    csrf = secrets.token_hex(32)

    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=ACCESS_MAX_AGE,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        httponly=True,
        secure=secure,
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
        secure=secure,
        samesite="lax",
        # Must outlive the ACCESS cookie, not match it. The double-submit token
        # is what authorises a token refresh, and a refresh happens precisely
        # when the access cookie has already lapsed — pinning it to
        # ACCESS_MAX_AGE meant every idle session lost the ability to refresh
        # and got logged out instead.
        max_age=REFRESH_MAX_AGE,
        path="/",
    )


def set_oidc_state_binding(response: Response, secret: str, max_age: int, request: Request) -> None:
    """Bind an in-flight OIDC login to the browser that started it.

    The `state` parameter is unguessable and single-use, which stops replay — but
    it is carried in a URL, so it does not prove the callback arrived in the SAME
    browser that began the flow. Without this an attacker can start a login,
    capture their own callback URL, and get a victim to visit it, silently signing
    the victim into the ATTACKER's account (login CSRF / session fixation).
    Anything the victim then uploads or types lands in an account the attacker
    controls.

    This cookie is the second half: the callback must present it, and it never
    leaves the browser it was set in.

    ``samesite="lax"`` is required, not a compromise — the callback is a
    top-level cross-site GET navigation from the provider, and ``strict`` would
    withhold the cookie and break every login.
    """
    response.set_cookie(
        OIDC_STATE_COOKIE,
        secret,
        httponly=True,
        secure=_secure_for_request(request),
        samesite="lax",
        max_age=max_age,
        path="/api/auth",
    )


def clear_oidc_state_binding(response: Response) -> None:
    """Drop the binding cookie once the flow completes or fails."""
    response.delete_cookie(OIDC_STATE_COOKIE, path="/api/auth")


def get_oidc_state_binding(request: Request) -> str | None:
    """Read the binding secret presented by the browser at the callback."""
    binding: str | None = request.cookies.get(OIDC_STATE_COOKIE)
    return binding


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
