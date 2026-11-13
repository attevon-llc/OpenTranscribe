"""``dispatch_analytics`` coalesces a burst per file and fails open without Redis."""

from __future__ import annotations

from app.tasks import analytics_dispatch
from app.tasks.analytics import analyze_transcript_task


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


def _patch(monkeypatch, redis):
    calls: list[dict] = []
    monkeypatch.setattr("app.core.redis.get_request_redis", lambda: redis)
    monkeypatch.setattr(analyze_transcript_task, "apply_async", lambda **kw: calls.append(kw))
    return calls


def test_burst_dispatches_once_per_file(monkeypatch):
    calls = _patch(monkeypatch, _FakeRedis())
    assert analytics_dispatch.dispatch_analytics("f1") is True
    assert analytics_dispatch.dispatch_analytics("f1") is False
    assert analytics_dispatch.dispatch_analytics("f2") is True
    assert [c["kwargs"]["file_uuid"] for c in calls] == ["f1", "f2"]
    assert all(c["countdown"] == analytics_dispatch.ANALYTICS_DEBOUNCE_SECONDS for c in calls)


def test_redis_failure_still_dispatches(monkeypatch):
    class _Down:
        def set(self, *a, **k):
            raise ConnectionError("down")

    calls = _patch(monkeypatch, _Down())
    assert analytics_dispatch.dispatch_analytics("f1") is True
    assert len(calls) == 1
