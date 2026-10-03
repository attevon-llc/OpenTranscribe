"""Orphan classification and the lease rules the reaper depends on (no live Redis needed).

The live end-to-end proof (a real worker SIGKILLed, real kombu, real Redis) is
``tests/integration/test_orphaned_delivery_recovery_live.py``. These pin the decisions
themselves: what counts as an orphan, what the reserved gauge stops counting, and the lease
hand-back rule that keeps a dropped run from posing as "queued".
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from celery.exceptions import Reject
from celery.exceptions import Retry

from app.core.broker_orphans import classify
from app.core.broker_orphans import parse_unacked
from app.core.celery_metrics import queue_snapshot
from app.core.task_config import task_recovery_config
from app.core.task_liveness import HEARTBEAT_KEY
from app.core.task_liveness import QUEUED_KEY
from app.core.task_liveness import RunState
from app.core.task_liveness import probe_runs
from app.core.task_liveness import run_heartbeat
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis

STALE = 120


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    monkeypatch.setattr(task_recovery_config, "BROKER_ORPHAN_STALE", STALE)
    return install_fake_redis(monkeypatch)


def _entry(
    task: str,
    *,
    kwargs: dict | None = None,
    args: list | None = None,
    stage_id: str = "stage-1",
    queue: str = "cpu",
    eta: str | None = None,
) -> str:
    """One kombu ``unacked`` value, in the shape Celery's protocol 2 publishes."""
    body = base64.b64encode(json.dumps([args or [], kwargs or {}, {}]).encode()).decode()
    message = {
        "body": body,
        "content-encoding": "utf-8",
        "content-type": "application/json",
        "headers": {"id": stage_id, "task": task, "eta": eta},
        "properties": {"body_encoding": "base64", "priority": 2, "delivery_info": {}},
    }
    return json.dumps([message, queue, queue])


def _preprocess(run_id: str, **kw) -> str:
    return _entry("transcription.preprocess", kwargs={"file_uuid": "f-1", "task_id": run_id}, **kw)


def _gpu_stage(run_id: str, **kw) -> str:
    payload = {"task_id": run_id, "file_uuid": "f-1", "file_id": 1}
    return _entry("transcription.gpu_transcribe", args=[payload], queue="gpu", **kw)


# =============================================================================
# Decoding
# =============================================================================
def test_a_stage_message_yields_its_run_from_kwargs_or_the_payload():
    first = parse_unacked("t1", _preprocess("run-a"), 1.0)
    later = parse_unacked("t2", _gpu_stage("run-b"), 1.0)

    assert (first.run_id, first.file_uuid, first.queue, first.is_stage) == (
        "run-a",
        "f-1",
        "cpu",
        True,
    )
    assert (later.run_id, later.queue, later.is_stage) == ("run-b", "gpu", True)


def test_a_non_stage_message_is_never_decoded_as_a_run():
    other = parse_unacked("t", _entry("analytics.analyze_transcript", kwargs={"task_id": "x"}), 1)

    assert other.run_id is None
    assert not other.is_stage


@pytest.mark.parametrize("raw", [b"not-json", json.dumps([{}, "cpu"]), json.dumps("x")])
def test_a_malformed_entry_decodes_to_nothing_rather_than_raising(raw):
    delivery = parse_unacked("t", raw, None)

    assert delivery.queue is None
    assert not delivery.is_stage


# =============================================================================
# Classification
# =============================================================================
def test_a_stage_whose_run_has_no_lease_is_orphaned_once_stale(fake_redis):
    now = time.time()
    dead = parse_unacked("t", _preprocess("run-dead"), now - STALE - 5)

    held, orphaned = classify([dead], now=now)

    assert orphaned == [dead]
    assert held == []


def test_a_leased_stage_is_held_however_old(fake_redis):
    now = time.time()
    fake_redis.mark_running("run-live", started_seconds_ago=3600)
    live = parse_unacked("t", _gpu_stage("run-live"), now - 7200)

    held, orphaned = classify([live], now=now)

    assert held == [live]
    assert orphaned == []


