"""A superseded transcription run must never execute beside its replacement (issue #1020).

Recovery can fail a transcription and dispatch a replacement while the original message is
still waiting in the broker. ``transcribe_gpu_task`` then set the failed Task back to
``in_progress`` and ran to completion beside the replacement -- two transcriptions of one file,
the later one's status overwriting the other's. These tests drive the real task bodies against
real Task rows and pin that a superseded run stands down before touching anything, while a
current run still proceeds.

Also pinned here: the liveness markers a stage writes (``core/task_liveness.py``) and the
queued marker the dispatcher writes, since the recovery tests in
``test_queued_transcription_recovery.py`` take those as given.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.core.task_liveness import HEARTBEAT_KEY
from app.core.task_liveness import QUEUED_KEY
from app.core.task_liveness import RunState
from app.core.task_liveness import probe_runs
from app.core.task_liveness import run_heartbeat
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.tasks.transcription import core as core_module
from app.tasks.transcription import cpu_task as cpu_task_module
from app.tasks.transcription import postprocess as postprocess_module
from app.tasks.transcription import preprocess as preprocess_module
from app.tasks.transcription import run_ownership
from tests.unit._fake_liveness_redis import BrokenRedis
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis


class _PipelineReachedError(Exception):
    """Raised by a stand-in for the first real piece of work after the entry checks."""


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    return install_fake_redis(monkeypatch)


@pytest.fixture
def ownership_reads_test_session(monkeypatch, db_session):
    """The ownership check opens its own session; point it at the test's savepoint."""

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(run_ownership, "session_scope", _scope)
    return db_session


