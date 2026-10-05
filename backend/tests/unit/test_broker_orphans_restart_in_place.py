"""A worker restarted IN PLACE does not shield its predecessor's deliveries (issue #1179).

The observation that prompted this: a GPU worker container was OOM-killed and restarted in
the same pod several times (same hostname, so the same Celery node name), the oldest unacked
GPU message aged past 20 minutes, and the orphaned-delivery sweep reported ``orphaned: 0``.
The hypothesis was that the sweep judges "a live consumer holds it" by worker hostname, which a
restarted process shares with the one that died.

These tests pin the decision logic the sweep actually uses, from the same inputs a restart in
place produces: the inspect broadcast answered by a node with the SAME name as the dead one,
reporting only what the NEW process holds. Liveness is decided per delivery -- by the run's
lease for a transcription stage, by the per-task heartbeat for anything else, and by the Celery
MESSAGE ID in the live workers' replies -- never by a node or host name. The new process starts
with an empty request buffer, so its predecessor's message ids are never among its replies.

The end-to-end version (real worker SIGKILLed, a new worker started under the same node name,
the real inspect broadcast) is
``tests/integration/test_orphaned_delivery_recovery_live.py::
test_a_worker_restarted_in_place_does_not_shield_its_predecessors_stage``.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from app.core import broker_orphans
from app.core.task_config import task_recovery_config
from tests.unit._fake_liveness_redis import FakeRedis
from tests.unit._fake_liveness_redis import install_fake_redis

NODE = "celery@gpu-worker-0"  # the restarted process keeps the dead one's node name


def _entry(task: str, *, stage_id: str, args=None, kwargs=None, queue: str = "gpu") -> str:
    body = base64.b64encode(json.dumps([args or [], kwargs or {}, {}]).encode()).decode()
    message = {
        "body": body,
        "content-encoding": "utf-8",
        "content-type": "application/json",
        "headers": {"id": stage_id, "task": task, "eta": None},
        "properties": {"body_encoding": "base64", "priority": 2, "delivery_info": {}},
    }
    return json.dumps([message, queue, queue])


def _gpu_stage(run_id: str, stage_id: str) -> str:
    payload = {"task_id": run_id, "file_uuid": f"file-{run_id}", "file_id": 1}
    return _entry("transcription.gpu_transcribe", stage_id=stage_id, args=[payload])


class _BrokerPipe:
    def __init__(self, broker: _Broker):
        self._broker = broker
        self._ops: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self._ops.append((name, args))
            return self

        return _record

    def execute(self) -> list[Any]:
        out: list[Any] = []
        for name, args in self._ops:
            if name == "hgetall":
                out.append(dict(self._broker.unacked))
            elif name == "zrange":
                out.append(list(self._broker.delivered.items()))
            elif name == "incr":
                self._broker.counters[args[0]] = self._broker.counters.get(args[0], 0) + 1
                out.append(self._broker.counters[args[0]])
            else:
                out.append(None)
        return out


class _Broker:
    """The broker's ``unacked`` hash + index, as kombu's Redis transport keeps them."""

    def __init__(self, unacked: dict[str, str], delivered: dict[str, float]):
        self.unacked = unacked
        self.delivered = delivered
        self.counters: dict[str, int] = {}

    def pipeline(self, transaction=True):
        return _BrokerPipe(self)


class _Conn:
    def __init__(self, broker: _Broker):
        self.default_channel = type("Channel", (), {"client": broker})()


class _Inspect:
    """``celery_app.control.inspect()`` answered by the restarted node only."""

    def __init__(self, active: list[str]):
        self._active = active

    def active(self):
        return {NODE: [{"id": i, "hostname": NODE} for i in self._active]}

    def reserved(self):
        return {NODE: []}

    def scheduled(self):
        return {NODE: []}


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    monkeypatch.setattr(task_recovery_config, "BROKER_ORPHAN_STALE", 120)
    monkeypatch.setattr(task_recovery_config, "BROKER_ORPHAN_UNTRACKED_STALE", 600)
    return install_fake_redis(monkeypatch)


@pytest.fixture
def restarted_node(monkeypatch):
    """Answer the inspect broadcast as the restarted process, under the dead one's name."""
    from app.core.celery import celery_app

    def _install(active: list[str]) -> None:
        monkeypatch.setattr(celery_app.control, "inspect", lambda timeout=3.0: _Inspect(active))

    return _install


