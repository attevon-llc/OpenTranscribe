"""The auth-cookie ``Secure`` flag and its one documented opt-out.

``app/auth/cookies.py`` computes ``_SECURE`` once at import time from
``settings.is_hardened``. A hardened deployment (the default — ``ENVIRONMENT``
defaults to ``production``) sets ``Secure`` on every auth cookie, which is correct
behind real TLS but is silently dropped by the browser on plain HTTP to anything
other than localhost/127.0.0.1 — so a homelab/small-business deployment reached over
a LAN IP (e.g. ``http://10.10.10.20:5173``) got a login that appeared to succeed and
then could never hold a session, indistinguishable from a wrong password.

``ALLOW_INSECURE_COOKIES`` (default ``false``) is the narrow, explicit opt-out for
that one case. An adversarial review of the first version of this fix found that
``_SECURE`` being a single process-wide constant meant the override stripped
``Secure`` from EVERY cookie the process ever set — including one served over
genuine HTTPS on the same process (e.g. a LAN deployment later put behind a TLS
reverse proxy). ``_secure_for_request`` closes that: with the override active, it
checks the actual request's scheme (which already reflects a trusted reverse
proxy's ``X-Forwarded-Proto`` via uvicorn's ``--proxy-headers``, per
``Dockerfile.prod``) and sets ``Secure`` on an actually-HTTPS request regardless.

This module pins: the module-level baseline decision (``_SECURE``, used only when
no request is available), the per-request decision used by every real cookie set
(``_secure_for_request``, exercised through ``set_auth_cookies`` /
``set_oidc_state_binding``), that ``Secure`` is genuinely present by default (not
just absent under the override — a gap the first version of this test file left
open), and that the warning about the override fires from the right place at
startup (``tests/test_production_secrets_guard.py``, not here — see that file for
why the warning was moved out of this module).
"""

from __future__ import annotations

import importlib

import pytest
from starlette.requests import Request


def _request(scheme: str) -> Request:
    """A minimal real Starlette ``Request`` with a given URL scheme.

    Only ``request.url.scheme`` is read by the code under test, but building a
    real ``Request`` (rather than a stub with a ``.url.scheme`` attribute) means
    this exercises Starlette's own scheme resolution rather than a test's guess
    at its shape.
    """
    scope = {
        "type": "http",
        "scheme": scheme,
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80 if scheme == "http" else 443),
    }
    return Request(scope)


def _reload_cookies_with(monkeypatch: pytest.MonkeyPatch, *, hardened: bool, allow_insecure: bool):
    """Reload ``app.auth.cookies`` under a specific hardened/override combination.

    ``_SECURE`` is a module-level constant computed at import time (like
    ``app.utils.client_ip``'s trusted-proxy list), so exercising a different
    settings combination means reloading the module — monkeypatching
    ``settings`` alone would not re-run the module body.
    """
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "is_hardened", property(lambda self: hardened))
    monkeypatch.setattr(settings, "ALLOW_INSECURE_COOKIES", allow_insecure)

    import app.auth.cookies as cookies

    return importlib.reload(cookies)


@pytest.fixture(autouse=True)
def _restore_real_cookies_module():
    """Leave the module reflecting real settings for every other test in the suite."""
    yield
    import app.auth.cookies as cookies

    importlib.reload(cookies)


def test_hardened_deployment_defaults_to_secure_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    cookies = _reload_cookies_with(monkeypatch, hardened=True, allow_insecure=False)
    assert cookies._SECURE is True


def test_allow_insecure_cookies_disables_secure_on_a_hardened_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one legitimate use: a homelab/small-business LAN deployment with no TLS proxy."""
    cookies = _reload_cookies_with(monkeypatch, hardened=True, allow_insecure=True)
    assert cookies._SECURE is False


@pytest.mark.parametrize(
    "allow_insecure",
    [False, True],
    ids=["override-off", "override-on-but-already-relaxed"],
)
def test_non_hardened_deployment_never_sets_secure(
    monkeypatch: pytest.MonkeyPatch, allow_insecure: bool
) -> None:
    """A dev/relaxed environment already allows plain-HTTP cookies either way."""
    cookies = _reload_cookies_with(monkeypatch, hardened=False, allow_insecure=allow_insecure)
    assert cookies._SECURE is False


def test_hardened_default_actually_sets_secure_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not just ``_SECURE is True`` — the real ``Set-Cookie`` headers must carry it.

    Deleting ``secure=_SECURE`` from every ``set_cookie`` call would still pass the
    ``_SECURE is True`` assertion above; only reading the actual response headers
    catches that. This is the hardened-without-override default every real
    deployment gets unless it explicitly opts out.
    """
    from starlette.responses import Response

    cookies = _reload_cookies_with(monkeypatch, hardened=True, allow_insecure=False)

    response = Response()
    cookies.set_auth_cookies(
        response, access_token="at", refresh_token="rt", request=_request("https")
    )
    cookies.set_oidc_state_binding(response, secret="s", max_age=60, request=_request("https"))

    set_cookie_headers = response.headers.getlist("set-cookie")
    assert len(set_cookie_headers) == 4
    for header in set_cookie_headers:
        assert "secure" in header.lower(), header


def test_the_override_strips_secure_only_on_an_actually_plain_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards against a future refactor that reads ``_SECURE`` for the access cookie
    but recomputes the flag (or a stale copy of it) for refresh/CSRF/OIDC-state."""
    from starlette.responses import Response

    cookies = _reload_cookies_with(monkeypatch, hardened=True, allow_insecure=True)

    response = Response()
    cookies.set_auth_cookies(
        response, access_token="at", refresh_token="rt", request=_request("http")
    )
    cookies.set_oidc_state_binding(response, secret="s", max_age=60, request=_request("http"))

    set_cookie_headers = response.headers.getlist("set-cookie")
    # 4 cookies: access, refresh, csrf, oidc-state binding.
    assert len(set_cookie_headers) == 4
    for header in set_cookie_headers:
        assert "secure" not in header.lower(), header


def test_the_override_does_not_strip_secure_from_an_actually_https_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression an adversarial review found in the first version of this fix.

    ``ALLOW_INSECURE_COOKIES`` is meant for the plain-HTTP LAN case, but the first
    implementation made ``_SECURE`` a single process-wide constant — so turning the
    override on ALSO stripped ``Secure`` from a request that was genuinely served
    over HTTPS on the same process (e.g. a LAN deployment later put behind a TLS
    reverse proxy, or one reachable over both a plain-HTTP LAN port and an HTTPS
    one). That loses real protection on a connection the override was never meant
    to touch, and a browser then attaches the resulting non-``Secure`` cookie to a
    plain-HTTP request too. The fix checks the actual request's scheme.
    """
    from starlette.responses import Response

    cookies = _reload_cookies_with(monkeypatch, hardened=True, allow_insecure=True)

    response = Response()
    cookies.set_auth_cookies(
        response, access_token="at", refresh_token="rt", request=_request("https")
    )
    cookies.set_oidc_state_binding(response, secret="s", max_age=60, request=_request("https"))

    set_cookie_headers = response.headers.getlist("set-cookie")
    assert len(set_cookie_headers) == 4
    for header in set_cookie_headers:
        assert "secure" in header.lower(), header
