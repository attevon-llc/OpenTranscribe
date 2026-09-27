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
