"""Characterization tests for ``app/tasks/embedding_consistency_repair.py``.

This module is the GPU-batch half of the embedding-consistency repair job: it
re-extracts and writes speaker embeddings for speakers OpenSearch is missing.
Its two result writers (``_v3_result_writer`` / ``_v4_result_writer``) are the last
place a bad vector could be caught before it lands in a kNN index used for speaker
matching, and ``_update_repair_progress`` is the only place multiple concurrent GPU
batch workers reconcile their counters. It had no tests.

Pinned here, in order:

1. **The zero-vector write (L52-183) — FIXED, not an open defect.** A single embedding is
   L2-normalized as-is; multiple embeddings are averaged with ``np.mean`` and THEN
   normalized. Both writers now compute the norm once, and when it is at or below
   ``_ZERO_NORM_EPSILON`` (the averaged vector's embeddings cancelled out) they log a
   ``logger.warning`` naming the speaker/file and SKIP the write entirely — no vector is
   sent to OpenSearch — instead of writing a degenerate ``[0.0, ...]`` embedding that would
   produce meaningless cosine-similarity scores for that speaker in every future search.
   ``test_v3_writer_skips_write_and_warns_when_embeddings_cancel_out`` asserts NO write call
   happened and that a warning naming the speaker uuid and file was logged; the write path
   for a normal (non-degenerate) averaged vector is covered by test 2 below, so the
   zero-norm skip does not silently blind the suite to the normalize-and-write branch. The
   skip is also reflected correctly downstream: ``_v3_result_writer``/``_v4_result_writer``
   only increment ``written`` for embeddings actually sent to OpenSearch, so
   ``_run_repair_phase``'s ``last_write_count`` closure (see item 4) and the batch task's
   per-file progress bookkeeping see a skipped speaker as zero embeddings written for that
   file, same as any other writer returning 0 — no separate accounting path was needed.
2. **``_v3_result_writer`` vs ``_v4_result_writer`` duplication (L42-101 / L104-154).**
   The normalize/average logic is copy-pasted between the two functions, differing only in
   which OpenSearch function they call and whether ``target_index`` is accepted. A fix to
   one (e.g. the zero-vector case above) is not automatically applied to the other.
   ``test_v3_and_v4_writers_produce_identical_aggregation_for_the_same_input`` locks the
   two paths to producing the same output today, so future drift between them fails a test
   instead of shipping silently. This is a maintainability finding, not a behavior
   assertion about which one is "right" — flagged in the docstring, production code left
   untouched.
3. **``_update_repair_progress``'s WATCH/MULTI retry loop (L190-276) — FIXED, not an open
   defect.** ``except redis.WatchError: continue`` now retries ONLY the specific exception
   ``redis-py`` raises when another writer touched ``_REDIS_PROGRESS_KEY`` between
   ``watch()`` and ``execute()`` (confirmed against the pinned ``redis>=8.0.1`` — it is
   ``redis.exceptions.WatchError``, aliased at the package top level as ``redis.WatchError``;
   no other module in this repo used it before this fix). Any other exception — a malformed
   stored value blowing up ``json.loads``, for example — now PROPAGATES immediately instead
   of being retried up to 5 times and then silently dropped by the ``for``/``else`` fallback.
   ``test_update_repair_progress_propagates_non_lock_conflict_exceptions_immediately`` forces
   the exact same ``json.JSONDecodeError`` scenario the old characterization test pinned as a
   silent drop, and now asserts it raises out of ``_update_repair_progress`` on the FIRST
   attempt (no retries).
   ``test_update_repair_progress_retries_on_genuine_watch_error`` is the case that must keep
   working: a fake pipeline that raises real ``redis.WatchError`` on its first N calls and
   then succeeds still applies the caller's increment once the lock stops contending.
4. **``_run_repair_phase``'s closure-based ``last_write_count`` (L251-306) — a regression
   guard for currently-safe behavior, not a defect.** The docstring at L273-275 claims this
   is safe "because ``process_batch_pipelined`` processes files sequentially: writer runs,
   then ``on_success``, before moving to the next file." ``test_run_repair_phase_attributes_write_counts_to_the_correct_file``
   drives a fake ``process_batch_pipelined`` across 2 files with DIFFERENT write counts and
   asserts each file's ``file_written`` entry matches its own count — pinning today's
   correct attribution so any future change to the closure (or to
   ``process_batch_pipelined``'s sequencing) that breaks it is caught.
5. **``_check_repair_completion``'s user_id fallback (L462-474).**
   ``progress.get("user_id", user_id)`` falls back to the caller's passed default whenever
   the stored progress dict has no ``"user_id"`` key. The only caller
   (``speaker_embedding_consistency_repair_batch_task``, L350-355) passes its own
   best-effort ``notify_user_id``, which itself defaults to ``1`` unless a ``user_id`` was
   found in the SAME Redis key moments earlier. ``test_check_repair_completion_falls_back_to_the_passed_user_id_when_progress_lacks_it``
   pins that the completion notification (WebSocket event + ``ProgressTracker``) goes to
   whatever default the caller passed — ``1`` — when the stored progress never recorded a
   real owner.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest
import redis

from app.tasks.embedding_consistency_repair import _check_repair_completion
from app.tasks.embedding_consistency_repair import _run_repair_phase
from app.tasks.embedding_consistency_repair import _update_repair_progress
from app.tasks.embedding_consistency_repair import _v3_result_writer
from app.tasks.embedding_consistency_repair import _v4_result_writer
from app.tasks.migration_pipeline import PreparedFile
from app.tasks.migration_pipeline import SpeakerSnapshot

_PROGRESS_KEY = "embedding_consistency_progress"


def _sr(speaker_id: int, value: np.ndarray) -> Any:
    """Stand-in for ``SegmentResult``: only ``.speaker_id`` / ``.value`` are read."""
    from types import SimpleNamespace

    return SimpleNamespace(speaker_id=speaker_id, value=value)


def _prepared(file_uuid: str, speakers: list[SpeakerSnapshot], **kw: Any) -> PreparedFile:
    return PreparedFile(
        file_uuid=file_uuid,
        audio_source="s3://bucket/key",
        speakers=speakers,
        speaker_segments={},
        media_file_id=kw.pop("media_file_id", 1),
        user_id=kw.pop("user_id", 1),
    )


class _WriteRecorder:
    """Stands in for ``add_speaker_embedding`` / ``add_speaker_embedding_v4``: records the
    exact kwargs (including the embedding vector) each call received, so tests can assert
    on the real captured VALUES rather than only ``mock.assert_called``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"result": "created"}


