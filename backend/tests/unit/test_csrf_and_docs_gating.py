"""CSRF exemption scope, cookie gating, and hardened-mode API docs gating.

Three defects pinned here:

1. ``_EXEMPT_PREFIXES`` carried the blanket prefix ``/api/auth/mfa/``. That covers the
   pre-auth ``/mfa/verify``, but it also exempted ``/mfa/setup`` — which overwrites
   ``totp_secret``, clears ``totp_enabled`` and wipes ``backup_codes`` with no code
   required — plus ``/mfa/verify-setup`` and ``/mfa/disable``. All three are
   cookie-authenticated, so a cross-origin page could strip a victim's MFA enrollment.
   Same shape for ``/api/auth/token``, whose prefix also exempted
   ``/api/auth/token/refresh``.

2. The middleware gated on ``"access_token" not in request.cookies``. The access cookie
   lives 60 minutes and the refresh cookie 7 days, so "only the refresh cookie is
   present" is a routine state in which every cookie-authenticated mutating request was
   unprotected.

3. ``FastAPI(...)`` published ``openapi_url``/``docs_url``/``redoc_url`` unconditionally,
   so Swagger UI enumerated the whole admin/auth surface anonymously in production.
   The gate is ``settings.is_hardened`` — never ``ENVIRONMENT == "production"``.

Pure unit tests: the middleware is exercised against a throwaway Starlette app, so
nothing here touches Postgres, Redis, MinIO, or OpenSearch.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.csrf import CSRFMiddleware
from app.middleware.csrf import _is_exempt

_CSRF = "a" * 64

# Every non-safe path the middleware is asked about below.
_ROUTES = (
    "/api/files",
    "/api/auth/token/refresh",
    "/api/auth/mfa/setup",
    "/api/auth/mfa/verify",
)


@pytest.fixture
def csrf_client() -> TestClient:
    """A minimal app wrapped in CSRFMiddleware — no DB, no auth, no routers."""

    async def ok(request):  # noqa: ANN001, ANN202 - trivial test endpoint
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route(path, ok, methods=["POST"]) for path in _ROUTES])
    app.add_middleware(CSRFMiddleware)
    return TestClient(app)


# ── Exemption table ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/mfa/setup",  # resets the TOTP secret + backup codes
        "/api/auth/mfa/verify-setup",
        "/api/auth/mfa/disable",
        "/api/auth/token/refresh",  # rotates tokens from the refresh cookie
        "/api/auth/logout",
        "/api/files",
        "/api/admin/users",
    ],
)
def test_cookie_authenticated_paths_are_not_exempt(path):
    assert _is_exempt(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/mfa/verify",  # pre-auth: holds an MFA token, not a session
        "/api/auth/login",
        "/api/auth/token",
        "/api/auth/register",
        "/api/auth/password-reset/request",
        "/api/auth/password-reset/confirm",
        "/api/auth/oidc/callback",
        "/api/auth/pki/authenticate",
        "/api/webhooks/anything",
        "/health",
    ],
)
def test_pre_auth_paths_stay_exempt(path):
    assert _is_exempt(path) is True


def test_mfa_verify_exemption_does_not_leak_to_its_siblings():
    """The regression in one assertion: exact path, not prefix."""
    assert _is_exempt("/api/auth/mfa/verify") is True
    assert _is_exempt("/api/auth/mfa/verify-setup") is False


def test_token_exemption_does_not_leak_to_refresh():
    assert _is_exempt("/api/auth/token") is True
    assert _is_exempt("/api/auth/token/refresh") is False


# ── Cookie gating in the middleware ──────────────────────────────────────────────


def test_refresh_cookie_alone_is_not_skipped(csrf_client):
    """The core fix: an expired access cookie must not disable CSRF."""
    resp = csrf_client.post("/api/auth/token/refresh", cookies={"refresh_token": "r"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "CSRF token missing or invalid"


def test_refresh_cookie_with_valid_double_submit_passes(csrf_client):
    resp = csrf_client.post(
        "/api/auth/token/refresh",
        cookies={"refresh_token": "r", "csrf_token": _CSRF},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert resp.status_code == 200


def test_refresh_cookie_with_mismatched_token_is_rejected(csrf_client):
    resp = csrf_client.post(
        "/api/auth/token/refresh",
        cookies={"refresh_token": "r", "csrf_token": _CSRF},
        headers={"X-CSRF-Token": "b" * 64},
    )
    assert resp.status_code == 403


def test_access_cookie_alone_still_requires_the_header(csrf_client):
    resp = csrf_client.post("/api/files", cookies={"access_token": "a"})
    assert resp.status_code == 403


def test_no_auth_cookie_is_not_a_cookie_session(csrf_client):
    """A request the browser attaches no credentials to has nothing to forge."""
    resp = csrf_client.post("/api/files")
    assert resp.status_code == 200


def test_bearer_requests_remain_exempt(csrf_client):
    resp = csrf_client.post(
        "/api/files",
        cookies={"access_token": "a"},
        headers={"Authorization": "Bearer whatever"},
    )
    assert resp.status_code == 200


def test_mfa_setup_is_protected_but_mfa_verify_is_not(csrf_client):
    """The CSRF-strips-your-MFA scenario, end to end through the middleware."""
    forged = csrf_client.post("/api/auth/mfa/setup", cookies={"access_token": "a"})
    assert forged.status_code == 403

    pre_auth = csrf_client.post("/api/auth/mfa/verify", cookies={"access_token": "a"})
    assert pre_auth.status_code == 200


# ── Idle sessions still carry a CSRF token ───────────────────────────────────────
#
# The csrf_token cookie used to be minted with the ACCESS token's lifetime while the
# refresh cookie lived for days, so a session returning after an idle period had no
# token left to double-submit — and a refresh is exactly the request an idle session
# makes. It now carries the REFRESH lifetime, so the double-submit check applies
# uniformly and needs no same-origin carve-out.


def test_csrf_cookie_outlives_the_access_cookie():
    """Regression: pinning the CSRF cookie to ACCESS_MAX_AGE broke idle refresh."""
    from starlette.responses import Response

    from app.auth.cookies import ACCESS_MAX_AGE
    from app.auth.cookies import CSRF_COOKIE
    from app.auth.cookies import REFRESH_MAX_AGE
    from app.auth.cookies import set_auth_cookies

    response = Response()
    set_auth_cookies(response, "access", "refresh")
    csrf_header = next(
        value.decode()
        for key, value in response.raw_headers
        if key == b"set-cookie" and value.decode().startswith(f"{CSRF_COOKIE}=")
    )

    assert f"Max-Age={REFRESH_MAX_AGE}" in csrf_header
    assert REFRESH_MAX_AGE > ACCESS_MAX_AGE


def test_refresh_cookie_is_samesite_strict():
    """It is only ever used by same-site XHR, so it never needs Lax."""
    from starlette.responses import Response

    from app.auth.cookies import REFRESH_COOKIE
    from app.auth.cookies import set_auth_cookies

    response = Response()
    set_auth_cookies(response, "access", "refresh")
    refresh_header = next(
        value.decode()
        for key, value in response.raw_headers
        if key == b"set-cookie" and value.decode().startswith(f"{REFRESH_COOKIE}=")
    )

    assert "samesite=strict" in refresh_header.lower()


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-origin"},
        {"Origin": "http://testserver", "Host": "testserver"},
        {"Sec-Fetch-Site": "cross-site"},
        {},  # no evidence at all
    ],
)
def test_missing_csrf_cookie_is_always_rejected(csrf_client, headers):
    """No same-origin carve-out: a cookie-authenticated write needs the token."""
    resp = csrf_client.post(
        "/api/auth/token/refresh",
        cookies={"refresh_token": "r"},
        headers=headers,
    )
    assert resp.status_code == 403


def test_matching_double_submit_token_is_accepted(csrf_client):
    resp = csrf_client.post(
        "/api/auth/token/refresh",
        cookies={"refresh_token": "r", "csrf_token": _CSRF},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert resp.status_code == 200


# ── OpenAPI / Swagger / ReDoc gating ─────────────────────────────────────────────


def test_docs_urls_are_none_when_hardened(monkeypatch):
    from app.core.config import settings
    from app.main import _resolve_docs_urls

    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert settings.is_hardened is True

    assert _resolve_docs_urls() == (None, None, None)


def test_unset_environment_also_hides_the_docs(monkeypatch):
    """Fail-closed: a blank/misspelled ENVIRONMENT must not publish the schema."""
    from app.core.config import settings
    from app.main import _resolve_docs_urls

    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "")

    assert _resolve_docs_urls() == (None, None, None)


@pytest.mark.parametrize("environment", ["development", "testing", "local"])
def test_docs_urls_are_served_when_not_hardened(monkeypatch, environment):
    from app.core.config import settings
    from app.main import _resolve_docs_urls

    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    assert settings.is_hardened is False

    openapi_url, docs_url, redoc_url = _resolve_docs_urls()
    assert openapi_url == f"{settings.API_PREFIX}/openapi.json"
    assert docs_url == f"{settings.API_PREFIX}/docs"
    assert redoc_url == f"{settings.API_PREFIX}/redoc"


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_enable_api_docs_is_the_explicit_escape_hatch(monkeypatch, value):
    from app.core.config import settings
    from app.main import _resolve_docs_urls

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setenv("ENABLE_API_DOCS", value)

    assert _resolve_docs_urls() == (
        f"{settings.API_PREFIX}/openapi.json",
        f"{settings.API_PREFIX}/docs",
        f"{settings.API_PREFIX}/redoc",
    )


@pytest.mark.parametrize("value", ["", "false", "0", "no", "maybe"])
def test_non_affirmative_opt_in_values_keep_the_docs_closed(monkeypatch, value):
    from app.core.config import settings
    from app.main import _resolve_docs_urls

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setenv("ENABLE_API_DOCS", value)

    assert _resolve_docs_urls() == (None, None, None)


def test_live_app_is_wired_to_the_gate():
    """Guards against the URLs drifting back to hard-coded literals."""
    from app.main import _resolve_docs_urls
    from app.main import app

    openapi_url, docs_url, redoc_url = _resolve_docs_urls()
    assert (app.openapi_url, app.docs_url, app.redoc_url) == (openapi_url, docs_url, redoc_url)
