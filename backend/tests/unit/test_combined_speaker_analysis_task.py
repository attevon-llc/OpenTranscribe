"""Tests for ``app/tasks/combined_speaker_analysis_task.py`` (issue #474).

Live caller confirmed: dispatched from the admin "reprocess all speakers" migration flow
and from the online-ASR per-file path (module docstring); it is the batch/orchestrator
pair that runs both the embedding model and the gender model over the same extracted
audio in one pass, so a file's segments are only decoded once instead of twice.

Three things covered, following the ``test_speaker_attribute_migration_task.py``
convention of driving real DB state and a structural Redis fake rather than asserting
only on mock call args:

1. ``_combined_result_writer``'s own job — failure isolation between the two
   independent writers, and the two writers' counts summed — proven against a REAL
   ``Speaker`` row for the gender half (the real ``_gender_result_writer``, unpatched)
   while the embedding half is stubbed (OpenSearch is out of scope for this module).
2. ``migrate_speakers_combined_task`` — the orchestrator: already-running / lock-contention
   short-circuits, the zero-files skip, batch splitting (``_BATCH_SIZE = 25``) with the
   exact kwargs each batch is dispatched with, the Redis lock's lifecycle (acquired with
   ``nx``, always released in ``finally`` — including on an exception), and the announcing
   WebSocket event.
3. ``analyze_speakers_combined_batch_task`` — the "stopped mid-run" early exit, a file
   that fails to prepare being counted as a failure without aborting the batch, and the
   completion path (progress + the COMBINED_MIGRATION_COMPLETE WS event) once every file
   has been accounted for. GPU/model loading is out of scope (no GPU in this environment)
   — ``prepare_file``/the two service getters/``process_batch_pipelined`` are stubbed at
   their origin modules, the same seam-substitution technique
   ``test_speaker_attribute_migration_task.py`` uses for the identical pipeline.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid as uuid_module
from typing import Any

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.services.speaker_analysis_models import SegmentResult
from app.tasks import combined_speaker_analysis_task as cst
from app.tasks import embedding_migration_v4 as emv4
from app.tasks import speaker_attribute_migration_task as sat
from app.tasks.migration_pipeline import PreparedFile

# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------


def _media_file(db_session, user, **extra: Any) -> MediaFile:
    mf = MediaFile(
        uuid=uuid_module.uuid4(),
        user_id=user.id,
        filename=f"clip-{uuid_module.uuid4().hex[:8]}.mp4",
        storage_path=f"user_{user.id}/{uuid_module.uuid4().hex}.mp4",
        file_size=1024,
        content_type="video/mp4",
        status=FileStatus.COMPLETED,
        duration=60.0,
        **extra,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _speaker(db_session, media_file: MediaFile, user, name: str, **extra: Any) -> Speaker:
    sp = Speaker(user_id=user.id, media_file_id=media_file.id, name=name, **extra)
    db_session.add(sp)
    db_session.commit()
    db_session.refresh(sp)
    return sp


def _prepared(media_file: MediaFile, user) -> PreparedFile:
    return PreparedFile(
        file_uuid=str(media_file.uuid),
        audio_source="https://example/presigned",
        speakers=[],
        speaker_segments={},
        media_file_id=media_file.id,
        user_id=user.id,
    )


@contextlib.contextmanager
def _scope_yielding(db_session):
    yield db_session


class FakeRedis:
    """Structural stand-in covering everything ``MigrationProgressService`` and the
    orchestrator's direct ``get_redis()`` lock calls actually use: ``get``/``set`` (with
    ``nx``/``ex``)/``delete``/``ping``, plus a real ``eval`` that mirrors
    ``MigrationProgressService._INCREMENT_LUA`` closely enough to drive
    ``increment_processed`` correctly (json in, atomic-processed-count-increment
    semantics, json out) — real Redis's Lua script is not available in-process, so this
    reimplements just the one script this module depends on.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            self.delete_calls.append(k)
            if k in self.store:
                del self.store[k]
                n += 1
        return n

    def ping(self) -> bool:
        return True

    def eval(self, script: str, numkeys: int, *args: Any) -> int:
        # Issue #657, defect 6: the real Lua script now takes the TTL as a
        # 5th ARGV (MIGRATION_STATUS_TTL_SECONDS) instead of a bare literal.
        key, file_uuid, is_failure_flag, now, _ttl = args
        raw = self.store.get(key)
        if not raw:
            return 0
        status = json.loads(raw)
        status["processed_files"] = status.get("processed_files", 0) + 1
        status["last_updated"] = now
        if is_failure_flag == "1" and file_uuid:
            failed = status.get("failed_files", [])
            if file_uuid not in failed:
                failed.append(file_uuid)
            status["failed_files"] = failed
        self.store[key] = json.dumps(status)
        return int(status["processed_files"])


