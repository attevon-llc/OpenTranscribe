"""Shared assertion helpers.

Available to every suite without an import dance: ``tests/`` is on ``sys.path`` (the root
conftest puts ``backend/`` there), so ``from tests.helpers import does_not_raise`` works from
any test module.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterator

import pytest
from starlette.requests import Request

#: A globally-routable literal, used as the answer for stubbed documentation hostnames.
PUBLIC_TEST_ADDRESS = "93.184.216.34"


def fake_request(scheme: str = "https") -> Request:
    """A minimal real Starlette ``Request`` carrying just a URL scheme.

    For a unit test that calls an auth endpoint HELPER directly (bypassing the real
    ASGI request cycle) and needs *something* to satisfy a ``request: Request``
    parameter — e.g. ``auth/cookies.py:_secure_for_request``, which reads only
    ``request.url.scheme``. Defaults to ``"https"``, the ordinary case, so a test
    that isn't specifically about the plain-HTTP LAN scenario
    (``ALLOW_INSECURE_COOKIES``) doesn't have to say so.
    """
    scope = {
        "type": "http",
        "scheme": scheme,
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 443 if scheme == "https" else 80),
    }
    return Request(scope)


def stub_public_dns(
    monkeypatch: pytest.MonkeyPatch,
    *,
    domain: str = "example.com",
    address: str = PUBLIC_TEST_ADDRESS,
) -> None:
    """Resolve ``<domain>`` and its subdomains to a public address for one test.

    The SSRF guard on user-supplied hosts (``utils/url_validation``) resolves the name
    and judges every address it answers with, so any suite that stores or fetches a
    hostname now depends on DNS. RFC 2606 reserves ``example.com`` but guarantees
    nothing about ``media.example.com`` — in practice every subdomain NXDOMAINs, so the
    media-source and MediaCMS suites would be refused for "cannot resolve", a reason
    that has nothing to do with what they assert.

    Every **other** name falls through to the real resolver on purpose. Those same
    suites assert that ``internalhost`` and ``redis.internal.svc`` are refused, and a
    blanket stub answering everything would quietly turn those into no-ops; a stub
    answering nothing would break the live Postgres connection the API fixtures need.

    Args:
        monkeypatch: The test's monkeypatch fixture; the patch is undone at teardown.
        domain: The domain (and subdomain suffix) to answer for.
        address: The address to answer with. Must be globally routable, or the guard
            under test will refuse it for a different reason than the test intends.
    """
    real_getaddrinfo = socket.getaddrinfo
    suffix = f".{domain}"

    def fake_getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN202 - socket's signature
        name = str(host).lower().rstrip(".")
        if name == domain or name.endswith(suffix):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def stub_pinned_session(monkeypatch: pytest.MonkeyPatch, target: str, session: object) -> None:
    """Make ``<target>.pinned_requests_session`` yield *session* instead of a real one.

    Callers that fetch a user-supplied URL inline (``protected_media_plugins/mediacms.py``,
    ``llm_service.py``) pin the validated address with ``url_validation.resolve_pinned_target``
    and issue the request through ``url_validation.pinned_requests_session``, which builds a
    real ``requests.Session`` — internally, via its own ``import requests``, so patching the
    module-level ``requests`` name a caller imported (the old mocking strategy here) no longer
    intercepts anything once the call goes through a session. Patching the session factory
    itself keeps the rest of the test (mock a response, assert on `.post`/`.get` call args)
    unchanged; only what supplies the session moves.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        target: Dotted path to the module under test, e.g.
            ``"app.services.protected_media_plugins.mediacms"`` — the same string a
            ``@patch(f"{target}.requests")`` would have used.
        session: The mock (or real) session object to yield. Its ``.post``/``.get`` are what
            the test configures and asserts against.
    """

    @contextlib.contextmanager
    def _fake_pinned_requests_session(_pinned_target: object) -> Iterator[object]:
        yield session

    monkeypatch.setattr(f"{target}.pinned_requests_session", _fake_pinned_requests_session)


@contextlib.contextmanager
def does_not_raise(reason: str) -> Iterator[None]:
    """Assert the block completes without raising, and say why that matters.

    "Calling this must not raise" is a real invariant — containment paths, no-op seams and
    boundary-accepting validators all have it. But written as a bare call with a
    ``# must not raise`` comment it is indistinguishable from an empty test: the comment is
    not executable, and nothing reports *what* was expected when it does raise. Roughly 30
    tests in this suite were in that shape (issue #431).

    This makes the invariant explicit and gives the failure a sentence instead of a bare
    traceback::

        with does_not_raise("5 GB is under the 15 GB ceiling"):
            validate_file_size_for_tenant(5 * GB, None)

    Prefer a stronger assertion when one exists — a return value, a recorded side effect, a
    log line. Use this when "it completed" genuinely IS the contract.

    Args:
        reason: What the caller is asserting, phrased so the failure message reads as a
            sentence. Not optional: "did not raise" without a why is the problem being fixed.

    Raises:
        Failed: via ``pytest.fail``, if the block raises anything.
    """
    if not reason.strip():
        raise ValueError("does_not_raise(reason=...) needs a reason; that is the point of it")
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 - re-reported as a test failure, not swallowed
        pytest.fail(f"{reason} — but it raised {type(exc).__name__}: {exc}")
