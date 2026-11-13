"""Store-layer mutation survivors for ``app/auth/lockout_store.py`` (issue #446).

Moved verbatim out of ``test_lockout_survivor_mutants.py`` when ``lockout.py`` was split by
concern: these three classes test the store (``InMemoryLockoutStore``, ``_get_redis_client``,
``_get_store``'s re-probe), not the counting policy, so they follow the code into their own
file. Each test's reasoning is unchanged; see that file's docstring for how the survivors
were triaged.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app.auth import lockout_store
from app.core.config import settings

# ── InMemoryLockoutStore.keys: prefix matching, not a character class ─────────────


class TestInMemoryStoreKeys:
    def test_keys_strips_only_the_trailing_wildcard_not_arbitrary_characters(self) -> None:
        """``rstrip("*")`` must treat ``"*"`` as a literal suffix marker.

        ``str.rstrip`` strips every trailing character IN its argument's character
        set. A mutant widening the argument to ``"XX*XX"`` still strips the ``*``
        (still in the set) but ALSO strips any trailing ``X`` from the prefix
        itself, silently broadening the match to keys that were never asked for.
        """
        store = lockout_store.InMemoryLockoutStore()
        store.set("lockout:userX", "keep")
        store.set("lockout:user999", "must-not-match")

        matched = store.keys("lockout:userX*")

        assert matched == ["lockout:userX"], (
            "a pattern ending in 'X*' must strip only the '*', not the prefix's own 'X'"
        )


# ── _get_redis_client: must actually use the configured URL ───────────────────────


class TestGetRedisClient:
    def test_connects_to_the_configured_url(self, monkeypatch) -> None:
        """Dropping ``settings.REDIS_URL`` to ``None`` would make every connection
        attempt fail (or connect to whatever redis-py defaults to), silently
        disabling distributed lockout storage for the life of the process."""
        import redis

        calls: list[object] = []

        class _FakePinger:
            def ping(self) -> bool:
                return True

        def fake_from_url(cls, url, **kwargs):
            calls.append(url)
            return _FakePinger()

        monkeypatch.setattr(redis.Redis, "from_url", classmethod(fake_from_url))

        client = lockout_store._get_redis_client()

        assert calls == [settings.REDIS_URL]
        assert client is not None


# ── _get_store: the reprobe timestamp must stay usable ─────────────────────────────


@pytest.fixture
def store_on_fallback_after_a_failed_reprobe(monkeypatch) -> None:
    """Put ``_get_store()`` through exactly one failed reprobe attempt.

    Common setup for both tests below, so neither has to repeat the four
    ``monkeypatch.setattr`` calls that put the module in "just probed, still on
    the fallback" state.
    """
    monkeypatch.setattr(lockout_store, "_store_initialized", True)
    monkeypatch.setattr(lockout_store, "_redis_client", None)
    monkeypatch.setattr(lockout_store, "_in_memory_store", lockout_store.InMemoryLockoutStore())
    monkeypatch.setattr(lockout_store, "_last_redis_probe", 0.0)  # force the window open

    with patch.object(lockout_store, "_get_redis_client", return_value=None):
        lockout_store._get_store()


class TestGetStoreReprobe:
    """``_last_redis_probe = None`` would make the NEXT call's
    ``now - _last_redis_probe`` raise ``TypeError`` -- crashing every login while
    Redis stays down, instead of just skipping the reprobe."""

    def test_a_failed_reprobe_records_a_real_timestamp(
        self, store_on_fallback_after_a_failed_reprobe
    ) -> None:
        assert isinstance(lockout_store._last_redis_probe, float)
        assert lockout_store._last_redis_probe > 0, (
            "0.0 is the pre-probe sentinel, not a real clock read"
        )

    def test_a_second_call_inside_the_window_does_not_crash_or_reprobe(
        self, store_on_fallback_after_a_failed_reprobe
    ) -> None:
        probe_calls: list[int] = []

        def _count_and_return_none():
            probe_calls.append(1)
            return None

        with patch.object(lockout_store, "_get_redis_client", side_effect=_count_and_return_none):
            store = lockout_store._get_store()  # raises TypeError under the mutant if this crashes

        assert probe_calls == [], "still inside the reprobe window -- must not probe again"
        assert isinstance(store, lockout_store.InMemoryLockoutStore)


# ── #1373: the store's degradation signals and its expiry/prune boundaries ─────────

STORE_LOGGER = "app.auth.lockout_store"


def _store_events(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == STORE_LOGGER and r.levelno == level]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestDegradationIsAudited:
    """Falling back to per-process lockout state is the auth-throttling bypass of issue
    #284 A1.16, so each step into and out of it must say so, at the right level."""

    def test_a_failed_redis_connection_warns_with_cause_and_traceback(self, monkeypatch, caplog):
        import redis

        def boom(cls, url, **kwargs):
            raise redis.ConnectionError("no route to redis for the test")

        monkeypatch.setattr(redis.Redis, "from_url", classmethod(boom))
        caplog.set_level(logging.DEBUG, logger=STORE_LOGGER)

        assert lockout_store._get_redis_client() is None

        (rec,) = _store_events(caplog, logging.WARNING)
        assert "Redis connection failed for lockout storage" in rec.getMessage()
        assert "no route to redis for the test" in rec.getMessage()
        assert rec.exc_info and rec.exc_info[0] is redis.ConnectionError

    def test_first_use_without_redis_warns_that_state_is_not_shared(self, monkeypatch, caplog):
        monkeypatch.setattr(lockout_store, "_store_initialized", False)
        monkeypatch.setattr(lockout_store, "_redis_client", None)
        monkeypatch.setattr(lockout_store, "_in_memory_store", None)
        caplog.set_level(logging.DEBUG, logger=STORE_LOGGER)

        with patch.object(lockout_store, "_get_redis_client", return_value=None):
            lockout_store._get_store()

        (rec,) = _store_events(caplog, logging.WARNING)
        msg = rec.getMessage()
        assert msg.startswith("Using in-memory lockout storage.")
        assert "Lockout state will not persist across restarts" in msg
        assert msg.endswith("will not work in distributed deployments.")

    def test_recovering_redis_is_announced_at_info(self, monkeypatch, caplog):
        monkeypatch.setattr(lockout_store, "_store_initialized", True)
        monkeypatch.setattr(lockout_store, "_redis_client", None)
        monkeypatch.setattr(lockout_store, "_in_memory_store", lockout_store.InMemoryLockoutStore())
        monkeypatch.setattr(lockout_store, "_last_redis_probe", 0.0)
        caplog.set_level(logging.DEBUG, logger=STORE_LOGGER)
        sentinel = object()

        with patch.object(lockout_store, "_get_redis_client", return_value=sentinel):
            assert lockout_store._get_store(clock=lambda: 10_000.0) is sentinel

        (rec,) = _store_events(caplog, logging.INFO)
        assert rec.getMessage().startswith("Redis recovered")
        assert rec.getMessage().endswith("resuming distributed lockout storage")