def test_a_fresh_delivery_without_a_lease_yet_is_held(fake_redis):
    """Delivered seconds ago: its first heartbeat may simply not have landed."""
    now = time.time()
    fresh = parse_unacked("t", _preprocess("run-new"), now - 5)

    _, orphaned = classify([fresh], now=now)

    assert orphaned == []


def test_a_retry_held_for_its_eta_is_not_an_orphan(fake_redis):
    """A backoff retry waits in a worker until its ETA, unacked and without a lease."""
    now = time.time()
    eta = (datetime.now(UTC) + timedelta(minutes=4)).isoformat()
    waiting = parse_unacked("t", _preprocess("run-eta", eta=eta), now - STALE - 60)

    _, orphaned = classify([waiting], now=now)

    assert orphaned == []


def test_nothing_is_orphaned_when_the_lease_store_is_unreadable(monkeypatch):
    from tests.unit._fake_liveness_redis import BrokenRedis

    monkeypatch.setattr("app.core.redis.get_redis", lambda: BrokenRedis())
    now = time.time()
    entry = parse_unacked("t", _preprocess("run-x"), now - 3600)

    held, orphaned = classify([entry], now=now)

    assert orphaned == []
    assert held == [entry]


# =============================================================================
# The reserved gauge stops counting what a dead worker took with it
# =============================================================================
class _Pipe:
    def __init__(self, unacked: dict, scores: list):
        self._unacked, self._scores = unacked, scores
        self.calls: list[str] = []

    def llen(self, key):
        self.calls.append("llen")
        return self

    def hgetall(self, key):
        self.calls.append("hgetall")
        return self

    def zrange(self, key, start, end, withscores=False):
        self.calls.append("zrange")
        return self

    def execute(self):
        out: list = []
        for call in self.calls:
            out.append(
                0 if call == "llen" else self._unacked if call == "hgetall" else self._scores
            )
        return out


class _Broker:
    def __init__(self, unacked: dict, delivered: dict):
        self._unacked = unacked
        self._scores = list(delivered.items())

    def pipeline(self, transaction=False):
        return _Pipe(self._unacked, self._scores)


def test_reserved_counts_live_stages_and_other_tasks_but_not_orphans(fake_redis):
    now = time.time()
    fake_redis.mark_running("run-live", started_seconds_ago=60)
    broker = _Broker(
        unacked={
            "live": _gpu_stage("run-live", stage_id="s-live"),
            "dead": _gpu_stage("run-dead", stage_id="s-dead"),
            "other": json.dumps([{}, "gpu", "gpu"]),  # an acks_late task with no lease to check
        },
        delivered={"live": now - 900, "dead": now - 900, "other": now - 30},
    )

    gpu = queue_snapshot(broker)["gpu"]

    assert gpu["reserved"] == 2
    assert gpu["orphaned"] == 1
    assert 890 <= gpu["oldest_unacked_age"] <= 960


# =============================================================================
# The lease hand-back rule
# =============================================================================
def test_a_stage_that_returns_hands_the_run_back_as_queued(fake_redis):
    with run_heartbeat("run-1"):
        pass

    assert probe_runs(["run-1"])["run-1"].state == RunState.QUEUED


@pytest.mark.parametrize("requeue", [Retry(), Reject(requeue=True)])
def test_a_retry_or_a_requeue_hands_the_run_back_as_queued(fake_redis, requeue):
    with pytest.raises(type(requeue)), run_heartbeat("run-2"):
        raise requeue

    assert probe_runs(["run-2"])["run-2"].state == RunState.QUEUED


