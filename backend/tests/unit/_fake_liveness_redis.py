"""A dict-backed stand-in for ``redis.Redis``, shared by the liveness, replay and export tests.

It started as the handful of calls ``core/task_liveness.py`` and ``core/task_cancellation.py``
make (issue #1020) and is now installed for every ``get_redis()`` / ``get_request_redis()`` call
in the tests that use it, so it has to answer whatever command the code under test sends. It
lagged once: ``bump_corpus_version`` started calling ``incr`` and eleven export tests failed with
an ``AttributeError`` from this file. ``tests/unit/test_fake_redis_covers_app_calls.py`` now
fails when the app sends a command through those factories that this fake does not implement.

TTLs are recorded but never expire: every test here asserts on marker presence at a single
instant, so expiry would only add a clock to fake. :meth:`FakeRedis.lapse` makes a key's TTL run
out on demand.
"""

from __future__ import annotations

import builtins
import fnmatch
import time
from collections.abc import Iterator

from app.core.task_liveness import HEARTBEAT_KEY
from app.core.task_liveness import QUEUED_KEY


class FakeRedis:
    """Enough of ``redis.Redis`` for the liveness markers and the cancellation flag."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}
        # (channel, message) for every publish; the fake has no subscribers.
        self.published: list[tuple[str, str]] = []
        # Runs whose stage message "waits on a broker queue list" (a cold shutdown put it
        # back). Read through ``broker_orphans.queued_run_ids``, which this file patches.
        self.queued_stage_runs: set[str] = set()

    def setex(self, name: str, time: int, value: str) -> bool:
        self.store[name] = str(value)
        self.ttls[name] = int(time)
        return True

    def _keyspaces(self) -> tuple[dict, ...]:
        return (self.store, self.hashes, self.zsets, self.lists, self.sets)

    def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            removed += int(any(space.pop(name, None) is not None for space in self._keyspaces()))
            self.ttls.pop(name, None)
        return removed

    def exists(self, *names: str) -> int:
        return sum(int(any(name in space for space in self._keyspaces())) for name in names)

    def type(self, name: str) -> str:
        kinds = ("string", "hash", "zset", "list", "set")
        spaces = zip(kinds, self._keyspaces(), strict=True)
        return next((kind for kind, space in spaces if name in space), "none")

    def scan_iter(self, match: str | None = None, count: int | None = None) -> Iterator[str]:
        keys = [key for space in self._keyspaces() for key in space]
        return iter([key for key in keys if match is None or fnmatch.fnmatchcase(key, match)])

    def expire(self, name: str, time: int) -> bool:
        if not any(name in space for space in self._keyspaces()):
            return False
        self.ttls[name] = int(time)
        return True

    def incr(self, name: str, amount: int = 1) -> int:
        value = int(self.store.get(name, "0")) + amount
        self.store[name] = str(value)
        return value

    def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 0

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(key) for key in keys]

    def get(self, name: str) -> str | None:
        return self.store.get(name)

    def set(
        self, name: str, value: str, ex: int | None = None, px: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and name in self.store:
            return None
        self.store[name] = str(value)
        if ex is not None:
            self.ttls[name] = int(ex)
        elif px is not None:
            self.ttls[name] = int(px) // 1000
        return True

    # --- hashes (the lost-task replay records, issue #1067) ---------------------------------
    def hset(self, name: str, key: str, value: str) -> int:
        self.hashes.setdefault(name, {})[key] = str(value)
        return 1

    def hget(self, name: str, key: str) -> str | None:
        return self.hashes.get(name, {}).get(key)

    def hdel(self, name: str, *keys: str) -> int:
        bucket = self.hashes.get(name, {})
        return sum(int(bucket.pop(key, None) is not None) for key in keys)

    def hexists(self, name: str, key: str) -> bool:
        return key in self.hashes.get(name, {})

    def hgetall(self, name: str) -> dict[str, str]:
        return dict(self.hashes.get(name, {}))

    def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        bucket = self.hashes.setdefault(name, {})
        bucket[key] = str(int(bucket.get(key, "0")) + amount)
        return int(bucket[key])

    # --- lists and sets -----------------------------------------------------------------------
    def lpush(self, name: str, *values: str) -> int:
        bucket = self.lists.setdefault(name, [])
        for value in values:
            bucket.insert(0, str(value))
        return len(bucket)

    def lrange(self, name: str, start: int, end: int) -> list[str]:
        bucket = self.lists.get(name, [])
        return bucket[start : None if end == -1 else end + 1]

    def ltrim(self, name: str, start: int, end: int) -> bool:
        if name in self.lists:
            self.lists[name] = self.lrange(name, start, end)
        return True

    def sadd(self, name: str, *values: str) -> int:
        bucket = self.sets.setdefault(name, set())
        added = {str(v) for v in values} - bucket
        bucket |= added
        return len(added)

    # ``builtins.set``: inside this class ``set`` names the SET command defined above.
    def smembers(self, name: str) -> builtins.set[str]:
        return builtins.set(self.sets.get(name, builtins.set()))

    def srem(self, name: str, *values: str) -> int:
        bucket = self.sets.get(name, set())
        removed = {str(v) for v in values} & bucket
        bucket -= removed
        return len(removed)

    # --- sorted sets (the per-file infrastructure-requeue counter) ---------------------------
    def zincrby(self, name: str, amount: float, value: str) -> float:
        bucket = self.zsets.setdefault(name, {})
        bucket[value] = bucket.get(value, 0.0) + amount
        return bucket[value]

    def zscore(self, name: str, value: str) -> float | None:
        return self.zsets.get(name, {}).get(value)

    def zrem(self, name: str, *values: str) -> int:
        bucket = self.zsets.get(name, {})
        return sum(int(bucket.pop(v, None) is not None) for v in values)

    # Score bounds are positional-only: redis-py calls them min / max, which would shadow the
    # builtins here, and a positional-only name cannot be mismatched by a keyword call.
    def zcount(self, name: str, low: float | str, high: float | str, /) -> int:
        low, high = float(low), float(high)
        return sum(1 for score in self.zsets.get(name, {}).values() if low <= score <= high)

    def zrangebyscore(self, name: str, low: float | str, high: float | str, /) -> list[str]:
        low, high = float(low), float(high)
        bucket = self.zsets.get(name, {})
        return [
            member
            for member, score in sorted(bucket.items(), key=lambda item: item[1])
            if low <= score <= high
        ]

    def zremrangebyscore(self, name: str, low: float | str, high: float | str, /) -> int:
        return self.zrem(name, *self.zrangebyscore(name, low, high))

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    # --- helpers for arranging a run's state -----------------------------------------------
    # Never named after a Redis command: the parity test holds these apart from the real API.
    def lapse(self, key: str) -> None:
        """The TTL of ``key`` ran out."""
        self.delete(key)

    def mark_queued(self, task_id: str) -> None:
        self.setex(QUEUED_KEY.format(task_id=task_id), 3600, "1")

    def mark_running(self, task_id: str, *, started_seconds_ago: float) -> None:
        self.setex(
            HEARTBEAT_KEY.format(task_id=task_id), 300, repr(time.time() - started_seconds_ago)
        )


class _FakePipeline:
    """Queues commands and runs them against the fake on ``execute()``, as a pipeline does.

    After ``watch()`` it runs commands immediately and returns their results, until ``multi()``
    starts buffering again (redis-py's optimistic-locking shape). Nothing else writes to the
    fake between the two, so ``execute()`` never raises ``WatchError``.
    """

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple, dict]] = []
        self._immediate = False

    def watch(self, *names: str) -> bool:
        self._immediate = True
        return True

    def unwatch(self) -> bool:
        self._immediate = False
        return True

    def multi(self) -> None:
        self._immediate = False

    def __getattr__(self, name: str):
        def _run_or_record(*args, **kwargs):
            if self._immediate:
                return getattr(self._redis, name)(*args, **kwargs)
            self._ops.append((name, args, kwargs))
            return self

        return _run_or_record

    def execute(self) -> list:
        ops, self._ops = self._ops, []
        return [getattr(self._redis, name)(*args, **kwargs) for name, args, kwargs in ops]


class BrokenRedis:
    """Every call fails, as an unreachable broker does."""

    def __getattr__(self, name: str):
        def _raise(*args, **kwargs):
            raise ConnectionError("redis unreachable")

        return _raise


def install_fake_redis(monkeypatch) -> FakeRedis:
    """Route every ``get_redis()`` call through a fresh :class:`FakeRedis`."""
    fake = FakeRedis()
    # Both clients: workers write liveness through get_redis(), request-path readers
    # (stuck-task detection, the cancel endpoints) through get_request_redis().
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)
    monkeypatch.setattr("app.core.redis.get_request_redis", lambda: fake)
    monkeypatch.setattr("app.core.broker_orphans.queued_run_ids", lambda: fake.queued_stage_runs)
    return fake
