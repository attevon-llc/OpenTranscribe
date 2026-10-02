"""A worker SIGKILLed while holding a transcription stage: real Celery, real kombu, real Redis.

What this proves end to end, with a real ``celery worker`` subprocess whose whole process
group is SIGKILLed mid-stage (the shape of an OOM kill, a node loss, or a container killed at
the end of its stop grace period -- the prefork parent dies with its child, so
``reject_on_worker_lost`` never gets a chance):

1. The defect: kombu does NOT give the message back. It stays in the ``unacked`` hash (it
   would until the six-hour visibility timeout), and before this change
   ``celery_queue_reserved`` kept counting it as held work.
2. Once the run's lease lapses, the reserved gauge drops it and reports it as orphaned.
3. The reaper puts that exact stage back at the HEAD of its priority list -- ahead of a
   submission made after the kill -- removes it from ``unacked`` (so no six-hour duplicate),
   and a fresh worker runs it first, without the file ever leaving PROCESSING.

And, separately, what Celery's own soft shutdown does on this version (5.6.x) with a real
Redis broker, because the deployment advice depends on it.

Needs a Redis it may write to; uses database 15 and deletes only the keys it touched. Point
``REDIS_PORT`` (and ``REDIS_PASSWORD``) at it, e.g. a throwaway
``docker run -d -p 127.0.0.1:56380:6379 redis:7-alpine`` with ``REDIS_PORT=56380``. Postgres
is the usual test database (``db_session``).

Run:
    cd backend && REDIS_PORT=56380 pytest -m integration -o addopts="" \\
        tests/integration/test_orphaned_delivery_recovery_live.py -v
"""

from __future__ import annotations

import importlib
import os
import signal
import subprocess  # nosec B404 - starts a celery worker under test, fixed argv
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
import redis as redis_lib
from kombu import Connection
from kombu.transport.redis import Channel

from app.core.constants import CPUPriority
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task

pytestmark = [
    pytest.mark.integration,
    # One unacked hash per Redis DB: tests here must never run beside each other.
    pytest.mark.xdist_group("orphan_delivery_live"),
]

BACKEND = Path(__file__).resolve().parents[2]
_TEST_DB = 15
_QUEUE = "cpu"
_PRIORITY = CPUPriority.PIPELINE_CRITICAL
_LEASE_TTL = 3
_STALE = 2


def _redis_url() -> str:
    password = os.environ.get("REDIS_PASSWORD", "")
    port = os.environ.get("REDIS_PORT", "5177")
    auth = f":{password}@" if password else ""
    return f"redis://{auth}127.0.0.1:{port}/{_TEST_DB}"


def _priority_key(priority: int) -> str:
    return _QUEUE if priority == 0 else f"{_QUEUE}{Channel.sep}{priority}"


@pytest.fixture
def broker(monkeypatch):
    client = redis_lib.from_url(_redis_url())
    try:
        client.ping()
    except redis_lib.exceptions.RedisError as exc:
        pytest.skip(f"live Redis (db {_TEST_DB}) unreachable: {exc}")
    if client.hlen(Channel.unacked_key):
        pytest.skip(f"db {_TEST_DB} already holds unacked messages; refusing to share it")
    monkeypatch.setenv("ORPHAN_TEST_BROKER_URL", _redis_url())
    # Every lease read/write in THIS process goes to the test Redis too.
    monkeypatch.setattr("app.core.redis.get_redis", lambda: client)
    yield client
    keys = [Channel.unacked_key, Channel.unacked_index_key, Channel.unacked_mutex_key]
    keys += [_priority_key(p) for p in range(10)]
    keys += list(client.scan_iter("orphan-test:*"))
    keys += list(client.scan_iter("transcription_*"))
    keys += list(client.scan_iter("_kombu.binding.*"))
    keys += list(client.scan_iter("celery-task-meta-*"))
    client.delete(*keys)
    client.close()


@pytest.fixture
def worker_app(broker):
    module = importlib.import_module("tests.integration._orphan_worker_app")
    return importlib.reload(module)


