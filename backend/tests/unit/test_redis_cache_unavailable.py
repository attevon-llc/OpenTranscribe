"""An unreachable Redis must cost ONE connection attempt, not one per call.

``RedisCacheService.redis`` used to do ``if self._redis is None: <connect>``, and a
failed connect set ``self._redis = None`` — so the failure was never remembered and
every subsequent cache call re-dialled, each time paying redis-py's default
exponential-backoff retry policy.

Measured cost of that (issue #431): one tag merge — 3 creates plus a merge, each
busting several keys — spent **71 of its 87 seconds inside ``time.sleep`` in
``redis/retry.py``**, 200 sleeps, making a three-tag test take 75 s. The same shape
in production means a Redis outage adds several backoff sleeps to every request
that reads or invalidates the cache, so a degraded cache presents as a dead API.

These tests pin the two halves of the fix: the failure is cached for a cooldown,
and the client is built with retries disabled because the cooldown is the retry
policy.
"""

from __future__ import annotations

import app.services.redis_cache_service as cache_module
from app.services.redis_cache_service import RedisCacheService


class _ConnectionRefusedError(Exception):
    """Stand-in for redis.exceptions.ConnectionError."""


def _failing_client_factory(counter: list[int]):
    """A redis.Redis stand-in that counts construction attempts and refuses to ping."""

    class _Client:
        def __init__(self, **_kwargs):
            counter.append(1)

        def ping(self):
            raise _ConnectionRefusedError("connection refused")

    return _Client


def test_a_failed_connection_is_attempted_once_not_once_per_call(monkeypatch):
    """The regression test: 50 cache reads must cost ONE connection attempt."""
    attempts: list[int] = []
    fake_redis_module = type(
        "_RedisModule", (), {"Redis": staticmethod(_failing_client_factory(attempts))}
    )
    monkeypatch.setitem(__import__("sys").modules, "redis", fake_redis_module)

    service = RedisCacheService()
    for _ in range(50):
        assert service.get("cache:tags:1") is None

    assert len(attempts) == 1, (
        f"Redis was dialled {len(attempts)} times for 50 cache reads. The "
        "'unavailable' verdict is not being remembered, so every call pays a "
        "connection attempt plus redis-py's backoff sleeps."
    )


def test_the_cooldown_expires_so_a_recovered_redis_is_picked_up(monkeypatch):
    """The breaker must close again — a permanent circuit would disable caching forever."""
    attempts: list[int] = []
    fake_redis_module = type(
        "_RedisModule", (), {"Redis": staticmethod(_failing_client_factory(attempts))}
    )
    monkeypatch.setitem(__import__("sys").modules, "redis", fake_redis_module)

    clock = [1000.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: clock[0])

    service = RedisCacheService()
    assert service.get("cache:tags:1") is None
    assert len(attempts) == 1

    # Still inside the cooldown: no new attempt.
    clock[0] += cache_module._UNAVAILABLE_COOLDOWN_SECONDS - 1
    assert service.get("cache:tags:1") is None
    assert len(attempts) == 1

    # Past the cooldown: exactly one more attempt.
    clock[0] += 2
    assert service.get("cache:tags:1") is None
    assert len(attempts) == 2


def test_the_client_is_built_with_retries_disabled(monkeypatch):
    """redis-py's own backoff must not stack on top of this service's cooldown."""
    captured: dict = {}

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def ping(self):
            return True

    fake_redis_module = type("_RedisModule", (), {"Redis": staticmethod(_Client)})
    monkeypatch.setitem(__import__("sys").modules, "redis", fake_redis_module)

    assert RedisCacheService().redis is not None
    assert "retry" in captured, "the client must be constructed with an explicit retry policy"
    # One attempt, no sleeping: retries==0 is what keeps a dead Redis cheap.
    assert captured["retry"].get_retries() == 0
