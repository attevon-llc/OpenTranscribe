"""Store-layer mutation survivors for ``app/auth/lockout_store.py`` (issue #446).

Moved verbatim out of ``test_lockout_survivor_mutants.py`` when ``lockout.py`` was split by
concern: these three classes test the store (``InMemoryLockoutStore``, ``_get_redis_client``,
``_get_store``'s re-probe), not the counting policy, so they follow the code into their own
file. Each test's reasoning is unchanged; see that file's docstring for how the survivors
were triaged.
"""

from __future__ import annotations

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
