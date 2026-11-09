"""get_active_tasks must cost a fixed number of Redis round trips, never a SCAN."""

from unittest.mock import patch

import pytest

from app.services.progress_tracker import ProgressTracker


class _CountingRedis:
    """In-memory Redis subset; ``calls`` records each server round trip (a pipeline is one)."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.calls: list[str] = []

    def pipeline(self):
        outer = self
        queued: list = []

        class _Pipe:
            def set(self, k, v, ex=None):
                queued.append(lambda: outer.kv.__setitem__(k, v))

            def sadd(self, k, *m):
                queued.append(lambda: outer.sets.setdefault(k, set()).update(m))

            def expire(self, *a):
                pass

            def execute(self):
                outer.calls.append("pipeline")
                for fn in queued:
                    fn()

        return _Pipe()

    def smembers(self, k):
        self.calls.append("smembers")
        return set(self.sets.get(k, set()))

    def mget(self, keys):
        self.calls.append("mget")
        return [self.kv.get(k) for k in keys]

    def srem(self, k, *m):
        self.calls.append("srem")
        self.sets.get(k, set()).difference_update(m)

    def scan_iter(self, *a, **k):
        self.calls.append("scan_iter")
        return iter(self.kv)

    def get(self, key):
        self.calls.append("get")
        return self.kv.get(key)

    def set(self, k, v, ex=None):
        self.kv[k] = v


@pytest.fixture
def redis_stub():
    stub = _CountingRedis()
    with patch("app.services.progress_tracker.get_request_redis", return_value=stub):
        yield stub


def test_lists_running_tasks_with_two_round_trips(redis_stub):
    for i in range(5):
        ProgressTracker(task_type=f"type{i}", user_id=7, total=10).start()
    ProgressTracker(task_type="other", user_id=8, total=10).start()
    redis_stub.calls.clear()

    found = ProgressTracker.get_active_tasks(7)

    assert sorted(s["task_type"] for s in found) == [f"type{i}" for i in range(5)]
    assert redis_stub.calls == ["smembers", "mget"]


def test_expired_state_is_pruned_from_index(redis_stub):
    ProgressTracker(task_type="gone", user_id=9, total=1).start()
    redis_stub.kv.pop("task_progress:gone:9")

    assert ProgressTracker.get_active_tasks(9) == []
    assert redis_stub.sets["task_progress_index:9"] == set()
