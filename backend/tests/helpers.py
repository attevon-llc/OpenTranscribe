"""Shared assertion helpers.

Available to every suite without an import dance: ``tests/`` is on ``sys.path`` (the root
conftest puts ``backend/`` there), so ``from tests.helpers import does_not_raise`` works from
any test module.
"""

from __future__ import annotations

import contextlib
import socket
import types
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.exceptions import DomainError
from app.models.media import MediaFile

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


def caller_access_gate(user_id: int) -> dict[str, Any]:
    """The personal-scope OpenSearch read gate for ``user_id``, written out literally.

    ``accessible_user_ids`` AND NOT ``availability`` in (trashed, quarantined): the shape
    ``services/search/access_filter.access_filter_clause`` emits with no organization and
    no quarantine review exemption. Spelled out here rather than built by calling the
    product function, so a test asserting the gate is present cannot pass merely because
    the product and the expectation share one (possibly wrong) builder.
    """
    return {
        "bool": {
            "filter": [{"terms": {"accessible_user_ids": [user_id]}}],
            "must_not": [{"terms": {"availability": ["trashed", "quarantined"]}}],
        }
    }


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

    def fake_getaddrinfo(host, port, *args, **kwargs):  # socket's signature
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


def make_stub_module(name: str, **attributes: object) -> types.ModuleType:
    """A module object standing in for an optional dependency the test does not install.

    The attributes are whatever the test supplies, so they are set from kwargs: mypy cannot
    know the surface of a module that is not installed, and a bare ``types.ModuleType`` has
    no declared attributes to assign to.
    """
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


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
    except BaseException as exc:  # re-reported as a test failure, not swallowed
        pytest.fail(f"{reason} — but it raised {type(exc).__name__}: {exc}")


def reset_rate_limiter() -> None:
    """Clear EVERY counter the module-level slowapi ``limiter`` holds.

    ``Limiter.reset()`` only resets the primary storage (Redis). The limiter is built with
    ``in_memory_fallback_enabled`` (see ``app.auth.rate_limit._create_limiter``), so when
    Redis is unreachable — always the case on CI and on most host runs — every request is
    counted in a separate in-memory fallback storage that ``reset()`` never touches. Counters
    from one test then eat into the next test's budget on the same xdist worker (#1131/#1133
    flake). slowapi exposes no public API for the fallback, hence the private attributes;
    they are read defensively so a slowapi upgrade degrades to the old behaviour instead of
    breaking every rate-limit test.
    """
    from app.auth.rate_limit import limiter

    try:
        limiter.reset()  # primary storage; raises when Redis is unreachable
    except Exception:  # unreachable Redis is the normal case here
        pass
    fallback_storage = getattr(limiter, "_fallback_storage", None)
    if fallback_storage is not None:
        fallback_storage.reset()
    # Start the next test on the primary storage again rather than inheriting "dead".
    if hasattr(limiter, "_storage_dead"):
        limiter._storage_dead = False


def detail_dict(exc: HTTPException | DomainError) -> dict[str, Any]:
    """The structured ``detail`` payload of an ``HTTPException`` or ``DomainError`` raised with a dict.

    Starlette types ``HTTPException.detail`` as ``str``, but the app raises these with
    ``{"code": ..., "message": ...}`` dicts (FastAPI serialises them as JSON). Narrowing
    here keeps the test's own assertion honest: a handler that regressed to a plain string
    fails this ``isinstance`` check instead of an obscure ``TypeError`` on the index.
    """
    detail: object = exc.detail
    assert isinstance(detail, dict), f"expected a dict detail, got {type(detail).__name__}"
    return detail


def reset_search_infrastructure_state() -> None:
    """Forget which chunks index and fusion pipelines this process has verified.

    The verification cache is per process and nothing in app code needs to drop it (an
    index recreation leaves the index present, and pipelines are cluster-level), so only
    tests that repoint index names or stub the existence checks reset it.
    """
    from app.services.search import search_infra

    with search_infra._state_lock:
        search_infra._index_verified = False
        search_infra._verified_pipelines.clear()


def media_file_count(db: Session, user_id: int) -> int:
    """How many media files ``user_id`` owns.

    Use this, not a table-wide ``query(MediaFile).count()``, to assert that a request created
    no row: other xdist workers commit and delete media files in the same database, so a global
    count taken before and after a request can move without the request doing anything.
    """
    return db.query(MediaFile).filter(MediaFile.user_id == user_id).count()


def set_youtube_policy(monkeypatch, **fields):
    """Pin the effective YouTube ingestion policy for one test (the stored policy is DB-backed)."""
    from dataclasses import replace

    from app.services import youtube_policy

    policy = replace(youtube_policy.DEFAULT_POLICY, **fields)
    monkeypatch.setattr(youtube_policy, "current_youtube_policy", lambda: policy)


class StoreBackedTaskLocks:
    """A ``task_lock_manager`` stand-in whose locks live in a dict-backed fake Redis store.

    For suites that drive a task against an in-process fake Redis: the real manager takes a
    redis-py ``Lock`` (Lua scripts), which no dict fake can run. Same contract as
    ``TaskLockManager.acquire_lock``: NX on entry, yields whether it holds the key, and
    releases only a lock it took.
    """

    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    @contextlib.contextmanager
    def acquire_lock(self, lock_key: str, timeout: int = 300, blocking_timeout: int = 0):
        if lock_key in self.store:
            yield False
            return
        self.store[lock_key] = "held"
        try:
            yield True
        finally:
            self.store.pop(lock_key, None)

    def is_locked(self, lock_key: str) -> bool:
        return lock_key in self.store
