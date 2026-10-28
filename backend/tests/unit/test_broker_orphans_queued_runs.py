"""A run whose stage message WAITS on a queue list is not a lost run.

A cold shutdown hands the running stage back to the head of its queue (billiard 4.3.1+). Until
a worker takes it again the message is in neither the ``unacked`` hash nor held by a lease, so
the reaper's lost-run half used to fail the attempt and re-dispatch a second copy of a run
that was first in line. ``reclaim_orphaned_deliveries`` must therefore exclude runs that are
queued as well as runs that are unacked.

Live counterpart: ``tests/integration/test_orphaned_delivery_recovery_live.py``.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from kombu.transport.redis import Channel

from app.core import broker_orphans
from app.core.constants import CeleryQueues


def _message(task: str, *, run_id: str | None) -> str:
    payload = {"task_id": run_id, "file_uuid": f"file-{run_id}"} if run_id else {}
    body = base64.b64encode(json.dumps([[payload], {}, {}]).encode()).decode()
    return json.dumps(
        {
            "body": body,
            "content-encoding": "utf-8",
            "content-type": "application/json",
            "headers": {"id": f"stage-{run_id}", "task": task, "eta": None},
            "properties": {"body_encoding": "base64", "priority": 2, "delivery_info": {}},
        }
    )


class _Pipe:
    def __init__(self, client: _Client):
        self._client = client
        self._ops: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self._ops.append((name, args))
            return self

        return _record

    def execute(self):
        out: list[Any] = []
        for name, args in self._ops:
            if name == "lrange":
                self._client.read_keys.append(args[0])
                out.append(list(self._client.lists.get(args[0], [])))
            elif name in ("hgetall",):
                out.append({})
            elif name == "zrange":
                out.append([])
            else:
                out.append(None)
        return out


class _Client:
    def __init__(self, lists: dict[str, list[str]]):
        self.lists = lists
        self.read_keys: list[str] = []

    def pipeline(self, transaction=True):
        return _Pipe(self)


class _Conn:
    def __init__(self, client: _Client):
        self.default_channel = type("Channel", (), {"client": client})()


def _priority_key(queue: str, priority: int) -> str:
    return queue if priority == 0 else f"{queue}{Channel.sep}{priority}"


def test_a_stage_message_on_a_queue_list_names_its_run():
    raw = _message("transcription.cpu_transcribe", run_id="run-1")
    assert broker_orphans.queued_run_id(raw) == "run-1"


def test_a_non_stage_or_unreadable_message_names_no_run():
    assert broker_orphans.queued_run_id(_message("speaker.extract_embeddings", run_id="r")) is None
    assert broker_orphans.queued_run_id("not json") is None
    assert broker_orphans.queued_run_id("[]") is None


def test_queued_runs_are_read_from_every_queue_and_priority_list():
    client = _Client(
        {
            _priority_key(CeleryQueues.CPU, 2): [
                _message("transcription.cpu_transcribe", run_id="run-cpu")
            ],
            _priority_key(CeleryQueues.GPU, 0): [
                _message("transcription.gpu_transcribe", run_id="run-gpu"),
                _message("speaker.extract_embeddings", run_id=None),
            ],
        }
    )

    assert broker_orphans.read_queued_run_ids(client) == {"run-cpu", "run-gpu"}


def test_the_reaper_does_not_treat_a_run_first_in_line_as_lost(monkeypatch):
    from app.services import transcription_retry

    seen: list[set[str]] = []

    def _record(exclude: set[str]) -> int:
        seen.append(set(exclude))
        return 0

    monkeypatch.setattr(transcription_retry, "recover_lost_runs", _record)
    client = _Client(
        {
            _priority_key(CeleryQueues.CPU, 2): [
                _message("transcription.cpu_transcribe", run_id="run-queued")
            ]
        }
    )

    summary = broker_orphans.reclaim_orphaned_deliveries(connection=_Conn(client), now=time.time())

    assert "error" not in summary, summary
    assert seen == [{"run-queued"}], (
        "a run with its stage message waiting on a queue was offered to the lost-run "
        "recovery, which fails that attempt and dispatches a duplicate"
    )


def test_only_the_queues_a_transcription_stage_can_wait_on_are_read():
    assert set(broker_orphans.transcription_stage_queues()) == {
        CeleryQueues.GPU,
        CeleryQueues.CPU,
        CeleryQueues.CPU_TRANSCRIBE,
        CeleryQueues.GPU_TRANSCRIBE,
        CeleryQueues.GPU_DIARIZE,
        CeleryQueues.CLOUD_ASR,
    }


def test_every_call_time_routed_stage_declares_its_queues():
    """A stage dispatched with a call-time queue must be listed, or the reaper never looks."""
    from app.core.celery import INTENTIONALLY_UNROUTED_TASKS
    from app.core.celery import UNROUTED_STAGE_QUEUES

    unrouted_stages = set(INTENTIONALLY_UNROUTED_TASKS) & broker_orphans.TRANSCRIPTION_STAGES
    assert unrouted_stages == set(UNROUTED_STAGE_QUEUES)


def test_a_queue_no_stage_uses_is_never_read():
    client = _Client({_priority_key(CeleryQueues.DOWNLOAD, 0): ["would-be-parsed"]})

    broker_orphans.read_queued_run_ids(client)

    assert client.read_keys
    assert not [k for k in client.read_keys if k.startswith(CeleryQueues.DOWNLOAD)]


class _MovingClient(_Client):
    """A broker whose queued message only becomes visible on the second list read.

    Models a cold-shutdown restore (unacked -> list) landing between the sweep's first
    list read and its unacked read: a single read of each structure sees it in neither.
    """

    def __init__(self, message: str, key: str):
        super().__init__({})
        self._message = message
        self._key = key
        self.list_reads = 0

    def pipeline(self, transaction=True):
        pipe = _Pipe(self)
        outer = self
        original_execute = pipe.execute

        def _execute():
            if any(name == "lrange" for name, _ in pipe._ops):
                outer.list_reads += 1
                if outer.list_reads >= 2:
                    outer.lists[outer._key] = [outer._message]
            return original_execute()

        pipe.execute = _execute  # type: ignore[method-assign]
        return pipe


def test_a_stage_restored_between_the_two_reads_is_still_seen(monkeypatch):
    from app.services import transcription_retry

    seen: list[set[str]] = []

    def _record(exclude: set[str]) -> int:
        seen.append(set(exclude))
        return 0

    monkeypatch.setattr(transcription_retry, "recover_lost_runs", _record)
    client = _MovingClient(
        _message("transcription.cpu_transcribe", run_id="run-moving"),
        _priority_key(CeleryQueues.CPU, 2),
    )

    broker_orphans.reclaim_orphaned_deliveries(connection=_Conn(client), now=time.time())

    assert seen == [{"run-moving"}], (
        "a stage that reached its queue list after the first read was offered to the "
        "lost-run recovery, which would dispatch a duplicate"
    )
