"""Shared-state loss must be observable, recoverable, and never use ``KEYS``.

Three defects, all on unauthenticated paths:

* ``session.OIDCStateStore._count_states`` ran ``KEYS oidc:state:*`` on every OIDC
  login. ``KEYS`` is O(whole keyspace) and blocks the Redis event loop; Redis here is
  also the Celery broker and the cache, so an anonymous caller could stall the entire
  instance by hitting the login route. ``_cleanup_oldest_states`` still did the same,
  and it runs precisely when the store is at its cap — i.e. under attack.
* ``session._get_store`` and ``rate_limit._create_limiter`` fell back to per-process
  state and never came back. The rate limiter's choice was made once at import and
  frozen into every ``@limiter.limit`` decorator, so a Redis blip at startup left the
  replica counting in its own memory forever: N replicas meant N x the configured
  auth rate limit.
* Neither emitted a metric, so a deployment running with no shared auth state looked
  healthy.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app.auth import rate_limit as rate_limit_module
from app.auth import session as session_module


class _FakeRequest:
    """The two attributes ``resolve_client_ip`` reads."""

    client = type("_Client", (), {"host": "10.1.2.3"})()
    headers: dict[str, str] = {}


class _RecordingRedis:
    """Enough of a Redis client to observe which scan primitive is used."""

    def __init__(self, keys):
        self._keys = list(keys)
        self.keys_calls = 0
        self.scan_calls = 0
        self.deleted: list[str] = []

    def keys(self, _pattern):
        self.keys_calls += 1
        return list(self._keys)

    def scan_iter(self, match=None, count=None):  # noqa: ARG002 - mirrors redis-py
        self.scan_calls += 1
        yield from list(self._keys)

    def delete(self, key):
        self.deleted.append(key)
        return 1

    def get(self, _key):
        return None

    def set(self, *_a, **_k):
        return True


@pytest.mark.unit
class TestOidcStateStoreDoesNotUseKeys:
    def test_counting_uses_scan(self):
        store = session_module.OIDCStateStore(max_states=10)
        fake = _RecordingRedis([f"oidc:state:{i}" for i in range(3)])
        store._store = fake

        assert store._count_states() == 3
        assert fake.scan_calls == 1
        assert fake.keys_calls == 0, "KEYS blocks the Redis event loop"

    def test_counting_stops_one_past_the_cap(self):
        """The cap is a yes/no question; counting the whole keyspace is wasted work."""
        store = session_module.OIDCStateStore(max_states=5)
        store._store = _RecordingRedis([f"oidc:state:{i}" for i in range(500)])

        assert store._count_states() == 6

    def test_cleanup_uses_scan_and_is_bounded(self):
        """The cleanup path runs when the store is at its cap — under attack."""
        store = session_module.OIDCStateStore(max_states=10)
        fake = _RecordingRedis([f"oidc:state:{i}" for i in range(50)])
        store._store = fake

        removed = store._cleanup_oldest_states(count=4)

        assert removed == 4
        assert len(fake.deleted) == 4
        assert fake.scan_calls == 1
        assert fake.keys_calls == 0

    def test_in_memory_fallback_still_works_without_scan(self):
        """The fallback store has no SCAN; the code must not require one."""
        store = session_module.OIDCStateStore(max_states=10)
        store._store = session_module.InMemoryStore()
        store.store.set("oidc:state:a", "{}", ex=60)

        assert store._count_states() == 1
        assert store._cleanup_oldest_states(count=5) == 1


@pytest.mark.unit
class TestOidcStoreDegradation:
    def _reset(self):
        session_module._redis_client = None
        session_module._in_memory_store = None
        session_module._store_initialized = False
        session_module._last_redis_probe = 0.0

    def teardown_method(self):
        self._reset()

    def test_falls_back_and_records_the_metric(self):
        self._reset()
        with (
            patch.object(session_module, "get_redis_client", return_value=None),
            patch.object(session_module, "_record_degradation") as degraded,
        ):
            store = session_module._get_store()

        assert isinstance(store, session_module.InMemoryStore)
        assert degraded.call_args.args == ("oidc_state", "local")

    def test_redis_is_reused_not_rebuilt_per_call(self):
        """A PING + fresh connection pool per call is not a health check, it is a leak."""
        self._reset()
        sentinel = _RecordingRedis([])
        with patch.object(session_module, "get_redis_client", return_value=sentinel) as factory:
            for _ in range(5):
                assert session_module._get_store() is sentinel

        assert factory.call_count == 1

    def test_the_fallback_re_probes_instead_of_latching(self):
        """A transient outage must not cost the process its shared state forever."""
        self._reset()
        recovered = _RecordingRedis([])

        with patch.object(session_module, "get_redis_client", return_value=None):
            assert isinstance(session_module._get_store(), session_module.InMemoryStore)

        # Pretend the re-probe interval has elapsed.
        session_module._last_redis_probe -= 10_000
        with patch.object(session_module, "get_redis_client", return_value=recovered):
            assert session_module._get_store() is recovered

    def test_it_reuses_the_lockout_reprobe_policy(self):
        """One interval constant, not two that can drift apart."""
        import inspect

        source = inspect.getsource(session_module._get_store)
        assert "REDIS_REPROBE_SECONDS" in source
        assert "from app.auth.lockout import REDIS_REPROBE_SECONDS" in source


@pytest.mark.unit
class TestRateLimiterDegradation:
    def test_limiter_is_pointed_at_redis_regardless_of_startup_probe(self):
        """The frozen ``memory://`` choice is the bug; the URI must always be Redis."""
        with patch.object(rate_limit_module, "_redis_reachable", return_value=False):
            limiter = rate_limit_module._create_limiter()

        assert type(limiter._storage).__name__ != "MemoryStorage", (
            "a failed startup probe must not permanently downgrade the limiter"
        )

    def test_in_memory_fallback_is_enabled(self):
        """Without it a dead storage raises instead of degrading — and never recovers."""
        limiter = rate_limit_module._create_limiter()
        assert limiter._in_memory_fallback_enabled is True
        assert limiter._fallback_limiter is not None

    def _run_key_func(self, storage_dead: bool):
        """Drive the key func once with the limiter reporting ``storage_dead``."""
        key_func = rate_limit_module._get_key_func()

        with (
            patch.object(rate_limit_module, "limiter", create=True) as fake_limiter,
            patch.object(rate_limit_module, "_record_degradation") as degraded,
        ):
            fake_limiter._storage_dead = storage_dead
            key_func(cast(Request, _FakeRequest()))

        return degraded

    def test_degraded_requests_are_counted(self):
        """The key func runs for every rate-limited request; it is where we can see it."""
        degraded = self._run_key_func(storage_dead=True)

        assert degraded.call_args.args == ("rate_limit", "local")

    def test_healthy_requests_are_not_counted(self):
        assert not self._run_key_func(storage_dead=False).called


@pytest.mark.unit
def test_degradation_counter_exists_with_bounded_labels():
    """Prometheus labels must stay bounded — no user ids, no raw paths."""
    from app.core import metrics

    assert sorted(metrics.security_state_degraded_total._labelnames) == ["control", "fallback"]
