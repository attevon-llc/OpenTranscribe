"""A dict-backed stand-in for the Redis calls ``core/task_liveness.py`` and
``core/task_cancellation.py`` make, shared by the issue #1020 test modules.

TTLs are recorded but never expire: every test here asserts on marker presence at a single
instant, so expiry would only add a clock to fake.
"""

from __future__ import annotations

import time

from app.core.task_liveness import HEARTBEAT_KEY
from app.core.task_liveness import QUEUED_KEY


class FakeRedis:
    """Enough of ``redis.Redis`` for the liveness markers and the cancellation flag."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def setex(self, key: str, ttl: int, value: str) -> bool:
        self.store[key] = str(value)
        self.ttls[key] = int(ttl)
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += int(self.store.pop(key, None) is not None)
            self.ttls.pop(key, None)
        return removed

    def exists(self, key: str) -> int:
        return int(key in self.store)

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store.get(key) for key in keys]

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        if ex is not None:
            self.ttls[key] = int(ex)
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

    # --- sorted sets (the per-file infrastructure-requeue counter) ---------------------------
    def zincrby(self, name: str, amount: float, member: str) -> float:
        bucket = self.zsets.setdefault(name, {})
        bucket[member] = bucket.get(member, 0.0) + amount
        return bucket[member]

    def zscore(self, name: str, member: str) -> float | None:
        return self.zsets.get(name, {}).get(member)

    def zrem(self, name: str, *members: str) -> int:
        bucket = self.zsets.get(name, {})
        return sum(int(bucket.pop(m, None) is not None) for m in members)

    def zcount(self, name: str, low: float, high: float | str) -> int:
        top = float("inf") if high == "+inf" else float(high)
        return sum(1 for score in self.zsets.get(name, {}).values() if low <= score <= top)

    def expire(self, key: str) -> None:
        """Test helper: the TTL of ``key`` ran out."""
        self.store.pop(key, None)
        self.ttls.pop(key, None)

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    # --- helpers for arranging a run's state -----------------------------------------------
    def mark_queued(self, task_id: str) -> None:
        self.setex(QUEUED_KEY.format(task_id=task_id), 3600, "1")

    def mark_running(self, task_id: str, *, started_seconds_ago: float) -> None:
        self.setex(
            HEARTBEAT_KEY.format(task_id=task_id), 300, repr(time.time() - started_seconds_ago)
        )


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple]] = []

    def delete(self, *keys: str) -> _FakePipeline:
        self._ops.append(("delete", keys))
        return self

    def setex(self, key: str, ttl: int, value: str) -> _FakePipeline:
        self._ops.append(("setex", (key, ttl, value)))
        return self

    def __getattr__(self, name: str):
        def _record(*args):
            self._ops.append((name, args))
            return self

        return _record

    def execute(self) -> list:
        return [getattr(self._redis, name)(*args) for name, args in self._ops]


class BrokenRedis:
    """Every call fails, as an unreachable broker does."""

    def __getattr__(self, name: str):
        def _raise(*args, **kwargs):
            raise ConnectionError("redis unreachable")

        return _raise


def install_fake_redis(monkeypatch) -> FakeRedis:
    """Route every ``get_redis()`` call through a fresh :class:`FakeRedis`."""
    fake = FakeRedis()
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)
    return fake