@pytest.fixture
def workers():
    """Start real ``celery worker`` subprocesses; stop whatever is left at teardown."""
    started: list[subprocess.Popen] = []

    def _start(**extra_env: str) -> subprocess.Popen:
        env = {
            **os.environ,
            "ORPHAN_TEST_BROKER_URL": _redis_url(),
            "REDIS_URL": _redis_url(),
            "TRANSCRIPTION_HEARTBEAT_INTERVAL_SECONDS": "1",
            "TRANSCRIPTION_HEARTBEAT_TTL_SECONDS": str(_LEASE_TTL),
            "PYTHONPATH": str(BACKEND),
            **extra_env,
        }
        proc = subprocess.Popen(  # nosec B603 - fixed argv, test-owned process
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "tests.integration._orphan_worker_app",
                "worker",
                "-Q",
                _QUEUE,
                "-c",
                "1",
                "-P",
                "prefork",
                "--without-gossip",
                "--without-mingle",
                "-n",
                f"orphan-{uuid.uuid4().hex[:8]}@%h",
                "--loglevel",
                "INFO",
            ],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # its own process group: parent AND children die together
        )
        started.append(proc)
        return proc

    yield _start
    for proc in started:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


@contextmanager
def _bridged_scope(db_session):
    yield db_session
    db_session.commit()


def _wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def _run_row(db, user) -> tuple[MediaFile, Task]:
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        user_id=user.id,
        filename="orphan-live.wav",
        storage_path=f"user_{user.id}/orphan-live.wav",
        file_size=1024,
        content_type="audio/wav",
        status=FileStatus.PROCESSING,
        retry_count=0,
    )
    db.add(media_file)
    db.commit()
    task = Task(
        id=f"orphan-live-{uuid.uuid4()}",
        user_id=user.id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
        created_at=datetime.now(UTC),
    )
    db.add(task)
    db.commit()
    return media_file, task


def _send(broker, worker_app, media_file: MediaFile, task: Task, *, block: bool) -> None:
    """Publish one stage exactly as ``dispatch_transcription_pipeline`` does: mark the run
    queued first, then publish at the pipeline's priority."""
    from app.core.task_liveness import mark_queued

    if block:
        broker.set(worker_app.BLOCK_KEY.format(task_id=task.id), "1", ex=600)
    mark_queued(task.id)
    worker_app.app.send_task(
        "transcription.preprocess",
        kwargs={"file_uuid": str(media_file.uuid), "task_id": task.id},
        queue=_QUEUE,
        priority=_PRIORITY,
    )