# ---------------------------------------------------------------------------
# 1. Zero-vector write
# ---------------------------------------------------------------------------


def test_v3_writer_skips_write_and_warns_when_embeddings_cancel_out(caplog):
    speaker = SpeakerSnapshot(id=1, uuid="spk-1", name="Speaker A")
    prepared = _prepared("f1", [speaker], media_file_id=42, user_id=7)
    emb1 = np.array([1.0, 2.0, 3.0])
    emb2 = np.array([-1.0, -2.0, -3.0])
    results_by_model = {"embedding": [_sr(1, emb1), _sr(1, emb2)]}

    recorder = _WriteRecorder()
    with (
        patch("app.services.opensearch_service.add_speaker_embedding", recorder),
        caplog.at_level("WARNING", logger="app.tasks.embedding_consistency_repair"),
    ):
        written = _v3_result_writer(prepared, results_by_model, {"spk-1"})

    # Fixed behavior: the degenerate (zero-norm) vector is never written.
    assert written == 0
    assert len(recorder.calls) == 0
    assert any("spk-1" in r.message and "f1" in r.message for r in caplog.records)


def test_v4_writer_also_skips_write_and_warns_when_embeddings_cancel_out(caplog):
    speaker = SpeakerSnapshot(id=2, uuid="spk-2", name="Speaker C")
    prepared = _prepared("f3", [speaker])
    results_by_model = {"embedding": [_sr(2, np.array([5.0, -1.0])), _sr(2, np.array([-5.0, 1.0]))]}

    recorder = _WriteRecorder()
    with (
        patch("app.services.opensearch_service.add_speaker_embedding_v4", recorder),
        caplog.at_level("WARNING", logger="app.tasks.embedding_consistency_repair"),
    ):
        written = _v4_result_writer(prepared, results_by_model, {"spk-2"})

    assert written == 0
    assert len(recorder.calls) == 0
    assert any("spk-2" in r.message and "f3" in r.message for r in caplog.records)


