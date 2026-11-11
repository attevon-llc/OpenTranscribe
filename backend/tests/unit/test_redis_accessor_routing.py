"""``route_redis_accessors`` must redirect EVERY synchronous ``app.core.redis`` accessor.

The live broker suites point this process at a throwaway Redis database. They used to patch
``get_redis`` alone; 1fe7bf595 moved the lease, replay and queue-depth reads onto
``get_request_redis()``, so those reads went to db 0 while the test wrote db 15, and eight
live tests failed or timed out waiting for a lease that lapsed in the wrong database. The
helper derives the accessor list from the module, so these tests pin both the derivation
(it finds the accessors that exist today and skips the async one) and the effect.
"""

from __future__ import annotations

import pytest

from app.core import redis as redis_core
from tests.fixtures.redis_accessors import route_redis_accessors
from tests.fixtures.redis_accessors import sync_redis_accessor_names


@pytest.mark.unit
def test_the_derivation_finds_every_sync_accessor_and_not_the_async_one() -> None:
    names = sync_redis_accessor_names()
    assert {"get_redis", "get_request_redis", "get_probe_redis"} <= set(names), names
    assert "get_async_redis" not in names


@pytest.mark.unit
def test_every_sync_accessor_returns_the_routed_client(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = sync_redis_accessor_names()
    sentinel = object()
    routed = route_redis_accessors(monkeypatch, sentinel)
    assert routed == expected
    for name in routed:
        assert getattr(redis_core, name)() is sentinel, f"{name} still reaches the real Redis"
