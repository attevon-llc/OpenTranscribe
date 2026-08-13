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

from app.auth import lockout as lockout_module
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

    def ping(self):
        # `lockout._get_redis_client` PINGs before returning the client, so a stand-in
        # without this is reported as an unreachable Redis and the test silently exercises
        # the fallback instead of the path it names.
        return True


class _CountingProbe:
    """A real stand-in for ``get_redis_client`` that counts how often it was asked.

    A ``Mock`` would do this, but then the assertion is on ``mock.call_count`` — mock
    bookkeeping, which ``scripts/audit-tests.py``'s ``mock-only`` detector correctly flags,
    because a test whose every assertion is about a mock proves wiring rather than
    behaviour. Here the probe count IS the behaviour under test (each probe is a connection
    setup plus a PING on an unauthenticated path), so it is counted on a real object.
    """

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._result


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

    # ---------------------------------------------------------------- mutation gaps
    #
    # The five tests below were written from surviving mutants (issue #431). The tests
    # above cover the happy path of each branch, which is why the mutants lived: the
    # re-probe test jumps `_last_redis_probe -= 10_000`, so it proves a probe eventually
    # happens and says nothing about *when*. Flipping `now - _last_redis_probe` to
    # `now + _last_redis_probe` (always true, re-probe every call), `>=` to `>` (never at
    # the boundary), and dropping the `InMemoryStore()` construction all survived it.

    def test_the_fallback_does_not_re_probe_within_the_interval(self):
        """The negative half of the re-probe policy, and the reason it exists.

        `_get_store` runs on an **unauthenticated** endpoint. Re-probing on every call is
        what this code replaced: a fresh client plus a PING per OIDC login step. A mutation
        making the interval check always-true restores exactly that and no existing test
        notices, because they all assert that a probe *does* eventually happen.
        """
        self._reset()
        probe = _CountingProbe(result=None)
        with patch.object(session_module, "get_redis_client", probe):
            first = session_module._get_store()
            # Still well inside the interval — no further probe is allowed.
            rest = [session_module._get_store() for _ in range(5)]

        assert probe.calls == 1, (
            f"Redis was probed {probe.calls}x across 6 calls — the interval check is not "
            "holding, so every unauthenticated request pays a connection setup and a PING"
        )
        # ...and the six calls are one store, not six: the count above is about cost, this
        # is about correctness, and a test asserting only the count would pass for a
        # function that returned a fresh store each time.
        assert isinstance(first, session_module.InMemoryStore)
        assert all(store is first for store in rest)

    def test_it_re_probes_exactly_at_the_interval_boundary(self):
        """`>=`, not `>`. At exactly the interval, the probe is due.

        Pinned because `>` differs from `>=` only on the boundary, and a policy that is
        one tick late forever is indistinguishable from one that works until you measure it.
        """
        from app.auth.lockout import REDIS_REPROBE_SECONDS

        self._reset()
        recovered = _RecordingRedis([])
        with patch.object(session_module, "get_redis_client", return_value=None):
            assert isinstance(session_module._get_store(), session_module.InMemoryStore)

        # The clock must be PATCHED, not merely rewound. Setting
        # `_last_redis_probe = time.monotonic() - REDIS_REPROBE_SECONDS` and letting the
        # code read the real clock puts `now` a few microseconds PAST the boundary, so `>`
        # is satisfied too and the `>=`-vs-`>` distinction goes untested — which is how the
        # mutant survived the first version of this test.
        frozen = session_module._last_redis_probe + REDIS_REPROBE_SECONDS
        with (
            patch.object(session_module.time, "monotonic", return_value=frozen),
            patch.object(session_module, "get_redis_client", return_value=recovered),
        ):
            assert session_module._get_store() is recovered, (
                "no re-probe at exactly REDIS_REPROBE_SECONDS — the comparison is `>` "
                "rather than `>=`, so every re-probe is one full interval late"
            )

    def test_the_first_fallback_call_returns_a_real_store(self):
        """Not None. The fallback is on the login path, so None is a 500 at the door.

        **This test does not kill the corresponding mutant, and cannot.** Dropping the
        `_in_memory_store = InMemoryStore()` assignment in the not-yet-initialised branch is
        an EQUIVALENT mutation: the later `if _in_memory_store is None` re-creates the store
        before returning, so no observable behaviour changes. Verified by applying the
        mutation and watching all 19 tests still pass.

        Kept anyway, because the *property* — the first fallback call hands back a usable
        store — is worth pinning against a future edit that removes the second construction
        too. Recorded here rather than in a mutation report so the next person triaging
        `session` does not re-litigate it as a finding.
        """
        self._reset()
        with (
            patch.object(session_module, "get_redis_client", return_value=None),
            patch.object(session_module, "_record_degradation"),
        ):
            first = session_module._get_store()

        assert first is not None
        assert isinstance(first, session_module.InMemoryStore)

    def test_every_fallback_caller_shares_one_store(self):
        """One `InMemoryStore` per process, or OIDC state is written and never found.

        A state stored by one caller and redeemed by another is the whole point of the
        store; handing out a fresh instance per call degrades from "not shared across
        replicas" to "not shared across function calls", which fails every login rather
        than a fraction of them.
        """
        self._reset()
        with (
            patch.object(session_module, "get_redis_client", return_value=None),
            patch.object(session_module, "_record_degradation"),
        ):
            stores = [session_module._get_store() for _ in range(4)]

        assert all(s is stores[0] for s in stores)

    def test_no_degradation_is_recorded_while_redis_is_healthy(self):
        """The metric must mean something.

        `security_state_degraded_total` is how a deployment learns its OIDC logins are
        per-replica. Incrementing it on the healthy path would make it fire constantly and
        train whoever reads the dashboard to ignore it — a worse outcome than not having it,
        and invisible to any test that only checks the failing path.
        """
        self._reset()
        healthy = _RecordingRedis([])
        with (
            patch.object(session_module, "get_redis_client", return_value=healthy),
            patch.object(session_module, "_record_degradation") as degraded,
        ):
            for _ in range(3):
                assert session_module._get_store() is healthy

        degraded.assert_not_called()

    def test_a_broken_metrics_backend_cannot_break_the_login_flow(self):
        """`_record_degradation` swallows everything, and that contract is load-bearing.

        It runs on the fallback path — i.e. when infrastructure is *already* degraded — so
        a raising metrics import would turn "Redis is down, logins still work per-replica"
        into "logins are down too".
        """
        self._reset()
        with (
            patch.dict("sys.modules", {"app.core.metrics": None}),
            patch.object(session_module, "get_redis_client", return_value=None),
        ):
            store = session_module._get_store()  # must not raise

        assert isinstance(store, session_module.InMemoryStore)


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