@pytest.fixture
def actions(monkeypatch) -> dict[str, list[str]]:
    """Record what the sweep does instead of touching a real broker or database."""
    from app.services import transcription_retry

    done: dict[str, list[str]] = {"restored": [], "discarded": []}
    monkeypatch.setattr(transcription_retry, "run_status", lambda run_id: "current")
    monkeypatch.setattr(transcription_retry, "allow_infra_requeue", lambda key: (True, 1))
    monkeypatch.setattr(transcription_retry, "recover_lost_runs", lambda exclude: 0)

    def _restore(tag, connection=None):
        done["restored"].append(tag)
        return True

    monkeypatch.setattr(broker_orphans, "restore_to_front", _restore)
    monkeypatch.setattr(
        broker_orphans, "discard_delivery", lambda client, tag: done["discarded"].append(tag)
    )
    return done


def test_live_workers_are_identified_by_message_id_not_by_node_name(fake_redis, restarted_node):
    restarted_node(["stage-new"])

    held = broker_orphans.held_by_live_workers()

    assert held == {"stage-new"}
    assert NODE not in held


def test_a_restarted_process_does_not_shield_its_predecessors_stage(
    fake_redis, restarted_node, actions
):
    """The dead process's GPU stage: no lease (it died with the process), delivered long ago.
    The restarted process -- same node name -- runs a different stage and says so."""
    now = time.time()
    fake_redis.mark_running("run-new", started_seconds_ago=30)  # the new process's own run
    broker = _Broker(
        unacked={
            "tag-old": _gpu_stage("run-old", "stage-old"),
            "tag-new": _gpu_stage("run-new", "stage-new"),
        },
        delivered={"tag-old": now - 20 * 60, "tag-new": now - 30},
    )
    restarted_node(["stage-new"])

    summary = broker_orphans.reclaim_orphaned_deliveries(connection=_Conn(broker), now=now)

    assert actions["restored"] == ["tag-old"]
    assert summary["orphaned"] == 1, summary
    assert summary["requeued"] == 1, summary


def test_a_restarted_process_does_not_shield_its_predecessors_untracked_task(
    fake_redis, restarted_node, actions
):
    """A GPU-queue task with no run lease (a speaker task, say): judged by its own heartbeat
    and by message id, so the predecessor's copy is requeued as well."""
    now = time.time()
    broker = _Broker(
        unacked={
            "tag-old": _entry("speaker.extract_embeddings", stage_id="job-old"),
            "tag-new": _entry("speaker.extract_embeddings", stage_id="job-new"),
        },
        delivered={"tag-old": now - 20 * 60, "tag-new": now - 20 * 60},
    )
    fake_redis.setex("task_heartbeat:job-new", 120, "1")
    restarted_node(["job-new"])

    summary = broker_orphans.reclaim_orphaned_deliveries(connection=_Conn(broker), now=now)

    assert actions["restored"] == ["tag-old"]
    assert summary["requeued"] == 1, summary


def test_a_delivery_the_restarted_process_really_holds_is_left_alone(
    fake_redis, restarted_node, actions
):
    """The guard the sweep does rely on: the message id is in a live worker's reply."""
    now = time.time()
    broker = _Broker(
        unacked={"tag-held": _gpu_stage("run-held", "stage-held")},
        delivered={"tag-held": now - 20 * 60},
    )
    restarted_node(["stage-held"])

    summary = broker_orphans.reclaim_orphaned_deliveries(connection=_Conn(broker), now=now)

    assert actions["restored"] == []
    assert summary["orphaned"] == 0, summary
