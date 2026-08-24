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

#: A globally-routable literal, used as the answer for stubbed documentation hostnames.
PUBLIC_TEST_ADDRESS = "93.184.216.34"


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
