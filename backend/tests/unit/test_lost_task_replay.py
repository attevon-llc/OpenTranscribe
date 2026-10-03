"""Replay of idempotent tasks whose worker died mid-run (issue #1067).

After a CPU worker was OOM-killed, 14 ``speaker_attribute_detection`` and 2
``speaker_clustering`` Task rows stayed ``in_progress`` with nothing running. The detection
task was early-acked, so its message was gone, and the clustering task's ``acks_late``
message sat un-acked until the 6 h visibility timeout. These tests drive
``app/core/task_replay.py`` against a fake Redis: a worker records and heartbeats the run, the
worker "dies" (its heartbeat key expires), and the sweep must re-send exactly that run, only
that run, a bounded number of times, and never a transcription.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core import task_replay as tr
from app.core.celery import celery_app
from app.core.constants import CeleryQueues
from app.core.task_config import TaskRecoveryConfig
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.task_detection_service import TaskDetectionService
from tests.unit._fake_liveness_redis import BrokenRedis
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    fake = install_fake_redis(monkeypatch)
    monkeypatch.setattr(tr, "_ensure_beat_thread", lambda: None)
    monkeypatch.setattr(tr, "_running", set())
    return fake


class _FakeTask:
    """Hashable (a signal sender must be), with just what a worker-side hook reads."""

    priority = None

    def __init__(self, name: str, request: SimpleNamespace) -> None:
        self.name, self.request = name, request


def _task(name: str, *, queue: str = "cpu", priority: int = 4, eager: bool = False):
    return _FakeTask(
        name,
        SimpleNamespace(delivery_info={"routing_key": queue, "priority": priority}, is_eager=eager),
    )


class _Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name, **options):
        self.calls.append((name, options))


def _die(fake: FakeRedis, task_id: str, *, seconds_ago: float = 600) -> None:
    """The worker running ``task_id`` was killed ``seconds_ago``: no beat, no postrun."""
    fake.expire(tr.HEARTBEAT_KEY.format(task_id=task_id))
    record = json.loads(fake.hget(tr.REPLAY_HASH, task_id) or "{}")
    record["recorded_at"] = time.time() - seconds_ago
    fake.hset(tr.REPLAY_HASH, task_id, json.dumps(record))


# --- the worker side -----------------------------------------------------------------------


def test_a_replayable_task_is_recorded_and_heartbeats(fake_redis):
    tr.on_task_start(_task("detect_speaker_attributes"), "t-1", ["file-uuid", 7], {})

    record = json.loads(fake_redis.hget(tr.REPLAY_HASH, "t-1"))
    assert record["name"] == "detect_speaker_attributes"
    assert record["args"] == ["file-uuid", 7]
    assert (record["queue"], record["priority"], record["attempt"]) == ("cpu", 4, 0)
    assert fake_redis.get(tr.HEARTBEAT_KEY.format(task_id="t-1")) is not None

    fake_redis.expire(tr.HEARTBEAT_KEY.format(task_id="t-1"))
    tr._beat_all()
    assert fake_redis.get(tr.HEARTBEAT_KEY.format(task_id="t-1")) is not None, (
        "the heartbeat loop did not refresh a running task"
    )


def test_a_finished_task_leaves_nothing_to_replay(fake_redis):
    task = _task("speaker.cluster_for_file")
    tr.on_task_start(task, "t-2", ["f", 1], {})
    tr.on_task_end(task, "t-2")

    assert fake_redis.hgetall(tr.REPLAY_HASH) == {}
    assert fake_redis.get(tr.HEARTBEAT_KEY.format(task_id="t-2")) is None


@pytest.mark.parametrize(
    "name", ["transcription.gpu_transcribe", "transcription.process_file", "cleanup.deep_cleanup"]
)
def test_unlisted_and_transcription_tasks_are_never_recorded(fake_redis, name):
    tr.on_task_start(_task(name, queue="gpu"), "t-3", [], {})
    assert fake_redis.hgetall(tr.REPLAY_HASH) == {}


def test_an_eager_run_is_not_recorded(fake_redis):
    tr.on_task_start(_task("detect_speaker_attributes", eager=True), "t-4", [], {})
    assert fake_redis.hgetall(tr.REPLAY_HASH) == {}


def test_a_worker_side_redis_outage_does_not_fail_the_task(monkeypatch):
    monkeypatch.setattr("app.core.redis.get_redis", lambda: BrokenRedis())
    monkeypatch.setattr(tr, "_running", set())

    tr.on_task_start(_task("detect_speaker_attributes"), "t-5", [], {})
    tr.on_task_end(_task("detect_speaker_attributes"), "t-5")

    assert tr._running == set(), "an unrecorded run must not be heartbeaten"


def test_nothing_gpu_or_transcription_is_replayable():
    """A replayed transcription is a second GPU run; the allowlist must never admit one."""
    celery_app.loader.import_default_modules()  # register every task module in `include`
    routes = celery_app.conf.task_routes
    gpu_queues = {q for q in CeleryQueues.ALL if q.startswith("gpu")}
    for name in tr.REPLAYABLE_TASKS:
        assert not name.startswith("transcription."), name
        assert routes.get(name, {}).get("queue") not in gpu_queues, name
        assert name in celery_app.tasks, f"{name} is not a registered task"
    assert not tr.is_replayable("transcription.gpu_transcribe")


def test_the_sweep_is_scheduled_on_the_utility_queue():
    schedule = celery_app.conf.beat_schedule["reclaim-lost-tasks"]
    assert schedule["task"] == "system.reclaim_lost_tasks"
    assert celery_app.conf.task_routes["system.reclaim_lost_tasks"]["queue"] == "utility"


# --- the sweep ---------------------------------------------------------------------------


def test_a_dead_run_is_replayed_once_under_its_own_id(fake_redis, monkeypatch):
    monkeypatch.setattr(tr, "_task_row_is_terminal", lambda task_id: False)
    marked: list[tuple[str, str]] = []
    monkeypatch.setattr(tr, "_mark_task_row", lambda t, s, m: marked.append((t, s)))
    tr.on_task_start(_task("detect_speaker_attributes", priority=4), "dead", ["f-uuid", 3], {})
    tr.on_task_start(_task("speaker.cluster_for_file", priority=2), "alive", ["g-uuid", 3], {})
    _die(fake_redis, "dead")
    sent = _Sent()

    summary = tr.reclaim_lost_tasks(send_task=sent)

    assert sent.calls == [
        (
            "detect_speaker_attributes",
            {"args": ["f-uuid", 3], "kwargs": {}, "task_id": "dead", "queue": "cpu", "priority": 4},
        )
    ]
    assert summary["replayed"] == 1 and summary["alive"] == 1
    assert json.loads(fake_redis.hget(tr.REPLAY_HASH, "dead"))["attempt"] == 1
    assert marked == [("dead", "pending")]

    again = _Sent()
    tr.reclaim_lost_tasks(send_task=again)
    assert again.calls == [], "a second sweep inside the claim window re-sent the same run"


def test_the_replay_keeps_its_attempt_count_when_it_starts(fake_redis, monkeypatch):
    monkeypatch.setattr(tr, "_task_row_is_terminal", lambda task_id: False)
    monkeypatch.setattr(tr, "_mark_task_row", lambda *a: None)
    task = _task("detect_speaker_attributes")
    tr.on_task_start(task, "p", ["f", 1], {})
    _die(fake_redis, "p")
    tr.reclaim_lost_tasks(send_task=_Sent())

    tr.on_task_start(task, "p", ["f", 1], {})  # the replay starts on another worker

    assert json.loads(fake_redis.hget(tr.REPLAY_HASH, "p"))["attempt"] == 1


def test_a_poison_task_stops_after_the_attempt_limit(fake_redis, monkeypatch):
    monkeypatch.setattr(tr, "_task_row_is_terminal", lambda task_id: False)
    marked: list[tuple[str, str]] = []
    monkeypatch.setattr(tr, "_mark_task_row", lambda t, s, m: marked.append((t, s)))
    monkeypatch.setattr(tr.task_recovery_config, "TASK_REPLAY_MAX_ATTEMPTS", 2)
    task = _task("detect_speaker_attributes")
    sent = _Sent()

    for _ in range(3):  # the task OOMs its worker every time
        tr.on_task_start(task, "poison", ["f", 1], {})
        _die(fake_redis, "poison")
        fake_redis.expire(tr.CLAIM_KEY.format(task_id="poison"))
        tr.reclaim_lost_tasks(send_task=sent)

    assert len(sent.calls) == 2
    assert marked[-1] == ("poison", "failed")
    assert fake_redis.hget(tr.REPLAY_HASH, "poison") is None


def test_an_unreadable_redis_replays_nothing(monkeypatch):
    monkeypatch.setattr("app.core.redis.get_redis", lambda: BrokenRedis())
    sent = _Sent()
    summary = tr.reclaim_lost_tasks(send_task=sent)
    assert sent.calls == [] and summary.get("error") == 1


def test_a_record_whose_task_already_finished_is_dropped(fake_redis, monkeypatch):
    monkeypatch.setattr(tr, "_task_row_is_terminal", lambda task_id: True)
    tr.on_task_start(_task("analytics.analyze_transcript"), "done", ["f"], {})
    _die(fake_redis, "done")
    sent = _Sent()

    summary = tr.reclaim_lost_tasks(send_task=sent)

    assert sent.calls == [] and summary["dropped"] == 1
    assert fake_redis.hget(tr.REPLAY_HASH, "done") is None


# --- against real Task rows ----------------------------------------------------------------


@contextmanager
def _real_scope(db_session):
    try:
        yield db_session
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise


def _stale_attr_row(db, user, task_id: str) -> Task:
    media_file = MediaFile(
        user_id=user.id,
        filename="m.wav",
        storage_path=f"u/{uuid.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status=FileStatus.COMPLETED,
    )
    db.add(media_file)
    db.flush()
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    row = Task(
        id=task_id,
        user_id=user.id,
        media_file_id=media_file.id,
        task_type="speaker_attribute_detection",
        status="in_progress",
        progress=0.3,
        created_at=long_ago,
        updated_at=long_ago,
    )
    db.add(row)
    db.commit()
    return row


def test_the_sweep_moves_the_lost_row_back_to_pending_then_fails_it_when_exhausted(
    fake_redis, monkeypatch, db_session, normal_user
):
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _real_scope(db_session))
    monkeypatch.setattr(tr.task_recovery_config, "TASK_REPLAY_MAX_ATTEMPTS", 1)
    task_id = str(uuid.uuid4())
    _stale_attr_row(db_session, normal_user, task_id)
    task = _task("detect_speaker_attributes")

    tr.on_task_start(task, task_id, ["f", normal_user.id], {})
    _die(fake_redis, task_id)
    tr.reclaim_lost_tasks(send_task=_Sent())
    db_session.expire_all()
    assert db_session.get(Task, task_id).status == "pending"

    tr.on_task_start(task, task_id, ["f", normal_user.id], {})
    _mark_in_progress(db_session, task_id)
    _die(fake_redis, task_id)
    fake_redis.expire(tr.CLAIM_KEY.format(task_id=task_id))
    tr.reclaim_lost_tasks(send_task=_Sent())
    db_session.expire_all()
    row = db_session.get(Task, task_id)
    assert row.status == "failed"
    assert "Worker lost 2 time(s)" in row.error_message


def _mark_in_progress(db, task_id: str) -> None:
    row = db.get(Task, task_id)
    row.status = "in_progress"
    db.commit()


def test_the_health_check_leaves_a_replay_tracked_task_to_the_sweep(
    fake_redis, db_session, normal_user
):
    """Without this the 30-minute budget fails a row the sweep is about to replay."""
    detection = TaskDetectionService(config=TaskRecoveryConfig(STALENESS_THRESHOLD=300))
    tracked, untracked = str(uuid.uuid4()), str(uuid.uuid4())
    _stale_attr_row(db_session, normal_user, tracked)
    _stale_attr_row(db_session, normal_user, untracked)
    tr.on_task_start(_task("detect_speaker_attributes"), tracked, ["f", 1], {})
    _die(fake_redis, tracked)  # dead, but its replay record is waiting for the sweep

    stuck = {t.id for t in detection.identify_stuck_tasks(db_session)}

    assert tracked not in stuck
    assert untracked in stuck, "an ordinary over-budget task must still be reported stuck"


# --- the speaker-attribute dedupe guard ----------------------------------------------------


def test_the_speaker_attribute_guard_is_taken_over_from_a_dead_holder(fake_redis):
    from app.tasks.speaker_attribute_task import _take_over_guard

    fake_redis.set("speaker_attr_detect:f", "1")  # written by a worker that was OOM-killed
    assert _take_over_guard(fake_redis, "speaker_attr_detect:f", "replay") is True
    assert fake_redis.get("speaker_attr_detect:f") == "replay"

    assert _take_over_guard(fake_redis, "speaker_attr_detect:f", "replay") is True, (
        "a replay under the same id must not skip itself as its own duplicate"
    )

    fake_redis.setex(tr.HEARTBEAT_KEY.format(task_id="replay"), 120, "1")
    assert _take_over_guard(fake_redis, "speaker_attr_detect:f", "someone-else") is False, (
        "a live holder's guard was stolen"
    )


def test_the_celery_signals_are_wired(fake_redis):
    """Through Celery's own prerun/postrun signals, as a worker fires them."""
    from celery.signals import task_postrun
    from celery.signals import task_prerun

    task = _task("speaker.cluster_for_file")
    task_prerun.send(sender=task, task_id="sig", task=task, args=["f", 1], kwargs={})
    assert fake_redis.hget(tr.REPLAY_HASH, "sig") is not None

    task_postrun.send(sender=task, task_id="sig", task=task, args=["f", 1], kwargs={})
    assert fake_redis.hget(tr.REPLAY_HASH, "sig") is None


