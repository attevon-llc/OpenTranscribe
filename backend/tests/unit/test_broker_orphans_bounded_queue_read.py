"""The orphan sweep never copies a whole oversized queue backlog into the worker.

``read_queued_run_ids`` runs on every reaper sweep (every 60 s) and on every health check
that judges a run lost. It used to ``LRANGE key 0 -1`` every priority list of every queue a
transcription stage can wait on, ``gpu`` included. The #1337 load test had ~15 GB of
oversized repair messages on ``gpu``: each sweep pulled all of it into the cpu-worker
(23 GB RSS, a 12-14 GB Redis client output buffer) and took 52 s.

These tests run against a real Redis and measure what Redis actually sent
(``INFO stats`` ``total_net_output_bytes``).
"""

from __future__ import annotations

import base64
import json

import pytest
from kombu.transport.redis import Channel

from app.core import broker_orphans
from app.core.queues import CeleryQueues

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]

MiB = 1024 * 1024


def _message(task: str, run_id: str | None, pad: int = 0) -> str:
    payload = {"task_id": run_id, "file_uuid": f"file-{run_id}", "pad": "x" * pad}
    body = base64.b64encode(json.dumps([[payload], {}, {}]).encode()).decode()
    return json.dumps(
        {
            "body": body,
            "content-encoding": "utf-8",
            "content-type": "application/json",
            "headers": {"id": f"stage-{run_id}", "task": task, "eta": None},
            "properties": {"body_encoding": "base64", "priority": 0, "delivery_info": {}},
        }
    )


def _key(queue: str, priority: int) -> str:
    return queue if priority == 0 else f"{queue}{Channel.sep}{priority}"


def _sent_bytes(client) -> int:
    return int(client.info("stats")["total_net_output_bytes"])


@pytest.fixture
def queue(local_model_redis, lm_namespace):
    name = f"{lm_namespace}:gpu"
    yield name
    local_model_redis.delete(*(_key(name, p) for p in range(10)))


def test_an_oversized_backlog_is_measured_not_read(local_model_redis, queue):
    client = local_model_redis
    client.lpush(_key(queue, 0), _message("transcription.gpu_transcribe", "run-1"))
    big = _message("speaker_embedding_consistency_repair_batch", None, pad=3 * MiB)
    client.lpush(_key(queue, 9), *([big] * 30))  # ~120 MB, over the byte budget

    before = _sent_bytes(client)
    result = broker_orphans.read_queued_run_ids(client, queues=[queue])
    sent = _sent_bytes(client) - before

    assert result is None, "an over-budget backlog must read as 'cannot tell'"
    assert sent < MiB, f"the sweep copied {sent / MiB:.0f} MiB out of Redis to decide that"


def test_more_messages_than_the_entry_budget_is_not_read(local_model_redis, queue, monkeypatch):
    monkeypatch.setattr(broker_orphans, "QUEUE_SCAN_MAX_ENTRIES", 50, raising=False)
    msg = _message("transcription.gpu_transcribe", "run-1")
    local_model_redis.lpush(_key(queue, 0), *([msg] * 51))

    assert broker_orphans.read_queued_run_ids(local_model_redis, queues=[queue]) is None


def test_a_backlog_within_budget_is_still_read_completely(local_model_redis, queue):
    """The control: the bound changes nothing for an ordinary queue."""
    client = local_model_redis
    client.lpush(_key(queue, 0), _message("transcription.gpu_transcribe", "run-1"))
    client.lpush(_key(queue, 4), _message("transcription.diarize_gpu", "run-2"))
    client.lpush(_key(queue, 9), _message("speaker_embedding_consistency_repair_batch", None))

    assert broker_orphans.read_queued_run_ids(client, queues=[queue]) == {"run-1", "run-2"}


class _Conn:
    def __init__(self, client):
        self.default_channel = type("Channel", (), {"client": client})()


def test_the_reaper_judges_no_run_lost_when_the_queues_cannot_be_read(
    local_model_redis, monkeypatch
):
    """A run first in line on an unreadable queue must not be re-dispatched as lost."""
    from app.services import transcription_retry

    offered: list[set[str]] = []

    def _record(exclude: set[str]) -> int:
        offered.append(exclude)
        return 0

    monkeypatch.setattr(transcription_retry, "recover_lost_runs", _record)
    monkeypatch.setattr(broker_orphans, "QUEUE_SCAN_MAX_ENTRIES", 5, raising=False)
    key = _key(CeleryQueues.CPU, 2)
    msg = _message("transcription.cpu_transcribe", "run-first-in-line")
    local_model_redis.lpush(key, *([msg] * 6))
    try:
        summary = broker_orphans.reclaim_orphaned_deliveries(connection=_Conn(local_model_redis))
    finally:
        local_model_redis.delete(key)

    assert offered == [], "lost-run recovery ran on a sweep that could not read the queues"
    assert summary.get("queues_unread") == 1