class _FakeUuidQuery:
    """Stands in for ``db.query(MediaFile.uuid).filter(...)`` — ``.filter()`` is a no-op
    (the orchestrator's own filter is not under test here) and ``.all()`` returns canned
    single-element row tuples, exactly the shape ``[str(r[0]) for r in rows]`` expects."""

    def __init__(self, rows: list[tuple[Any]]) -> None:
        self._rows = rows

    def filter(self, *_args: Any, **_kwargs: Any) -> _FakeUuidQuery:
        return self

    def all(self) -> list[tuple[Any]]:
        return self._rows


class _FakeCompletedFilesSession:
    """Only implements ``.query(MediaFile.uuid)`` — enough for
    ``migrate_speakers_combined_task``'s completed-files lookup.

    The orchestrator's query has no ``user_id`` scoping at all (it processes every
    COMPLETED file system-wide), so against the live dev stack "no completed files" and
    "exactly N completed files" are not constructible by simply not creating rows — the
    dev DB already has real completed files from real usage. This fake replaces the query
    result entirely, independent of whatever the live DB actually contains.
    """

    def __init__(self, uuids: list[str]) -> None:
        self._rows = [(u,) for u in uuids]

    def query(self, *_args: Any, **_kwargs: Any) -> _FakeUuidQuery:
        return _FakeUuidQuery(self._rows)


def _install_fake_redis(monkeypatch) -> FakeRedis:
    """Route BOTH Redis entry points the orchestrator/progress service use to one fake.

    ``migrate_speakers_combined_task`` calls ``get_redis()`` directly (module-level import)
    for the orchestrator lock; ``combined_migration_progress`` (a ``MigrationProgressService``
    instance) resolves its own client lazily via ``self._redis_client``. Both must point at
    the same fake or the lock and the progress status would disagree with each other.
    """
    fake = FakeRedis()
    monkeypatch.setattr(cst, "get_redis", lambda: fake)
    monkeypatch.setattr(cst.combined_migration_progress, "_redis_client", fake)
    return fake


# --------------------------------------------------------------------------------------
# 1. _combined_result_writer — failure isolation between the two independent writers
# --------------------------------------------------------------------------------------


def test_both_writers_succeed_and_their_counts_are_summed(monkeypatch, db_session, normal_user):
    mf = _media_file(db_session, normal_user)
    sp = _speaker(db_session, mf, normal_user, "SPEAKER_00")

    monkeypatch.setattr(emv4, "_embedding_result_writer", lambda prepared, results: 3)
    monkeypatch.setattr(sat, "session_scope", lambda: _scope_yielding(db_session))

    results = {
        "gender": [
            SegmentResult(model_name="gender", speaker_id=sp.id, value=("female", 0.8)),
        ]
    }

    total = cst._combined_result_writer(_prepared(mf, normal_user), results)

    assert total == 3 + 1  # embedding stub count + one real gender prediction written
    db_session.expire_all()
    refreshed = db_session.get(Speaker, sp.id)
    assert refreshed.predicted_gender == "female"


def test_embedding_writer_failure_does_not_prevent_the_gender_write(
    monkeypatch, db_session, normal_user, caplog
):
    """The module docstring's own claim: 'a failure in one does not prevent the other'."""
    mf = _media_file(db_session, normal_user)
    sp = _speaker(db_session, mf, normal_user, "SPEAKER_00")

    def _boom_embedding(prepared, results):
        raise RuntimeError("opensearch unreachable")

    monkeypatch.setattr(emv4, "_embedding_result_writer", _boom_embedding)
    monkeypatch.setattr(sat, "session_scope", lambda: _scope_yielding(db_session))

    results = {
        "gender": [SegmentResult(model_name="gender", speaker_id=sp.id, value=("male", 0.9))]
    }

    with caplog.at_level(logging.ERROR, logger=cst.__name__):
        total = cst._combined_result_writer(_prepared(mf, normal_user), results)

    # Only the gender writer's count -- the embedding failure contributed nothing, but did
    # not raise out of _combined_result_writer either.
    assert total == 1
    db_session.expire_all()
    refreshed = db_session.get(Speaker, sp.id)
    assert refreshed.predicted_gender == "male", (
        "gender must still be written despite the embedding failure"
    )
    assert any(
        "Embedding write failed" in r.getMessage() and "gender will still run" in r.getMessage()
        for r in caplog.records
    )