# --- what the per-file pipeline dispatches must be covered -------------------------------

#: Tasks the per-file pipeline dispatches that are deliberately NOT replayable, each with why.
#: A task in neither this map nor ``REPLAYABLE_TASKS`` is a run that silently vanishes when its
#: worker dies mid-run (an early ack drops the message; nothing re-sends it).
_NOT_REPLAYED_ON_PURPOSE = {
    # GPU work: a replay is a second GPU run, and the allowlist admits no GPU task.
    "rediarize": "GPU queue",
    # Updates speaker profiles and closes the pipeline run (firing its completion hook), so a
    # second run is not known to reach the same end state. Its loss is still recovered: the
    # pipeline run it was closing reads as lost and is re-dispatched.
    "extract_speaker_embeddings": "not idempotent; pipeline run recovery covers its loss",
    # A short fan-out of other dispatches; a replay would dispatch the LLM tasks twice.
    "transcription.enrich_and_dispatch": "fan-out only; replay would double-dispatch",
    # Benchmark bookkeeping only.
    "pipeline_timing.flush_tail": "benchmark markers only",
}

_PIPELINE_DISPATCH_MODULES = (
    "app/tasks/transcription/preprocess.py",
    "app/tasks/transcription/postprocess.py",
    "app/tasks/transcription/background.py",
    "app/tasks/transcription/downstream.py",
    "app/tasks/ingest_artifacts_task.py",
)