def _file(db, user) -> MediaFile:
    media_file = MediaFile(
        user_id=user.id,
        filename=f"g-{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        file_size=1024,
        content_type="audio/wav",
        status=FileStatus.PROCESSING,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _run(db, user, media_file, *, status: str, created_ago: timedelta) -> Task:
    task = Task(
        id=f"g1020-{uuid.uuid4()}",
        user_id=user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status=status,
        created_at=datetime.now(UTC) - created_ago,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _context(task: Task, media_file: MediaFile) -> dict:
    return {
        "task_id": task.id,
        "file_uuid": str(media_file.uuid),
        "file_id": media_file.id,
        "user_id": media_file.user_id,
        "storage_path": media_file.storage_path,
        "file_name": media_file.filename,
        "content_type": media_file.content_type,
        "diarization_source": "provider",
    }


def _run_gpu_task(context: dict, reached: list[str]) -> dict:
    """Run the real ``transcribe_gpu_task`` body up to its first piece of real work."""

    def _reach(user_id):
        reached.append(context["task_id"])
        raise _PipelineReachedError

    def _reraise(ctx, task_id, file_uuid, wav, exc):
        raise exc

    with (
        patch.object(core_module, "update_task_status"),
        patch.object(core_module, "_resolve_asr_provider_or_none", _reach),
        patch.object(core_module, "_finish_failed_or_aborted", _reraise),
    ):
        result: dict = core_module.transcribe_gpu_task.run(context)
    return result


# =============================================================================
# transcribe_gpu_task
# =============================================================================
def test_a_run_recovery_already_failed_stands_down_at_pickup(
    db_session, normal_user, fake_redis, ownership_reads_test_session
):
    """The literal #1020 sequence: recovery failed the Task while its message was queued."""
    media_file = _file(db_session, normal_user)
    failed = _run(
        db_session, normal_user, media_file, status="failed", created_ago=timedelta(hours=2)
    )
    reached: list[str] = []

    result = _run_gpu_task(_context(failed, media_file), reached)

    assert result["status"] == "superseded"
    assert result["task_id"] == failed.id
    assert reached == []
    db_session.refresh(failed)
    assert failed.status == "failed"


def test_a_run_replaced_by_a_newer_transcription_stands_down(
    db_session, normal_user, fake_redis, ownership_reads_test_session
):
    """Superseded without being failed first -- e.g. a replacement dispatched by a path that
    left the old row alone. The newer run is what owns the file now."""
    media_file = _file(db_session, normal_user)
    old = _run(
        db_session, normal_user, media_file, status="in_progress", created_ago=timedelta(hours=2)
    )
    _run(
        db_session, normal_user, media_file, status="in_progress", created_ago=timedelta(minutes=5)
    )
    reached: list[str] = []

    result = _run_gpu_task(_context(old, media_file), reached)

    assert result["status"] == "superseded"
    assert reached == []


def test_the_current_run_still_proceeds(
    db_session, normal_user, fake_redis, ownership_reads_test_session
):
    """CONTROL: without this, a guard that stood every run down would pass the two above."""
    media_file = _file(db_session, normal_user)
    current = _run(
        db_session, normal_user, media_file, status="in_progress", created_ago=timedelta(hours=2)
    )
    reached: list[str] = []

    with pytest.raises(_PipelineReachedError):
        _run_gpu_task(_context(current, media_file), reached)

    assert reached == [current.id]


def test_a_superseded_payload_from_preprocess_passes_straight_through(fake_redis):
    """Preprocess stood down and returned the marker; the GPU stage must not index it."""
    payload = {"status": "superseded", "task_id": "t", "file_uuid": "u", "file_id": 1}

    assert core_module.transcribe_gpu_task.run(dict(payload)) == payload


# =============================================================================
# The other stages
# =============================================================================
def test_preprocess_stands_down_for_a_superseded_run(
    db_session, normal_user, fake_redis, ownership_reads_test_session
):
    media_file = _file(db_session, normal_user)
    failed = _run(
        db_session, normal_user, media_file, status="failed", created_ago=timedelta(hours=2)
    )

    with patch("app.utils.uuid_helpers.get_file_by_uuid", side_effect=_PipelineReachedError):
        result = preprocess_module.preprocess_for_transcription.run(
            file_uuid=str(media_file.uuid), task_id=failed.id
        )

    assert result["status"] == "superseded"


def test_cpu_transcription_stands_down_for_a_superseded_run(
    db_session, normal_user, fake_redis, ownership_reads_test_session
):
    media_file = _file(db_session, normal_user)
    failed = _run(
        db_session, normal_user, media_file, status="failed", created_ago=timedelta(hours=2)
    )

    with patch.object(cpu_task_module, "update_task_status", side_effect=_PipelineReachedError):
        result = cpu_task_module.transcribe_cpu_task.run(_context(failed, media_file))

    assert result["status"] == "superseded"


def test_finalize_ignores_a_superseded_run_and_keeps_the_temp_audio():
    """The temp audio is keyed by file, and the replacement run is using it."""
    payload = {"status": "superseded", "task_id": "t", "file_uuid": "u", "file_id": 1}

    with patch.object(postprocess_module, "_cleanup_temp") as cleanup:
        result = postprocess_module.finalize_transcription.run(dict(payload))

    assert result == payload
    cleanup.assert_not_called()


# =============================================================================
# Liveness markers
# =============================================================================
def test_a_running_stage_replaces_its_queued_marker_with_a_heartbeat(fake_redis):
    fake_redis.mark_queued("run-1")

    with run_heartbeat("run-1"):
        during = probe_runs(["run-1"])["run-1"]
        assert HEARTBEAT_KEY.format(task_id="run-1") in fake_redis.store
        assert QUEUED_KEY.format(task_id="run-1") not in fake_redis.store

    after = probe_runs(["run-1"])["run-1"]
    assert during.state == RunState.RUNNING
    assert during.started_at is not None
    # The stage ended and handed the run back to the broker (its successor link).
    assert after.state == RunState.QUEUED
    assert HEARTBEAT_KEY.format(task_id="run-1") not in fake_redis.store


def test_a_run_with_no_markers_is_dead_and_an_unreadable_broker_is_unknown(monkeypatch, fake_redis):
    assert probe_runs(["gone"])["gone"].state == RunState.DEAD

    monkeypatch.setattr("app.core.redis.get_redis", lambda: BrokenRedis())

    assert probe_runs(["gone"])["gone"].state == RunState.UNKNOWN


def test_dispatch_records_the_run_as_queued(db_session, normal_user, fake_redis, monkeypatch):
    """Without the dispatch-time marker a run waiting for its first worker reads as lost."""
    from types import SimpleNamespace

    import app.tasks.transcription.dispatch as dispatch_module

    @contextlib.contextmanager
    def _scope():
        yield db_session

    chain_stub = SimpleNamespace(apply_async=lambda **kwargs: SimpleNamespace(id="ok"))
    monkeypatch.setattr(dispatch_module, "session_scope", _scope)
    monkeypatch.setattr(dispatch_module, "chain", lambda *a, **k: chain_stub)
    media_file = _file(db_session, normal_user)

    task_id = dispatch_module.dispatch_transcription_pipeline(
        file_uuid=str(media_file.uuid), gpu_queue="gpu"
    )

    assert probe_runs([task_id])[task_id].state == RunState.QUEUED