def test_v3_writer_still_writes_a_near_zero_but_nonzero_norm_vector():
    """Norm just above the epsilon floor must still be normalized and written --
    only a norm at/below ``_ZERO_NORM_EPSILON`` is treated as degenerate."""
    speaker = SpeakerSnapshot(id=3, uuid="spk-3", name="Speaker D")
    prepared = _prepared("f4", [speaker])
    # A single embedding with a tiny but well-above-epsilon norm.
    results_by_model = {"embedding": [_sr(3, np.array([1e-4, 0.0, 0.0]))]}

    recorder = _WriteRecorder()
    with patch("app.services.opensearch_service.add_speaker_embedding", recorder):
        written = _v3_result_writer(prepared, results_by_model, {"spk-3"})

    assert written == 1
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["embedding"] == pytest.approx([1.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# 2. v3/v4 duplication -- parity guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "embeddings",
    [
        [np.array([3.0, 4.0, 0.0])],  # single-embedding branch (L75-79 / L130-134)
        [np.array([3.0, 4.0, 0.0]), np.array([0.0, 4.0, 3.0])],  # averaged branch
    ],
)
def test_v3_and_v4_writers_produce_identical_aggregation_for_the_same_input(embeddings):
    speaker = SpeakerSnapshot(id=5, uuid="spk-5", name="Speaker B")
    prepared = _prepared("f2", [speaker], media_file_id=99, user_id=3)
    results_by_model = {"embedding": [_sr(5, emb) for emb in embeddings]}

    v3_recorder = _WriteRecorder()
    v4_recorder = _WriteRecorder()
    with patch("app.services.opensearch_service.add_speaker_embedding", v3_recorder):
        v3_written = _v3_result_writer(prepared, results_by_model, {"spk-5"})
    with patch("app.services.opensearch_service.add_speaker_embedding_v4", v4_recorder):
        v4_written = _v4_result_writer(prepared, results_by_model, {"spk-5"})

    assert v3_written == v4_written == 1
    v3_embedding = v3_recorder.calls[0]["embedding"]
    v4_embedding = v4_recorder.calls[0]["embedding"]
    assert v3_embedding == pytest.approx(v4_embedding)
    assert v3_recorder.calls[0]["segment_count"] == v4_recorder.calls[0]["segment_count"]
    # Confirms real normalization happened (not a coincidental match on raw vectors).
    assert np.linalg.norm(v3_embedding) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 3. _update_repair_progress -- WATCH/MULTI retry loop
# ---------------------------------------------------------------------------


class _ExplodingPipe:
    """A pipeline whose ``.get()`` always raises a non-``WatchError`` exception -- e.g.
    what a malformed stored value would do to ``json.loads`` inside the real retry loop.
    This must NOT be caught by the narrowed ``except redis.WatchError`` -- it should
    propagate out of ``_update_repair_progress`` on the first attempt."""

    def watch(self, key: str) -> None:
        pass

    def get(self, key: str) -> str:
        raise json.JSONDecodeError("boom", "corrupt", 0)

    def unwatch(self) -> None:
        pass

    def multi(self) -> None:
        pass

    def execute(self) -> None:
        pass


class _NonLockConflictExplodingRedis:
    """Fake Redis client: plain ``get``/``set`` work normally, but every ``pipeline()``
    call hands back a pipe that explodes with a non-``WatchError`` exception on ``.get()``."""

    def __init__(self, initial: dict[str, Any]) -> None:
        self.store: dict[str, str] = {_PROGRESS_KEY: json.dumps(initial)}
        self.pipeline_attempts = 0

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def pipeline(self, transaction: bool = True) -> _ExplodingPipe:
        self.pipeline_attempts += 1
        return _ExplodingPipe()


class _WatchErrorThenSucceedsPipe:
    """A pipeline that raises a REAL ``redis.WatchError`` on ``.get()`` for the first
    ``fail_count`` construction attempts, then behaves like a normal successful
    WATCH/MULTI/EXEC round-trip -- the genuine "another writer touched the key while
    we held WATCH" scenario the retry loop exists to handle."""

    def __init__(self, redis_client: _WatchErrorThenSucceedsRedis) -> None:
        self._client = redis_client

    def watch(self, key: str) -> None:
        pass

    def get(self, key: str) -> str | None:
        self._client.pipeline_attempts += 1
        if self._client.pipeline_attempts <= self._client.fail_count:
            raise redis.WatchError("watched key modified")
        return self._client.store.get(key)

    def unwatch(self) -> None:
        pass

    def multi(self) -> None:
        pass

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._pending = (key, value)

    def execute(self) -> None:
        if getattr(self, "_pending", None) is not None:
            key, value = self._pending
            self._client.store[key] = value


class _WatchErrorThenSucceedsRedis:
    """Fake Redis client whose pipeline fails with genuine ``redis.WatchError`` a fixed
    number of times before succeeding -- the case the retry loop must keep handling."""

    def __init__(self, initial: dict[str, Any], fail_count: int) -> None:
        self.store: dict[str, str] = {_PROGRESS_KEY: json.dumps(initial)}
        self.fail_count = fail_count
        self.pipeline_attempts = 0

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def pipeline(self, transaction: bool = True) -> _WatchErrorThenSucceedsPipe:
        return _WatchErrorThenSucceedsPipe(self)


class _FakeProgressTracker:
    """Records construction kwargs; ``get_state`` always reports nothing pre-existing so
    the update path under test proceeds without an unrelated resume branch firing."""

    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakeProgressTracker.instances.append(kwargs)

    @staticmethod
    def get_state(*_a: Any, **_kw: Any) -> None:
        return None

    def resume_from_state(self, _state: Any) -> None:
        pass

    def complete(self, message: str = "") -> None:
        pass


def test_update_repair_progress_propagates_non_lock_conflict_exceptions_immediately():
    """Fixed behavior: a non-``WatchError`` exception (e.g. corrupted stored state
    triggering ``json.JSONDecodeError``) is NOT retried and NOT silently swallowed -- it
    raises straight out of ``_update_repair_progress`` on the first attempt."""
    initial = {
        "processed_files": 3,
        "repaired": 1,
        "failed_files": [],
        "running": True,
        "total_files": 10,
        "user_id": 9,
    }
    fake_redis = _NonLockConflictExplodingRedis(initial)

    with (
        patch(
            "app.tasks.embedding_consistency_repair.get_redis",
            return_value=fake_redis,
        ),
        patch(
            "app.services.progress_tracker.ProgressTracker",
            _FakeProgressTracker,
        ),
        pytest.raises(json.JSONDecodeError),
    ):
        _update_repair_progress(total_files=10, success=True, repaired_count=5, user_id=9)

    # Raised on the very first attempt -- no retrying of a non-lock-conflict exception.
    assert fake_redis.pipeline_attempts == 1


def test_update_repair_progress_retries_on_genuine_watch_error():
    """The case that must keep working: a real ``redis.WatchError`` (another writer
    touched the key while we held WATCH) is retried, and once the contention clears the
    caller's increment is actually applied -- not dropped."""
    initial = {
        "processed_files": 3,
        "repaired": 1,
        "failed_files": [],
        "running": True,
        "total_files": 10,
        "user_id": 9,
    }
    # Fails on the first 2 pipeline attempts with a genuine WatchError, succeeds on the 3rd.
    fake_redis = _WatchErrorThenSucceedsRedis(initial, fail_count=2)
    emitted: list[dict[str, Any]] = []

    def _fake_emit(**kwargs: Any) -> None:
        emitted.append(kwargs)

    with (
        patch(
            "app.tasks.embedding_consistency_repair.get_redis",
            return_value=fake_redis,
        ),
        patch(
            "app.services.progress_tracker.ProgressTracker",
            _FakeProgressTracker,
        ),
        patch(
            "app.services.progress_tracker.emit_progress_notification",
            _fake_emit,
        ),
    ):
        result = _update_repair_progress(total_files=10, success=True, repaired_count=5, user_id=9)

    # Retried through the WatchError attempts before succeeding on the 3rd.
    assert fake_redis.pipeline_attempts == 3
    # The caller's increment actually landed this time.
    assert result is not None
    assert result["processed_files"] == 4
    assert result["repaired"] == 6
    stored = json.loads(fake_redis.store[_PROGRESS_KEY])
    assert stored["processed_files"] == 4
    assert stored["repaired"] == 6
    assert emitted[0]["processed"] == 4
    assert emitted[0]["extra_data"]["repaired"] == 6


# ---------------------------------------------------------------------------
# 4. _run_repair_phase -- closure attribution under sequential processing
# ---------------------------------------------------------------------------


def _fake_process_batch_pipelined(
    prepared_files: list[tuple[str, PreparedFile]],
    runner: Any,
    result_writer: Any,
    is_running_check: Any,
    on_file_success: Any,
    on_file_failure: Any,
    min_duration: float = 0.0,
    io_workers: int = 1,
) -> tuple[int, int]:
    """Mirrors the real pipeline's documented sequencing: for each file, run the writer
    to completion, THEN call on_file_success, THEN move to the next file."""
    success = 0
    for fuuid, prep in prepared_files:
        result_writer(prep, {"embedding": []})
        on_file_success(fuuid)
        success += 1
    return success, 0


def test_run_repair_phase_attributes_write_counts_to_the_correct_file():
    files = [
        ("f1", _prepared("f1", [])),
        ("f2", _prepared("f2", [])),
        ("f3", _prepared("f3", [])),
    ]
    counts = {"f1": 3, "f2": 7, "f3": 0}

    def writer_fn(prep: PreparedFile, _results: Any, _target_uuids: set[str]) -> int:
        return counts[prep.file_uuid]

    file_written: dict[str, int] = {}

    with (
        patch(
            "app.services.speaker_embedding_service.get_cached_embedding_service",
            return_value=object(),
        ),
        patch(
            "app.services.speaker_analysis_models.EmbeddingModelAdapter",
            lambda service: object(),
        ),
        patch(
            "app.services.speaker_analysis_models.MultiModelRunner",
            lambda adapters: object(),
        ),
        patch(
            "app.tasks.migration_pipeline.process_batch_pipelined",
            _fake_process_batch_pipelined,
        ),
    ):
        success_count = _run_repair_phase(
            mode="v3",
            target_uuids=set(),
            result_writer_fn=writer_fn,
            files_with_speakers=files,
            file_written=file_written,
            is_running_check=lambda: True,
            batch_index=0,
        )

    assert success_count == 3
    # Each file's write count is its OWN count, not the previous or next file's --
    # the failure mode a shared/misused closure would produce is e.g. f1 getting 0
    # (the initial value) or f2's count leaking onto f1.
    assert file_written == {"f1": 3, "f2": 7, "f3": 0}


# ---------------------------------------------------------------------------
# 5. _check_repair_completion -- user_id fallback
# ---------------------------------------------------------------------------


class _SimpleRedis:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


def test_check_repair_completion_falls_back_to_the_passed_user_id_when_progress_lacks_it():
    progress = {
        "processed_files": 5,
        "total_files": 5,
        "repaired": 4,
        "unrepairable": 1,
        "failed_files": [],
        "start_time": 0.0,
        # deliberately no "user_id" key
    }
    fake_redis = _SimpleRedis({_PROGRESS_KEY: json.dumps(progress)})
    ws_calls: list[tuple[int, str, dict[str, Any]]] = []

    def _fake_send_ws_event(user_id: int, notification_type: str, data: dict[str, Any]) -> bool:
        ws_calls.append((user_id, notification_type, data))
        return True

    with (
        patch(
            "app.tasks.embedding_consistency_repair.get_redis",
            return_value=fake_redis,
        ),
        patch(
            "app.services.progress_tracker.ProgressTracker",
            _FakeProgressTracker,
        ),
        patch(
            "app.tasks.embedding_consistency_repair.send_ws_event",
            _fake_send_ws_event,
        ),
    ):
        # 1 is the real caller's own default (speaker_embedding_consistency_repair_batch_task
        # L352: notify_user_id = 1 unless a user_id was already found in this same key).
        _check_repair_completion(total_files=5, user_id=1)

    assert len(ws_calls) == 1
    assert ws_calls[0][0] == 1
    assert _FakeProgressTracker.instances[-1]["user_id"] == 1
