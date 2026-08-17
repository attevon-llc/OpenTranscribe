"""``app/utils/benchmark_timing.py`` — the wall-clock instrumentation helpers.

Every writer is gated on ``ENABLE_BENCHMARK_TIMING`` via ``_enabled()``, which is
``lru_cache``d — one env read per process. Tests that flip the flag must clear
that cache themselves (``_reset_enabled_cache`` fixture below), or the value
leaks into whichever test runs next.

Redis is a genuinely out-of-process seam, so it's mocked here (matching
``test_chat_limits.py``'s convention); the DB-touching half of
``capture_queue_depth`` runs against the real ``db_session`` savepoint.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.utils import benchmark_timing


@pytest.fixture(autouse=True)
def _reset_enabled_cache(monkeypatch):
    """Isolate the ``lru_cache``d env-flag read between tests."""
    benchmark_timing._enabled.cache_clear()
    benchmark_timing._COLD_STATE.clear()
    yield
    benchmark_timing._enabled.cache_clear()
    benchmark_timing._COLD_STATE.clear()


def _enable(monkeypatch):
    monkeypatch.setenv("ENABLE_BENCHMARK_TIMING", "true")
    benchmark_timing._enabled.cache_clear()


def _disable(monkeypatch):
    monkeypatch.delenv("ENABLE_BENCHMARK_TIMING", raising=False)
    benchmark_timing._enabled.cache_clear()


def _redis() -> MagicMock:
    client = MagicMock()
    pipe = MagicMock()
    client.pipeline.return_value = pipe
    return client


# ---------------------------------------------------------------------------
# _enabled / benchmark_enabled
# ---------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    _disable(monkeypatch)
    assert benchmark_timing.benchmark_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on", "ON"])
def test_truthy_env_values_enable(monkeypatch, value):
    monkeypatch.setenv("ENABLE_BENCHMARK_TIMING", value)
    benchmark_timing._enabled.cache_clear()
    assert benchmark_timing.benchmark_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "garbage", ""])
def test_falsy_env_values_disable(monkeypatch, value):
    monkeypatch.setenv("ENABLE_BENCHMARK_TIMING", value)
    benchmark_timing._enabled.cache_clear()
    assert benchmark_timing.benchmark_enabled() is False


# ---------------------------------------------------------------------------
# mark
# ---------------------------------------------------------------------------


def test_mark_noop_when_task_id_falsy(monkeypatch, caplog):
    _enable(monkeypatch)
    client = _redis()
    with (
        patch("app.core.redis.get_redis", return_value=client),
        caplog.at_level("DEBUG", logger="app.utils.benchmark_timing"),
    ):
        benchmark_timing.mark(None, "ffmpeg_start")
        benchmark_timing.mark("", "ffmpeg_start")
    # No task_id means nothing to instrument — still a normal (non-raising) void return.
    client.pipeline.assert_not_called()
    # A real early return, not a caught exception with the same external shape.
    assert caplog.records == []


def test_mark_noop_when_disabled(monkeypatch, caplog):
    _disable(monkeypatch)
    client = _redis()
    with (
        patch("app.core.redis.get_redis", return_value=client),
        caplog.at_level("DEBUG", logger="app.utils.benchmark_timing"),
    ):
        benchmark_timing.mark("task-1", "ffmpeg_start")
    client.pipeline.assert_not_called()
    assert caplog.records == []


def test_mark_writes_hash_field_and_sets_ttl(monkeypatch):
    _enable(monkeypatch)
    client = _redis()
    with patch("app.core.redis.get_redis", return_value=client):
        benchmark_timing.mark("task-1", "ffmpeg_start", value=1000.5)

    # The module docstring promises the 24h dispatch.py convention — pin the real value,
    # not just that *some* TTL was passed to the mock.
    assert benchmark_timing.BENCHMARK_HASH_TTL_SECONDS == 24 * 60 * 60

    pipe = client.pipeline.return_value
    pipe.hset.assert_called_once_with("benchmark:task-1", "ffmpeg_start", "1000.5")
    pipe.expire.assert_called_once_with(
        "benchmark:task-1", benchmark_timing.BENCHMARK_HASH_TTL_SECONDS
    )
    pipe.execute.assert_called_once()


def test_mark_defaults_to_current_time(monkeypatch):
    _enable(monkeypatch)
    client = _redis()
    with (
        patch("app.core.redis.get_redis", return_value=client),
        patch("time.time", return_value=42.0),
    ):
        benchmark_timing.mark("task-1", "stage_start")

    pipe = client.pipeline.return_value
    # The key format is real business logic (shared with fetch_all's read side),
    # not a fact about the mock — assert it directly, not just what was called with.
    assert benchmark_timing._hash_key("task-1") == "benchmark:task-1"
    pipe.hset.assert_called_once_with("benchmark:task-1", "stage_start", "42.0")


def test_mark_swallows_redis_failure(monkeypatch, caplog):
    _enable(monkeypatch)
    with (
        patch("app.core.redis.get_redis", side_effect=ConnectionError("down")),
        caplog.at_level("DEBUG", logger="app.utils.benchmark_timing"),
    ):
        # Must not raise: instrumentation failures can never take down the pipeline.
        benchmark_timing.mark("task-1", "ffmpeg_start")

    # Prove the failure was actually hit and swallowed, not skipped entirely.
    assert any(
        "benchmark mark 'ffmpeg_start' for task-1 failed" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# mark_many
# ---------------------------------------------------------------------------


def test_mark_many_stringifies_non_string_values(monkeypatch):
    _enable(monkeypatch)
    client = _redis()
    with patch("app.core.redis.get_redis", return_value=client):
        benchmark_timing.mark_many("task-1", {"count": 3, "label": "already-a-string"})

    pipe = client.pipeline.return_value
    # The key format is real business logic, not a fact about the mock.
    assert benchmark_timing._hash_key("task-1") == "benchmark:task-1"
    pipe.hset.assert_called_once_with(
        "benchmark:task-1", mapping={"count": "3", "label": "already-a-string"}
    )


def test_mark_many_noop_on_empty_dict(monkeypatch, caplog):
    _enable(monkeypatch)
    client = _redis()
    with (
        patch("app.core.redis.get_redis", return_value=client),
        caplog.at_level("DEBUG", logger="app.utils.benchmark_timing"),
    ):
        benchmark_timing.mark_many("task-1", {})
    assert caplog.records == []
    client.pipeline.assert_not_called()


def test_mark_many_noop_when_disabled(monkeypatch, caplog):
    _disable(monkeypatch)
    client = _redis()
    with (
        patch("app.core.redis.get_redis", return_value=client),
        caplog.at_level("DEBUG", logger="app.utils.benchmark_timing"),
    ):
        benchmark_timing.mark_many("task-1", {"a": 1})
    client.pipeline.assert_not_called()
    assert caplog.records == []


# ---------------------------------------------------------------------------
# stage
# ---------------------------------------------------------------------------


def test_stage_emits_start_and_end_markers(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        benchmark_timing, "mark", lambda task_id, name, value=None: calls.append((task_id, name))
    )

    with benchmark_timing.stage("task-1", "ffmpeg"):
        pass

    assert [name for _, name in calls] == ["ffmpeg_start", "ffmpeg_end"]
    assert {task_id for task_id, _ in calls} == {"task-1"}


def test_stage_still_marks_end_on_exception(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        benchmark_timing, "mark", lambda task_id, name, value=None: calls.append(name)
    )

    with pytest.raises(ValueError, match="boom"):
        with benchmark_timing.stage("task-1", "ffmpeg"):
            raise ValueError("boom")

    assert calls == ["ffmpeg_start", "ffmpeg_end"]


# ---------------------------------------------------------------------------
# mark_cold_start
# ---------------------------------------------------------------------------


def test_mark_cold_start_first_call_is_cold(monkeypatch):
    _enable(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        benchmark_timing, "mark_many", lambda task_id, markers: calls.append(markers)
    )

    benchmark_timing.mark_cold_start("task-1", "gpu")

    assert calls == [{"gpu_worker_cold": "true"}]


def test_mark_cold_start_second_call_is_not_cold(monkeypatch):
    _enable(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        benchmark_timing, "mark_many", lambda task_id, markers: calls.append(markers)
    )

    benchmark_timing.mark_cold_start("task-1", "gpu")
    benchmark_timing.mark_cold_start("task-2", "gpu")

    assert calls == [{"gpu_worker_cold": "true"}, {"gpu_worker_cold": "false"}]


def test_mark_cold_start_tracks_per_worker_key(monkeypatch):
    _enable(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        benchmark_timing, "mark_many", lambda task_id, markers: calls.append(markers)
    )

    benchmark_timing.mark_cold_start("task-1", "gpu")
    benchmark_timing.mark_cold_start("task-1", "cpu")

    assert calls == [{"gpu_worker_cold": "true"}, {"cpu_worker_cold": "true"}]


# ---------------------------------------------------------------------------
# set_context
# ---------------------------------------------------------------------------


def test_set_context_drops_none_values(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        benchmark_timing, "mark_many", lambda task_id, markers: calls.append(markers)
    )

    benchmark_timing.set_context(
        "task-1", {"audio_duration_s": 12.5, "whisper_model": None, "gpu_device": "cuda:0"}
    )

    assert calls == [{"audio_duration_s": 12.5, "gpu_device": "cuda:0"}]


# ---------------------------------------------------------------------------
# record_retry
# ---------------------------------------------------------------------------


def test_record_retry_first_attempt_creates_list(monkeypatch):
    _enable(monkeypatch)
    client = _redis()
    client.hget.return_value = None
    with patch("app.core.redis.get_redis", return_value=client):
        benchmark_timing.record_retry("task-1", stage="preprocess", attempt=1, start=10.0, end=12.5)

    pipe = client.pipeline.return_value
    (key, field, value), _ = pipe.hset.call_args_list[0]
    assert key == "benchmark:task-1"
    assert field == "per_retry_timings"
    stored = json.loads(value)
    assert stored == [{"stage": "preprocess", "attempt": 1, "start_ms": 10000, "end_ms": 12500}]

    pipe.hset.assert_any_call("benchmark:task-1", "retry_count", "1")


def test_record_retry_appends_to_existing_list(monkeypatch):
    _enable(monkeypatch)
    client = _redis()
    existing = [{"stage": "preprocess", "attempt": 1, "start_ms": 0, "end_ms": 1000}]
    client.hget.return_value = json.dumps(existing).encode()
    with patch("app.core.redis.get_redis", return_value=client):
        benchmark_timing.record_retry(
            "task-1", stage="preprocess", attempt=2, start=1.0, end=3.0, error="timed out"
        )

    pipe = client.pipeline.return_value
    (_, _, value), _ = pipe.hset.call_args_list[0]
    stored = json.loads(value)
    assert len(stored) == 2
    assert stored[1] == {
        "stage": "preprocess",
        "attempt": 2,
        "start_ms": 1000,
        "end_ms": 3000,
        "error": "timed out",
    }
    pipe.hset.assert_any_call("benchmark:task-1", "retry_count", "2")


def test_record_retry_truncates_long_error(monkeypatch):
    _enable(monkeypatch)
    client = _redis()
    client.hget.return_value = None
    long_error = "x" * 500
    with patch("app.core.redis.get_redis", return_value=client):
        benchmark_timing.record_retry(
            "task-1", stage="preprocess", attempt=1, start=0.0, end=1.0, error=long_error
        )

    pipe = client.pipeline.return_value
    (_, _, value), _ = pipe.hset.call_args_list[0]
    stored = json.loads(value)
    assert len(stored[0]["error"]) == 240


def test_record_retry_recovers_from_corrupt_existing_json(monkeypatch):
    """A non-JSON or non-list existing value must not crash the write — start fresh."""
    _enable(monkeypatch)
    client = _redis()
    client.hget.return_value = b"not-json{{{"
    with patch("app.core.redis.get_redis", return_value=client):
        benchmark_timing.record_retry("task-1", stage="preprocess", attempt=1, start=0.0, end=1.0)

    pipe = client.pipeline.return_value
    (_, _, value), _ = pipe.hset.call_args_list[0]
    assert len(json.loads(value)) == 1


def test_record_retry_noop_when_disabled(monkeypatch, caplog):
    _disable(monkeypatch)
    client = _redis()
    with (
        patch("app.core.redis.get_redis", return_value=client),
        caplog.at_level("DEBUG", logger="app.utils.benchmark_timing"),
    ):
        benchmark_timing.record_retry("task-1", stage="preprocess", attempt=1, start=0.0, end=1.0)
    client.pipeline.assert_not_called()
    assert caplog.records == []


# ---------------------------------------------------------------------------
# fetch_all
# ---------------------------------------------------------------------------


def test_fetch_all_decodes_bytes_keys_and_values():
    client = MagicMock()
    client.hgetall.return_value = {b"ffmpeg_start": b"1000.0", b"ffmpeg_end": b"1002.5"}
    with patch("app.core.redis.get_redis", return_value=client):
        result = benchmark_timing.fetch_all("task-1")

    assert result == {"ffmpeg_start": "1000.0", "ffmpeg_end": "1002.5"}


def test_fetch_all_empty_on_miss():
    client = MagicMock()
    client.hgetall.return_value = {}
    with patch("app.core.redis.get_redis", return_value=client):
        assert benchmark_timing.fetch_all("nonexistent-task") == {}


def test_fetch_all_not_gated_on_disabled_flag(monkeypatch):
    """Unlike the writers, fetch_all always reads — consumers need it regardless of the flag."""
    _disable(monkeypatch)
    client = MagicMock()
    client.hgetall.return_value = {b"x": b"1"}
    with patch("app.core.redis.get_redis", return_value=client):
        assert benchmark_timing.fetch_all("task-1") == {"x": "1"}


def test_fetch_all_swallows_redis_failure():
    with patch("app.core.redis.get_redis", side_effect=ConnectionError("down")):
        assert benchmark_timing.fetch_all("task-1") == {}


# ---------------------------------------------------------------------------
# capture_queue_depth
# ---------------------------------------------------------------------------


def test_capture_queue_depth_noop_when_disabled(monkeypatch, caplog):
    _disable(monkeypatch)
    client = _redis()
    with (
        patch("app.core.redis.get_redis", return_value=client),
        caplog.at_level("DEBUG", logger="app.utils.benchmark_timing"),
    ):
        benchmark_timing.capture_queue_depth("task-1")
    client.pipeline.assert_not_called()
    assert caplog.records == []


def test_capture_queue_depth_writes_json_depths_and_in_flight_count(
    monkeypatch, db_session, normal_user
):
    _enable(monkeypatch)
    import contextlib

    from app.core.constants import CeleryQueues

    monkeypatch.setattr(
        "app.db.session_utils.session_scope",
        lambda: contextlib.nullcontext(db_session),
    )

    processing = MediaFile(
        user_id=normal_user.id,
        filename="in-flight.wav",
        storage_path="benchmark-test/in-flight.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.PROCESSING,
    )
    completed = MediaFile(
        user_id=normal_user.id,
        filename="done.wav",
        storage_path="benchmark-test/done.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.COMPLETED,
    )
    db_session.add_all([processing, completed])
    db_session.flush()

    client = _redis()
    pipe = client.pipeline.return_value
    pipe.execute.return_value = [1] * len(CeleryQueues.ALL)

    with patch("app.core.redis.get_redis", return_value=client):
        benchmark_timing.capture_queue_depth("task-1")

    calls = pipe.hset.call_args_list
    depth_call = next(
        c
        for c in calls
        if "mapping" in c.kwargs and "queue_depth_at_dispatch" in c.kwargs["mapping"]
    )
    depths_payload = json.loads(depth_call.kwargs["mapping"]["queue_depth_at_dispatch"])
    assert set(depths_payload) == set(CeleryQueues.ALL)
    assert all(v == 1 for v in depths_payload.values())

    concurrent_call = next(
        c
        for c in calls
        if "mapping" in c.kwargs and "concurrent_files_at_dispatch" in c.kwargs["mapping"]
    )
    assert concurrent_call.kwargs["mapping"]["concurrent_files_at_dispatch"] == "1"


def test_capture_queue_depth_survives_llen_failure(monkeypatch, caplog):
    _enable(monkeypatch)
    client = _redis()
    pipe = client.pipeline.return_value
    pipe.execute.side_effect = ConnectionError("redis down")

    with (
        patch("app.core.redis.get_redis", return_value=client),
        caplog.at_level("DEBUG", logger="app.utils.benchmark_timing"),
    ):
        # Must not raise even though the LLEN pipeline blew up.
        benchmark_timing.capture_queue_depth("task-1", queues=["gpu"])

    # Prove the failure was actually hit and swallowed, not skipped entirely.
    assert any("queue depth LLEN failed" in r.message for r in caplog.records)
