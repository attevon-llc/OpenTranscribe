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