@pytest.mark.unit
class TestLockoutStoreDegradation:
    """``lockout._get_store`` — the same shape as ``session._get_store``, same stakes.

    Lockout state is what stops password guessing, so when Redis goes down the fallback and
    its recovery are a security control, not a convenience. Mutation testing left the whole
    re-probe policy unasserted here even though the session equivalent is now covered: the
    interval could be made always-true (a fresh client plus a PING per login attempt, on an
    unauthenticated path), the boundary could slip by one interval, ``recovered is not None``
    could invert (so a recovered Redis is discarded and the process stays per-replica
    forever), and the fallback store could be left as ``None``.

    Per-replica lockout counting is the consequence that matters: N replicas means N times
    the configured threshold before anyone is locked out.
    """

    def _reset(self):
        lockout_module._redis_client = None
        lockout_module._in_memory_store = None
        lockout_module._store_initialized = False
        lockout_module._last_redis_probe = 0.0

    def teardown_method(self):
        self._reset()

    def test_the_client_decodes_responses(self):
        """``decode_responses=True`` is not cosmetic.

        Without it redis-py returns bytes, so ``key.startswith(LOCKOUT_PREFIX)`` in the
        cleanup sweep compares str to bytes and every key looks foreign — the sweep silently
        stops sweeping. Asserted on the kwargs actually passed, because nothing else observes
        it until that sweep quietly does nothing.
        """
        captured: dict[str, object] = {}

        class _FromUrlSpy:
            @staticmethod
            def from_url(url, **kwargs):
                captured.update(kwargs)
                captured["url"] = url
                return _RecordingRedis([])

        with patch.dict("sys.modules", {"redis": type("_M", (), {"Redis": _FromUrlSpy})}):
            client = lockout_module._get_redis_client()

        assert client is not None
        assert captured["decode_responses"] is True
        assert captured["socket_connect_timeout"] == 5
        assert captured["socket_timeout"] == 5

    def test_a_missing_redis_package_falls_back_rather_than_raising(self):
        """`ImportError` must degrade, not 500 the login route."""
        self._reset()
        with patch.dict("sys.modules", {"redis": None}):
            assert lockout_module._get_redis_client() is None

    def test_the_fallback_store_is_a_real_store(self):
        self._reset()
        with patch.object(lockout_module, "_get_redis_client", return_value=None):
            store = lockout_module._get_store()

        assert store is not None
        assert isinstance(store, lockout_module.InMemoryLockoutStore)

    def test_every_fallback_caller_shares_one_store(self):
        """One store per process, or two login attempts do not see each other's count.

        A fresh store per call degrades lockout from "counted per replica" to "not counted at
        all" — the threshold is never reached and the control is off.
        """
        self._reset()
        with patch.object(lockout_module, "_get_redis_client", return_value=None):
            stores = [lockout_module._get_store() for _ in range(4)]

        assert all(s is stores[0] for s in stores)

    def test_redis_is_probed_once_while_it_is_healthy(self):
        self._reset()
        healthy = _RecordingRedis([])
        probe = _CountingProbe(result=healthy)
        with patch.object(lockout_module, "_get_redis_client", probe):
            for _ in range(5):
                assert lockout_module._get_store() is healthy

        assert probe.calls == 1

    def test_the_fallback_does_not_re_probe_within_the_interval(self):
        """The always-true interval mutation restores a PING per unauthenticated attempt."""
        self._reset()
        probe = _CountingProbe(result=None)
        with patch.object(lockout_module, "_get_redis_client", probe):
            first = lockout_module._get_store()
            rest = [lockout_module._get_store() for _ in range(5)]

        assert probe.calls == 1, (
            f"Redis probed {probe.calls}x across 6 calls — every failed login would pay a "
            "connection setup and a PING"
        )
        assert isinstance(first, lockout_module.InMemoryLockoutStore)
        assert all(store is first for store in rest)

    def test_it_re_probes_exactly_at_the_interval_boundary(self):
        """``>=``, not ``>``. Needs a PATCHED clock: rewinding lands past the boundary."""
        self._reset()
        recovered = _RecordingRedis([])
        with patch.object(lockout_module, "_get_redis_client", return_value=None):
            assert isinstance(lockout_module._get_store(), lockout_module.InMemoryLockoutStore)

        frozen = lockout_module._last_redis_probe + lockout_module.REDIS_REPROBE_SECONDS
        with (
            patch.object(lockout_module.time, "monotonic", return_value=frozen),
            patch.object(lockout_module, "_get_redis_client", return_value=recovered),
        ):
            assert lockout_module._get_store() is recovered

    def test_a_recovered_redis_is_adopted_and_the_fallback_dropped(self):
        """``if recovered is not None`` must not invert.

        Inverted, a recovered Redis is thrown away and the replica counts lockouts in its own
        memory forever — the outage becomes permanent without another restart.
        """
        self._reset()
        recovered = _RecordingRedis([])
        with patch.object(lockout_module, "_get_redis_client", return_value=None):
            assert isinstance(lockout_module._get_store(), lockout_module.InMemoryLockoutStore)

        lockout_module._last_redis_probe -= 10_000
        with patch.object(lockout_module, "_get_redis_client", return_value=recovered):
            assert lockout_module._get_store() is recovered
        # ...and it stays adopted without re-probing.
        assert lockout_module._redis_client is recovered

    def test_a_probe_that_still_fails_keeps_the_same_fallback_store(self):
        """The negative of adoption: a failed re-probe must not discard accumulated counts."""
        self._reset()
        with patch.object(lockout_module, "_get_redis_client", return_value=None):
            first = lockout_module._get_store()
            lockout_module._last_redis_probe -= 10_000
            second = lockout_module._get_store()

        assert second is first