class TestInMemoryExpiryBoundaries:
    def test_a_key_is_gone_at_exactly_its_deadline(self) -> None:
        clock = _Clock()
        store = lockout_store.InMemoryLockoutStore(clock=clock)
        store.set("k", "v", ex=10)
        clock.now += 10
        assert store.get("k") is None

    def test_deleting_an_expired_key_reports_nothing_deleted(self) -> None:
        clock = _Clock()
        store = lockout_store.InMemoryLockoutStore(clock=clock)
        store.set("k", "v", ex=10)
        clock.now += 11
        assert store.delete("k") == 0

    def test_deleting_a_live_key_reports_exactly_one(self) -> None:
        store = lockout_store.InMemoryLockoutStore()
        store.set("k", "v")
        assert store.delete("k") == 1
        assert store.delete("k") == 0

    def test_deleting_a_key_drops_its_deadline_too(self) -> None:
        """A successful login deletes the attempt counter. Its deadline must go with it.

        The ``max_records`` ceiling only bounds ``_data``, so a deadline left behind for a
        deleted key is memory nothing evicts until the lockout window ends, one entry per
        cleared counter. It also breaks the invariant (every key in ``_expires`` is in
        ``_data``) that ``_drop_if_expired`` and ``_prune_expired`` rely on. Counted through
        ``_expires`` because bounded memory IS the contract.
        """
        store = lockout_store.InMemoryLockoutStore()
        store.set("lockout:a", "v", ex=3600)
        store.set("lockout:b", "v", ex=3600)

        assert store.delete("lockout:a") == 1

        assert "lockout:a" not in store._expires
        assert store._expires.keys() == store._data.keys() == {"lockout:b"}

    def test_keys_sweeps_a_key_whose_deadline_is_now(self) -> None:
        clock = _Clock()
        store = lockout_store.InMemoryLockoutStore(clock=clock)
        store.set("lockout:a", "v", ex=10)
        clock.now += 10
        assert store.keys("lockout:*") == []


class TestInMemoryPruneCadence:
    """The sweep piggybacks on writes: every ``_PRUNE_EVERY_WRITES``-th write, or when the
    store is over its bound. Counted through ``_data`` because bounded memory IS the contract."""

    def _with_one_expired_key(self):
        clock = _Clock()
        store = lockout_store.InMemoryLockoutStore(clock=clock)
        store.set("expired", "v", ex=1)  # write #1
        clock.now += 5
        return store

    def test_no_sweep_before_the_256th_write(self) -> None:
        store = self._with_one_expired_key()
        for i in range(lockout_store._PRUNE_EVERY_WRITES - 2):  # writes #2 .. #255
            store.set(f"k{i}", "v")
        assert "expired" in store._data

    def test_the_256th_write_sweeps(self) -> None:
        store = self._with_one_expired_key()
        for i in range(lockout_store._PRUNE_EVERY_WRITES - 1):  # writes #2 .. #256
            store.set(f"k{i}", "v")
        assert "expired" not in store._data

    def test_the_counter_restarts_from_zero_after_a_sweep(self) -> None:
        store = self._with_one_expired_key()
        for i in range(lockout_store._PRUNE_EVERY_WRITES - 1):
            store.set(f"a{i}", "v")  # sweep happens on the 256th write overall
        store.set("expired2", "v", ex=1)  # first write after the sweep
        store._clock.now += 5
        for i in range(lockout_store._PRUNE_EVERY_WRITES - 2):  # 255 writes since the sweep
            store.set(f"b{i}", "v")
        assert "expired2" in store._data

    def test_a_store_exactly_at_its_bound_is_not_swept(self) -> None:
        clock = _Clock()
        store = lockout_store.InMemoryLockoutStore(max_records=3, clock=clock)
        store.set("expired", "v", ex=1)
        clock.now += 5
        store.set("b", "v")
        store.set("c", "v")  # len == max_records: at the bound, not over it
        assert "expired" in store._data
