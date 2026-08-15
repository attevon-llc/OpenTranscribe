"""The download-prepare dispatch guard must be released when the task finishes.

``_ensure_prepare_enqueued`` sets ``download:prep:{file_id}:{mode}`` with ``NX`` so a
double-click, a POST plus its SSE stream, and multiple tabs collapse into ONE worker
task. It carries a 900 s expiry as a backstop — but nothing released it when the task
finished, so the expiry became the actual lifetime.

That made a download unrecoverable for 15 minutes: readiness cannot be resolved, ``NX``
refuses to re-dispatch because the guard is still set, so
``GET /files/{uuid}/download-stream`` waits on an event nobody will ever publish and the
browser hangs until it gives up. Reachable in production exactly because derived assets
are deliberately short-lived — one expiring inside the guard window is enough.

Found by an E2E test that looked flaky and wasn't: it failed 3/3 in isolation and passed
in the suite only because an earlier test left the asset already prepared, letting it take
the ``status == "ready"`` fast path. Measured: the same request took **90 s and failed**
with the stale guard held, and **6.35 s and passed** once it expired (issue #431).

These are unit tests over a fake Redis — no broker, no worker, no ffmpeg.
"""

from __future__ import annotations

import pytest

from app.services import download_events
from tests.helpers import does_not_raise


class _FakeRedis:
    """Minimal Redis stand-in supporting the guard's `set(nx=, ex=)` / `delete`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.delete_calls: list[str] = []

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key: str) -> int:
        self.delete_calls.append(key)
        return int(self.store.pop(key, None) is not None)


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    client = _FakeRedis()
    monkeypatch.setattr(download_events, "get_redis", lambda: client)
    return client


def test_the_guard_key_is_scoped_per_file_and_mode(fake_redis):
    """Two modes of the same file must not block each other."""
    mp3 = download_events.download_prep_guard_key(7, "audio_mp3")
    wav = download_events.download_prep_guard_key(7, "audio_wav")
    other_file = download_events.download_prep_guard_key(8, "audio_mp3")

    assert mp3 != wav != other_file
    assert mp3 == "download:prep:7:audio_mp3"


def test_releasing_the_guard_lets_the_next_request_dispatch(fake_redis):
    """The regression test: after release, `NX` must succeed again.

    Without the release this is exactly the stuck state -- the second `set(nx=True)`
    returns None, so no task is dispatched and the SSE stream waits forever.
    """
    key = download_events.download_prep_guard_key(42, "audio_mp3")

    assert fake_redis.set(key, "1", nx=True, ex=900) is True, "first dispatch must win"
    assert fake_redis.set(key, "1", nx=True, ex=900) is None, "the guard must dedupe"

    download_events.release_download_prep_guard(42, "audio_mp3")

    assert fake_redis.set(key, "1", nx=True, ex=900) is True, (
        "after the task finishes the guard must be gone, or the download is "
        "unrecoverable until the 900s expiry"
    )


def test_releasing_an_absent_guard_is_harmless(fake_redis):
    """Runs in a `finally`, so it must tolerate paths that never set the guard."""
    download_events.release_download_prep_guard(99, "audio_wav")
    assert fake_redis.delete_calls == ["download:prep:99:audio_wav"]


def test_a_redis_failure_during_release_is_swallowed(monkeypatch):
    """It runs in a `finally` on a task that may have SUCCEEDED.

    Letting this raise would turn a completed preparation into a failed task and lose
    the presigned URL that was already published.
    """

    class _Broken:
        def delete(self, key):
            raise ConnectionError("redis gone")

    monkeypatch.setattr(download_events, "get_redis", lambda: _Broken())

    # does_not_raise, not a bare call: "it did not raise" is only an assertion when
    # written as one, and the reason is mandatory there so it cannot decay into a
    # silent pass. scripts/audit-tests.py flagged the bare version as no-assertion.
    with does_not_raise("a guard-release failure must not fail an already-successful task"):
        download_events.release_download_prep_guard(1, "audio_mp3")


def test_the_prepare_task_releases_the_guard_even_when_it_fails(fake_redis, monkeypatch):
    """The `finally` must fire on the error path too, not just on success.

    A failed prepare that kept the guard would block every retry for 15 minutes --
    turning one transient ffmpeg error into a quarter-hour outage for that download.
    """
    from app.tasks import media_download

    key = download_events.download_prep_guard_key(5, "audio_mp3")
    fake_redis.set(key, "1", nx=True, ex=900)

    monkeypatch.setattr(
        media_download, "release_download_prep_guard", download_events.release_download_prep_guard
    )

    def _explode(*_args, **_kwargs):
        raise RuntimeError("session unavailable")

    monkeypatch.setattr(media_download, "session_scope", _explode)

    result = media_download.prepare_media_download_task.run(file_id=5, user_id=1, mode="audio_mp3")

    assert result["status"] == "error"
    assert key in fake_redis.delete_calls, "the guard must be released on the failure path"