def test_gender_writer_failure_does_not_prevent_the_embedding_count(
    monkeypatch, db_session, normal_user, caplog
):
    mf = _media_file(db_session, normal_user)
    sp = _speaker(db_session, mf, normal_user, "SPEAKER_00")

    monkeypatch.setattr(emv4, "_embedding_result_writer", lambda prepared, results: 5)

    def _boom_gender(prepared, results):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(sat, "_gender_result_writer", _boom_gender)

    with caplog.at_level(logging.ERROR, logger=cst.__name__):
        total = cst._combined_result_writer(_prepared(mf, normal_user), {"gender": []})

    assert total == 5
    db_session.expire_all()
    refreshed = db_session.get(Speaker, sp.id)
    assert refreshed.attributes_predicted_at is None, "the failed gender writer must not have run"
    assert any(
        "Gender write failed" in r.getMessage()
        and "embeddings may have succeeded" in r.getMessage()
        for r in caplog.records
    )


# --------------------------------------------------------------------------------------
# 2. migrate_speakers_combined_task — orchestrator
# --------------------------------------------------------------------------------------


def test_orchestrator_skips_without_touching_the_lock_when_already_running(monkeypatch):
    fake_redis = _install_fake_redis(monkeypatch)
    monkeypatch.setattr(cst.combined_migration_progress, "is_running", lambda: True)

    result = cst.migrate_speakers_combined_task.run(user_id=1)

    assert result == {"status": "skipped", "message": "Combined migration already in progress"}
    assert fake_redis.set_calls == [], "no lock should be attempted when already running"


def test_orchestrator_skips_when_another_orchestrator_holds_the_lock(monkeypatch):
    fake_redis = _install_fake_redis(monkeypatch)
    lock_key = f"{cst.combined_migration_progress.key_prefix}:orchestrator_lock"
    fake_redis.store[lock_key] = "some-other-task-id"

    result = cst.migrate_speakers_combined_task.run(user_id=1)

    assert result == {
        "status": "skipped",
        "message": "Combined migration orchestrator already starting",
    }
    assert fake_redis.store[lock_key] == "some-other-task-id", "must not steal the lock"


def test_orchestrator_with_no_completed_files_skips_and_releases_the_lock(monkeypatch, normal_user):
    fake_redis = _install_fake_redis(monkeypatch)
    monkeypatch.setattr(
        cst, "session_scope", lambda: _scope_yielding(_FakeCompletedFilesSession([]))
    )

    cst.migrate_speakers_combined_task.push_request(id="task-empty")
    try:
        result = cst.migrate_speakers_combined_task.run(user_id=normal_user.id)
    finally:
        cst.migrate_speakers_combined_task.pop_request()

    assert result == {"status": "skipped", "message": "No files to process"}
    lock_key = f"{cst.combined_migration_progress.key_prefix}:orchestrator_lock"
    assert lock_key not in fake_redis.store, "the finally block must release the lock"
    assert cst.combined_migration_progress.is_running() is False