def test_a_sigkilled_workers_stage_is_requeued_at_the_front_within_seconds(
    broker, worker_app, workers, db_session, normal_user, monkeypatch
):
    from app.core.broker_orphans import reclaim_orphaned_deliveries
    from app.core.celery_metrics import queue_snapshot
    from app.core.task_config import task_recovery_config

    monkeypatch.setattr(task_recovery_config, "BROKER_ORPHAN_STALE", _STALE)
    monkeypatch.setattr(
        "app.services.transcription_retry.session_scope", lambda: _bridged_scope(db_session)
    )
    interrupted_file, interrupted = _run_row(db_session, normal_user)
    newer_file, newer = _run_row(db_session, normal_user)

    worker_a = workers()
    _send(broker, worker_app, interrupted_file, interrupted, block=True)
    _wait_for(
        lambda: interrupted.id.encode() in broker.lrange(worker_app.STARTED_KEY, 0, -1),
        60,
        "worker A to start the stage",
    )
    live = queue_snapshot(broker)[_QUEUE]
    assert live["reserved"] == 1, live  # a live, leased stage is held work

    # Kill the whole worker -- prefork parent included -- with no chance to clean up.
    os.killpg(worker_a.pid, signal.SIGKILL)
    worker_a.wait(timeout=10)
    # Within one lease TTL plus the stale grace, the dead stage stops counting as held work.
    dead = _wait_for(
        lambda: (lambda q: q if q["orphaned"] == 1 else None)(queue_snapshot(broker)[_QUEUE]),
        _LEASE_TTL + _STALE + 10,
        "the dead stage to read as orphaned",
    )

    # (1) The defect: kombu keeps the message, unacked, and will for the visibility timeout.
    assert broker.hlen(Channel.unacked_key) == 1
    # (2) ...but it is no longer counted as work a worker holds.
    assert dead["reserved"] == 0, dead
    assert dead["orphaned"] == 1, dead
    assert dead["oldest_unacked_age"] >= _LEASE_TTL

    # A file submitted after the kill, same priority, already waiting when the reaper runs.
    _send(broker, worker_app, newer_file, newer, block=False)

    with Connection(
        _redis_url(), transport_options=dict(worker_app.app.conf.broker_transport_options)
    ) as conn:
        summary = reclaim_orphaned_deliveries(connection=conn, held_ids=set())

    assert summary["requeued"] == 1, summary
    assert summary["lost_runs"] == 0, summary  # the newer file is queued, not lost
    # (3) Out of unacked -- it can never come back as a six-hour duplicate -- and at the head.
    assert broker.hlen(Channel.unacked_key) == 0
    waiting = broker.lrange(_priority_key(_PRIORITY), 0, -1)
    assert len(waiting) == 2
    assert interrupted.id.encode() in waiting[-1]  # BRPOP takes the right end first
    assert queue_snapshot(broker)[_QUEUE]["pending"] == 2

    # The interrupted file is still in flight -- never failed, never ERROR.
    db_session.expire_all()
    assert db_session.query(Task).filter(Task.id == interrupted.id).one().status == "in_progress"
    assert (
        db_session.query(MediaFile).filter(MediaFile.id == interrupted_file.id).one().status
        == FileStatus.PROCESSING
    )

    broker.delete(worker_app.BLOCK_KEY.format(task_id=interrupted.id))
    broker.rpush(worker_app.RELEASE_KEY.format(task_id=interrupted.id), "go")
    workers()
    done = _wait_for(
        lambda: (lambda d: d if len(d) >= 2 else None)(broker.lrange(worker_app.DONE_KEY, 0, -1)),
        60,
        "worker B to finish both stages",
    )
    assert [d.decode() for d in done] == [interrupted.id, newer.id]


def test_a_cold_shutdown_drops_a_running_stage_and_the_reaper_still_requeues_the_file(
    broker, worker_app, workers, db_session, normal_user, monkeypatch
):
    """What Celery's soft/cold shutdown really does here -- and why the reaper must not trust it.

    Verified on celery 5.6 with real Redis: a COLD shutdown (SIGQUIT; SIGTERM too under
    ``REMAP_SIGTERM=SIGQUIT``) waits ``worker_soft_shutdown_timeout``, then cancels the running
    ``acks_late`` task -- and that cancel ACKS its message. It is neither in ``unacked`` nor
    back on the queue: dropped. (A WARM shutdown, plain SIGTERM, never applies the soft timeout;
    it waits for running tasks with no limit.)

    So the stage must NOT leave its run looking "queued" on the way out (it would read as
    waiting for a week), and the reaper's lost-run half must re-dispatch the file.
    """
    from app.core.broker_orphans import reclaim_orphaned_deliveries
    from app.core.task_config import task_recovery_config
    from app.core.task_liveness import RunState
    from app.core.task_liveness import probe_runs
    from app.services.task_recovery_service import TaskRecoveryService

    monkeypatch.setattr(task_recovery_config, "BROKER_ORPHAN_STALE", 0)
    monkeypatch.setattr(
        "app.services.transcription_retry.session_scope", lambda: _bridged_scope(db_session)
    )
    redispatched: list[int] = []

    def _record_retry(_self, file_id, countdown=None):
        redispatched.append(file_id)
        return True

    monkeypatch.setattr(TaskRecoveryService, "schedule_file_retry", _record_retry)
    monkeypatch.setattr(
        "app.tasks.transcription.notifications.send_progress_notification",
        lambda *_a, **_kw: None,
    )
    media_file, run = _run_row(db_session, normal_user)
    worker = workers(ORPHAN_TEST_SOFT_SHUTDOWN="1")
    _send(broker, worker_app, media_file, run, block=True)
    _wait_for(
        lambda: run.id.encode() in broker.lrange(worker_app.STARTED_KEY, 0, -1),
        60,
        "the worker to start the stage",
    )

    os.kill(worker.pid, signal.SIGQUIT)
    worker.wait(timeout=60)

    # The verified Celery behaviour: the message is gone from the broker entirely.
    assert broker.hlen(Channel.unacked_key) == 0
    assert broker.llen(_priority_key(_PRIORITY)) == 0
    assert broker.lrange(worker_app.DONE_KEY, 0, -1) == []
    # ...and the run does not pretend to be queued.
    assert probe_runs([run.id])[run.id].state == RunState.DEAD

    with Connection(
        _redis_url(), transport_options=dict(worker_app.app.conf.broker_transport_options)
    ) as conn:
        summary = reclaim_orphaned_deliveries(connection=conn, held_ids=set())

    assert summary["lost_runs"] == 1, summary
    assert redispatched == [media_file.id]
    db_session.expire_all()
    assert db_session.query(Task).filter(Task.id == run.id).one().status == "failed"
    assert (
        db_session.query(MediaFile).filter(MediaFile.id == media_file.id).one().status
        == FileStatus.PENDING
    )


