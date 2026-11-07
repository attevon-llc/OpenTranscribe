"""The hourly ``gdpr.erasure_execute_due`` tick (#1105): the latency of a scheduled erasure.

The daily ``gdpr.erasure_reconcile`` sweep stays the guarantee (its phase 0 runs the same
executor); the hourly tick keeps a 72-hour organization erasure from running up to a day late.
"""

from __future__ import annotations

from app.core.celery_beat import BEAT_ENTRY_EFFECT
from app.core.celery_beat import BEAT_SCHEDULE
from app.core.celery_beat import BeatEffect
from app.core.celery_routing import TASK_ROUTES

TASK = "gdpr.erasure_execute_due"


def test_the_tick_is_scheduled_hourly_on_the_utility_queue_and_classified_mutating():
    [(name, entry)] = [(n, e) for n, e in BEAT_SCHEDULE.items() if e["task"] == TASK]
    schedule = entry["schedule"]
    assert schedule.hour == set(range(24))
    assert schedule.minute == {35}
    assert entry["options"]["queue"] == "utility"
    assert TASK_ROUTES[TASK]["queue"] == "utility"
    assert BEAT_ENTRY_EFFECT[name] is BeatEffect.MUTATING


def test_the_task_is_registered_with_the_worker():
    from app.core.celery import celery_app

    celery_app.loader.import_default_modules()
    assert TASK in celery_app.tasks


def test_the_tick_runs_the_scheduled_erasure_executor(monkeypatch):
    from app.services import scheduled_erasure_service
    from app.tasks.erasure_reconciliation import erasure_execute_due

    monkeypatch.setattr(
        scheduled_erasure_service,
        "execute_due_requests",
        lambda: {"due": 2, "executed": 1, "failures": 1},
    )

    assert erasure_execute_due.run() == {"status": "ok", "due": 2, "executed": 1, "failures": 1}
