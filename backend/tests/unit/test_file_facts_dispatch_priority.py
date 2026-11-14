"""The ``file_facts`` backfill queues behind live work.

The hourly ``search_index_maintenance`` backfill queues up to 200 ``artifacts.generate_file_facts``
tasks at once, at the priority the transcription pipeline uses for a file a user is waiting on.
It now sends ``NLPPriority.BACKGROUND``: with Redis priorities (0 first, ``priority_steps``
0-9, ``queue_order_strategy="priority"``, prefetch 1) a new file's facts are still taken first.
A Celery ``rate_limit`` would not bound the burst; it is per worker instance, not global.
Asserted on the dispatch call, never by timing a queue.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.queues import NLPPriority
from app.tasks import ingest_artifacts_task
from app.tasks import search_maintenance_task


def test_the_backfill_queues_behind_files_a_user_just_transcribed(monkeypatch) -> None:
    """The batch is queued at once; Redis priorities (0 first) keep live work ahead of it."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        ingest_artifacts_task,
        "dispatch_file_facts",
        lambda file_id, **kwargs: calls.append({"file_id": file_id, **kwargs}),
    )
    db = MagicMock()
    query = db.query.return_value.filter.return_value
    query.order_by.return_value.count.return_value = 2
    query.order_by.return_value.limit.return_value.all.return_value = [(9,), (8,)]
    stats: dict[str, Any] = {}

    search_maintenance_task._dispatch_facts_backfill(db, stats, batch_size=2)

    assert calls == [
        {"file_id": 9, "priority": NLPPriority.BACKGROUND},
        {"file_id": 8, "priority": NLPPriority.BACKGROUND},
    ]
    assert stats["facts_backfill_dispatched"] == 2


@pytest.mark.parametrize(
    ("kwargs", "priority"),
    [({}, NLPPriority.AUTO_PIPELINE), ({"priority": NLPPriority.BACKGROUND}, 9)],
)
def test_the_dispatch_sends_the_priority_with_the_message(monkeypatch, kwargs, priority) -> None:
    task = MagicMock()
    monkeypatch.setattr(ingest_artifacts_task, "generate_file_facts_task", task)

    assert ingest_artifacts_task.dispatch_file_facts(7, **kwargs) is True

    task.apply_async.assert_called_once_with(
        kwargs={"file_id": 7, "pipeline_task_id": None}, priority=priority
    )