@pytest.mark.parametrize(
    "ended_by", [RuntimeError("boom"), SystemExit(1), KeyboardInterrupt(), Reject(requeue=False)]
)
def test_a_stage_torn_down_leaves_the_run_dead_not_queued(fake_redis, ended_by):
    """The cold-shutdown cancel acks the message on Redis: nothing is queued, so the run must
    read as DEAD for the reaper to re-dispatch it, not as QUEUED for a week."""
    with pytest.raises(type(ended_by)), run_heartbeat("run-3"):
        raise ended_by

    assert probe_runs(["run-3"])["run-3"].state == RunState.DEAD
    assert HEARTBEAT_KEY.format(task_id="run-3") not in fake_redis.store
    assert QUEUED_KEY.format(task_id="run-3") not in fake_redis.store


def test_the_lease_records_which_message_holds_it(fake_redis, monkeypatch):
    monkeypatch.setattr("app.core.task_liveness._current_stage_id", lambda: "celery-id-7")

    with run_heartbeat("run-4"):
        run = probe_runs(["run-4"])["run-4"]

    assert run.state == RunState.RUNNING
    assert run.owner == "celery-id-7"
    assert run.started_at is not None


def test_a_lease_written_before_owners_were_recorded_still_reads(fake_redis):
    fake_redis.mark_running("run-5", started_seconds_ago=30)  # the bare-epoch format

    run = probe_runs(["run-5"])["run-5"]

    assert run.state == RunState.RUNNING
    assert run.started_at is not None
    assert run.owner is None


# =============================================================================
# A second delivery of a running (or finished) stage stands down
# =============================================================================
@pytest.fixture
def current_run(db_session, normal_user, monkeypatch):
    import uuid as uuid_mod
    from contextlib import contextmanager

    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import Task
    from app.tasks.transcription import run_ownership

    @contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(run_ownership, "session_scope", _scope)
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=normal_user.id,
        filename="dup.wav",
        storage_path="dup/dup.wav",
        file_size=1,
        content_type="audio/wav",
        status=FileStatus.PROCESSING,
    )
    db_session.add(media_file)
    db_session.commit()
    task = Task(
        id=f"dup-{uuid_mod.uuid4()}",
        user_id=normal_user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
        created_at=datetime.now(UTC),
    )
    db_session.add(task)
    db_session.commit()
    return task


def _as_stage(monkeypatch, stage_id: str, *, succeeded: bool = False) -> None:
    monkeypatch.setattr("app.core.task_liveness._current_stage_id", lambda: stage_id)

    class _Result:
        def __init__(self, task_id, app=None):
            self.state = "SUCCESS" if succeeded else "PENDING"

    monkeypatch.setattr("celery.result.AsyncResult", _Result)


def test_a_copy_of_the_message_holding_the_lease_stands_down(fake_redis, current_run, monkeypatch):
    from app.tasks.transcription.run_ownership import superseded_result

    fake_redis.setex(
        HEARTBEAT_KEY.format(task_id=current_run.id),
        90,
        json.dumps({"started": time.time(), "owner": "stage-A"}),
    )
    _as_stage(monkeypatch, "stage-A")

    result = superseded_result(current_run.id, "f", stage="GPU transcription")

    assert result is not None and result["status"] == "superseded"


def test_a_different_stage_of_the_same_run_proceeds(fake_redis, current_run, monkeypatch):
    """The gpu-split diarize leg starts while the transcribe leg still holds the lease."""
    from app.tasks.transcription.run_ownership import superseded_result

    fake_redis.setex(
        HEARTBEAT_KEY.format(task_id=current_run.id),
        90,
        json.dumps({"started": time.time(), "owner": "stage-transcribe"}),
    )
    _as_stage(monkeypatch, "stage-diarize")

    assert superseded_result(current_run.id, "f", stage="GPU diarization") is None


def test_a_copy_of_a_stage_that_already_completed_stands_down(fake_redis, current_run, monkeypatch):
    from app.tasks.transcription.run_ownership import superseded_result

    _as_stage(monkeypatch, "stage-B", succeeded=True)

    result = superseded_result(current_run.id, "f", stage="Preprocess")

    assert result is not None and result["status"] == "superseded"