def _pipeline_dispatched_task_names() -> set[str]:
    """Registered names of every task ``<task>.delay(...)``/``.apply_async(...)``'d by the
    per-file pipeline modules, resolved through the Celery registry."""
    import ast
    from pathlib import Path

    celery_app.loader.import_default_modules()
    by_function = {getattr(t, "__name__", None): name for name, t in celery_app.tasks.items()}
    backend = Path(__file__).resolve().parents[2]
    names: set[str] = set()
    for module in _PIPELINE_DISPATCH_MODULES:
        tree = ast.parse((backend / module).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("delay", "apply_async")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in by_function
            ):
                names.add(by_function[node.func.value.id])
    return names


def test_every_task_the_pipeline_dispatches_is_replayable_or_excluded_for_a_reason():
    """The pipeline's own waveform task was missing: the allowlist named the bulk backfill
    task (``media.generate_waveform_data``) instead, so a waveform run lost with its worker
    was never re-sent and the file kept no waveform."""
    dispatched = _pipeline_dispatched_task_names()
    # Guard against a scan that matches nothing and passes vacuously.
    assert {"media.generate_waveform", "detect_speaker_attributes", "rediarize"} <= dispatched

    uncovered = sorted(
        name
        for name in dispatched
        if not tr.is_replayable(name) and name not in _NOT_REPLAYED_ON_PURPOSE
    )
    assert uncovered == []
    assert not set(_NOT_REPLAYED_ON_PURPOSE) & tr.REPLAYABLE_TASKS


@pytest.mark.parametrize("name", ["media.generate_waveform", "redaction.detect"])
def test_the_pipelines_waveform_and_redaction_runs_are_recorded_for_replay(fake_redis, name):
    tr.on_task_start(_task(name), "t-pipe", [11, "file-uuid"], {})

    record = json.loads(fake_redis.hget(tr.REPLAY_HASH, "t-pipe"))
    assert record["name"] == name
    assert record["args"] == [11, "file-uuid"]
