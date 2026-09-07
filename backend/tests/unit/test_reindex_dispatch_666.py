"""``dispatch_transcript_reindex`` debounce mechanics (issue #666), no live stack.

Real Redis state (a genuine SETNX + TTL, not a mock assertion of "was the key
set") proves the debounce: a second call inside the window must not queue a
second task, and a call after the key is gone must queue again. The Celery
dispatch itself is captured (there is no broker in this suite — every other
test in this repo captures `.apply_async`/`.delay` the same way, see
`tests/api/test_rename_propagation_dispatch.py`), but the coalescing logic
under test — the thing #666 asked to be debounced — is exercised for real
against a real, if fake, key-value store with real expiry semantics.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class _FakeRedis:
    """Just enough of `redis.Redis` for SET NX EX semantics to be REAL, not mocked."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}

    def set(self, key, value, nx=False, ex=None):
        now = time.monotonic()
        existing = self._store.get(key)
        if existing is not None:
            _, expires_at = existing
            if expires_at is not None and expires_at <= now:
                del self._store[key]
                existing = None
        if nx and key in self._store:
            return None
        expires_at = now + ex if ex is not None else None
        self._store[key] = (value, expires_at)
        return True

    def expire_now(self, key) -> None:
        """Test helper: force a key past its TTL without sleeping."""
        if key in self._store:
            value, _ = self._store[key]
            self._store[key] = (value, time.monotonic() - 1)


@pytest.fixture
def fake_redis():
    return _FakeRedis()


def _patched(fake_redis, task_mock):
    return (
        patch("app.core.redis.get_redis", return_value=fake_redis),
        patch("app.tasks.search_indexing_task.index_transcript_search_task", task_mock),
    )


def test_first_call_claims_the_debounce_key_and_queues_the_task(fake_redis):
    from app.core.constants import TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    task_mock = MagicMock()
    p1, p2 = _patched(fake_redis, task_mock)
    with p1, p2:
        queued = dispatch_transcript_reindex(file_id=1, file_uuid="file-a", user_id=7)

    assert queued is True
    task_mock.apply_async.assert_called_once_with(
        kwargs={"file_id": 1, "file_uuid": "file-a", "user_id": 7},
        countdown=TRANSCRIPT_REINDEX_DEBOUNCE_SECONDS,
    )
    # Real state: the debounce key genuinely exists in the store with a TTL,
    # not merely "redis.set was called".
    assert "transcript_reindex_debounce:file-a" in fake_redis._store
    _, expires_at = fake_redis._store["transcript_reindex_debounce:file-a"]
    assert expires_at is not None


def test_a_second_call_within_the_window_is_coalesced_not_queued_twice(fake_redis):
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    task_mock = MagicMock()
    p1, p2 = _patched(fake_redis, task_mock)
    with p1, p2:
        first = dispatch_transcript_reindex(file_id=1, file_uuid="file-b", user_id=7)
        # Twenty rapid edits to the same file within the debounce window —
        # the scenario issue #666 explicitly calls out.
        for _ in range(19):
            dispatch_transcript_reindex(file_id=1, file_uuid="file-b", user_id=7)

    assert first is True
    # Exactly ONE task queued for twenty mutations of the same file.
    task_mock.apply_async.assert_called_once()


def test_a_call_after_the_debounce_window_expires_queues_again(fake_redis):
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    task_mock = MagicMock()
    p1, p2 = _patched(fake_redis, task_mock)
    with p1, p2:
        dispatch_transcript_reindex(file_id=1, file_uuid="file-c", user_id=7)
        fake_redis.expire_now("transcript_reindex_debounce:file-c")
        second = dispatch_transcript_reindex(file_id=1, file_uuid="file-c", user_id=7)

    assert second is True
    assert task_mock.apply_async.call_count == 2


def test_different_files_are_debounced_independently(fake_redis):
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    task_mock = MagicMock()
    p1, p2 = _patched(fake_redis, task_mock)
    with p1, p2:
        a = dispatch_transcript_reindex(file_id=1, file_uuid="file-d", user_id=7)
        b = dispatch_transcript_reindex(file_id=2, file_uuid="file-e", user_id=7)

    assert a is True
    assert b is True
    assert task_mock.apply_async.call_count == 2


def test_a_redis_failure_degrades_to_no_dispatch_rather_than_raising():
    """A dispatch failure must never turn a successful edit into a failed request."""
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    with patch("app.core.redis.get_redis", side_effect=RuntimeError("redis is down")):
        queued = dispatch_transcript_reindex(file_id=1, file_uuid="file-f", user_id=7)

    assert queued is False