def test_orchestrator_dispatches_correctly_sized_batches_with_the_right_kwargs(
    monkeypatch, normal_user
):
    """30 completed files at _BATCH_SIZE=25 -> two batches of [25, 5], each carrying the
    right file_uuids slice, batch_index, total_batches, total_files and user_id.

    The 30 uuids are synthetic (see ``_FakeCompletedFilesSession``) rather than real
    ``MediaFile`` rows — the orchestrator's completed-files query has no per-user scoping,
    so against the shared live dev DB an exact count is only constructible by replacing
    the query result, not by inserting rows and hoping nothing else is COMPLETED too.
    """
    fake_redis = _install_fake_redis(monkeypatch)

    expected_uuids = {str(uuid_module.uuid4()) for _ in range(30)}
    monkeypatch.setattr(
        cst,
        "session_scope",
        lambda: _scope_yielding(_FakeCompletedFilesSession(sorted(expected_uuids))),
    )

    dispatched: list[dict[str, Any]] = []

    class _FakeAsyncResult:
        def __init__(self, task_id: str) -> None:
            self.id = task_id

    def _fake_apply_async(kwargs: dict[str, Any], priority=None):
        dispatched.append(kwargs)
        return _FakeAsyncResult(f"batch-{kwargs['batch_index']}")

    monkeypatch.setattr(cst.analyze_speakers_combined_batch_task, "apply_async", _fake_apply_async)

    ws_events: list[tuple[int, str, dict]] = []
    monkeypatch.setattr(
        cst, "send_ws_event", lambda uid, ntype, data: ws_events.append((uid, ntype, data))
    )

    cst.migrate_speakers_combined_task.push_request(id="task-dispatch")
    try:
        result = cst.migrate_speakers_combined_task.run(user_id=normal_user.id)
    finally:
        cst.migrate_speakers_combined_task.pop_request()

    assert result["status"] == "started"
    assert result["total_files"] == 30

    assert len(dispatched) == 2
    assert [len(b["file_uuids"]) for b in dispatched] == [25, 5]
    assert {u for b in dispatched for u in b["file_uuids"]} == expected_uuids
    for idx, batch in enumerate(dispatched):
        assert batch["batch_index"] == idx
        assert batch["total_batches"] == 2
        assert batch["total_files"] == 30
        assert batch["user_id"] == normal_user.id

    # Batch task ids were persisted under the module's key for later revocation-on-stop.
    stored_ids_key = f"{cst.combined_migration_progress.key_prefix}:batch_task_ids"
    assert json.loads(fake_redis.store[stored_ids_key]) == ["batch-0", "batch-1"]

    # The lock is released once batches are dispatched — combined_speaker_analysis_task's
    # orchestrator (unlike speaker_attribute_migration_task's) only releases it in `finally`,
    # so it must be gone on the success path too.
    lock_key = f"{cst.combined_migration_progress.key_prefix}:orchestrator_lock"
    assert lock_key not in fake_redis.store

    assert cst.combined_migration_progress.is_running() is True
    status = cst.combined_migration_progress.get_status()
    assert status["total_files"] == 30
    assert status["processed_files"] == 0

    assert len(ws_events) == 1
    uid, ntype, data = ws_events[0]
    assert uid == normal_user.id
    assert ntype == cst.NOTIFICATION_TYPE_COMBINED_MIGRATION_PROGRESS
    assert data["total_files"] == 30
    assert data["processed_files"] == 0
    assert data["running"] is True


def test_orchestrator_releases_the_lock_even_when_an_exception_hits_mid_run(monkeypatch):
    fake_redis = _install_fake_redis(monkeypatch)

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    # Force the exception well inside the guarded try, after the lock is acquired.
    monkeypatch.setattr(cst.combined_migration_progress, "start_migration", _boom)

    # _FakeCompletedFilesSession (see its docstring above), not a genuine DB session: a
    # real query against MediaFile.status == COMPLETED with no user_id scoping depends on
    # whatever the target database happens to contain. Locally, against the dev stack, real
    # completed files make total_files > 0 and the mocked start_migration() fires as
    # intended. Against CI's fresh, empty Postgres, total_files == 0 short-circuits BEFORE
    # start_migration() is ever called, so the task returns {"status": "skipped", "message":
    # "No files to process"} instead of hitting the exception path at all -- silently testing
    # nothing on a clean database, which CI is.
    monkeypatch.setattr(
        cst, "session_scope", lambda: _scope_yielding(_FakeCompletedFilesSession(["file-1"]))
    )

    cst.migrate_speakers_combined_task.push_request(id="task-boom")
    try:
        result = cst.migrate_speakers_combined_task.run(user_id=1)
    finally:
        cst.migrate_speakers_combined_task.pop_request()

    assert result == {"status": "error", "message": "boom"}

    lock_key = f"{cst.combined_migration_progress.key_prefix}:orchestrator_lock"
    assert lock_key not in fake_redis.store, "finally must release the lock on the exception path"


# --------------------------------------------------------------------------------------
# 3. analyze_speakers_combined_batch_task — batch orchestration (GPU/model loading stubbed)
# --------------------------------------------------------------------------------------