def _fetch_one(conn: Connection):
    """Take one message off the queue the way a worker does, leaving it unacked."""
    from kombu import Queue

    received = []
    with conn.Consumer(Queue(_QUEUE), callbacks=[lambda _body, msg: received.append(msg)]) as _c:
        conn.drain_events(timeout=5)
    return received[0]


def test_a_stage_that_stands_down_is_requeued_at_the_head_not_the_back(broker, worker_app):
    """Graceful stand-down (shutdown abort, broken CUDA context) must keep the file's place.

    kombu answers ``Reject(requeue=True)`` with an LPUSH -- the BACK of the queue, behind
    everything submitted while the stage ran. ``requeue_own_delivery_to_front`` moves the
    message to the head with kombu's own ``restore_by_tag`` instead.
    """
    from app.core.broker_orphans import requeue_own_delivery_to_front

    transport = dict(worker_app.app.conf.broker_transport_options)
    with Connection(_redis_url(), transport_options=transport) as conn:
        interrupted = worker_app.app.send_task(
            "transcription.preprocess",
            kwargs={"file_uuid": "f-old", "task_id": "run-old"},
            queue=_QUEUE,
            priority=_PRIORITY,
        )
        message = _fetch_one(conn)  # a worker picked up the older file...
        assert broker.hlen(Channel.unacked_key) == 1
        newer = worker_app.app.send_task(  # ...and a newer one arrived while it ran
            "transcription.preprocess",
            kwargs={"file_uuid": "f-new", "task_id": "run-new"},
            queue=_QUEUE,
            priority=_PRIORITY,
        )

        assert requeue_own_delivery_to_front(interrupted.id, connection=conn) is True
        message.reject(requeue=False)  # what the stage's Reject(requeue=False) does

        assert broker.hlen(Channel.unacked_key) == 0
        waiting = broker.lrange(_priority_key(_PRIORITY), 0, -1)
        assert interrupted.id.encode() in waiting[-1]  # next out (BRPOP takes the right end)
        assert newer.id.encode() in waiting[0]
        assert broker.exists("transcription_queued:run-old")


def test_kombu_reject_with_requeue_alone_sends_the_stage_to_the_back(broker, worker_app):
    """CONTROL for the test above: the plain requeue really does lose the file's place."""
    transport = dict(worker_app.app.conf.broker_transport_options)
    with Connection(_redis_url(), transport_options=transport) as conn:
        interrupted = worker_app.app.send_task(
            "transcription.preprocess",
            kwargs={"file_uuid": "f-old", "task_id": "run-old"},
            queue=_QUEUE,
            priority=_PRIORITY,
        )
        message = _fetch_one(conn)
        newer = worker_app.app.send_task(
            "transcription.preprocess",
            kwargs={"file_uuid": "f-new", "task_id": "run-new"},
            queue=_QUEUE,
            priority=_PRIORITY,
        )

        message.reject(requeue=True)

        waiting = broker.lrange(_priority_key(_PRIORITY), 0, -1)
        assert newer.id.encode() in waiting[-1]
        assert interrupted.id.encode() in waiting[0]