def test_batch_task_returns_stopped_immediately_when_migration_is_not_running(monkeypatch):
    monkeypatch.setattr(cst.combined_migration_progress, "is_running", lambda: False)

    result = cst.analyze_speakers_combined_batch_task.run(
        file_uuids=["nonexistent-uuid"], batch_index=0, total_batches=1, total_files=1, user_id=1
    )

    assert result == {"status": "stopped", "batch_index": 0}


def test_batch_task_counts_prepare_failures_without_dispatching_the_pipeline(
    monkeypatch, db_session, normal_user
):
    """Every file fails prepare_file -> batch reports 'empty', each is recorded as a
    failure in the progress tracker (not silently dropped), and process_batch_pipelined
    is never invoked (nothing prepared to feed it)."""
    fake_redis = _install_fake_redis(monkeypatch)
    cst.combined_migration_progress.start_migration(total_files=2, task_id="t")

    import app.tasks.migration_pipeline as mp

    def _boom_prepare(file_uuid, include_profile=False):
        raise ValueError(f"no such file {file_uuid}")

    monkeypatch.setattr(mp, "prepare_file", _boom_prepare)

    pipeline_called = []

    def _pipelined(**kwargs):
        pipeline_called.append(kwargs)
        return (0, 0)

    monkeypatch.setattr(mp, "process_batch_pipelined", _pipelined)

    result = cst.analyze_speakers_combined_batch_task.run(
        file_uuids=["uuid-a", "uuid-b"],
        batch_index=0,
        total_batches=1,
        total_files=2,
        user_id=normal_user.id,
    )

    assert result == {"status": "empty", "batch_index": 0}
    assert pipeline_called == []

    status = cst.combined_migration_progress.get_status()
    assert status["processed_files"] == 2
    assert set(status["failed_files"]) == {"uuid-a", "uuid-b"}


def test_batch_task_marks_complete_and_sends_the_completion_event_once_all_files_land(
    monkeypatch, db_session, normal_user
):
    """One file has no speakers (prepared=None, counted success immediately without
    reaching the pipeline); the other is fed through a stubbed process_batch_pipelined
    that reports success. Once processed == total, completion must fire exactly once."""
    fake_redis = _install_fake_redis(monkeypatch)
    mf_no_speakers = _media_file(db_session, normal_user)
    mf_with_speakers = _media_file(db_session, normal_user)
    cst.combined_migration_progress.start_migration(total_files=2, task_id="t")

    import app.services.speaker_attribute_service as attr_svc
    import app.services.speaker_embedding_service as embedding_svc
    import app.tasks.migration_pipeline as mp

    def _fake_prepare(file_uuid, include_profile=False):
        if file_uuid == str(mf_no_speakers.uuid):
            return None
        return _prepared(mf_with_speakers, normal_user)

    monkeypatch.setattr(mp, "prepare_file", _fake_prepare)
    monkeypatch.setattr(embedding_svc, "get_cached_embedding_service", lambda: object())
    monkeypatch.setattr(attr_svc, "get_cached_attribute_service", lambda: object())

    def _fake_pipeline(**kwargs):
        assert kwargs["runner"].model_names == ["embedding", "gender"]
        assert kwargs["min_duration"] == cst.SPEAKER_SHORT_SEGMENT_MIN_DURATION
        for fuuid, _prepared_file in kwargs["prepared_files"]:
            kwargs["on_file_success"](fuuid)
        return (len(kwargs["prepared_files"]), 0)

    monkeypatch.setattr(mp, "process_batch_pipelined", _fake_pipeline)

    ws_events: list[tuple[int, str, dict]] = []
    monkeypatch.setattr(
        cst, "send_ws_event", lambda uid, ntype, data: ws_events.append((uid, ntype, data))
    )

    result = cst.analyze_speakers_combined_batch_task.run(
        file_uuids=[str(mf_no_speakers.uuid), str(mf_with_speakers.uuid)],
        batch_index=0,
        total_batches=1,
        total_files=2,
        user_id=normal_user.id,
    )

    assert result == {"status": "success", "batch_index": 0}

    status = cst.combined_migration_progress.get_status()
    assert status["processed_files"] == 2
    assert status["running"] is False, "complete_migration must have flipped running off"

    complete_events = [
        e for e in ws_events if e[1] == cst.NOTIFICATION_TYPE_COMBINED_MIGRATION_COMPLETE
    ]
    assert len(complete_events) == 1
    _uid, _ntype, data = complete_events[0]
    assert data["total_files"] == 2
    assert data["success_count"] == 2
    assert data["failed_files"] == []
