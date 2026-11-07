"""The trash purge tasks are wired everywhere a beat task must be (issue #1101).

``test_beat_entry_effects`` / ``test_task_routes_complete`` fail on a task that is missing
from a registry; this pins what the trash's entries must SAY: the hourly purge runs on the
utility queue, is classified as mutating for the inertness checker, both tasks are registered
and routed, and neither is replayed after worker loss (the next hourly tick re-selects every
file still due, and a replayed duplicate run would race the first one's destroy).
"""

from __future__ import annotations

from celery.schedules import crontab

from app.core import task_replay
from app.core.celery import celery_app
from app.core.celery_beat import BEAT_ENTRY_EFFECT
from app.core.celery_beat import BEAT_SCHEDULE
from app.core.celery_beat import BeatEffect
from app.core.celery_routing import TASK_ROUTES
from app.core.queues import CeleryQueues

NAMES = ("files.purge_trash", "files.purge_trash_now")


def test_the_hourly_purge_is_scheduled_on_the_utility_queue_and_is_mutating():
    entry = BEAT_SCHEDULE["purge-trash"]
    assert entry["task"] == "files.purge_trash"
    assert entry["schedule"] == crontab(minute=45)
    assert entry["options"]["queue"] == "utility"
    assert BEAT_ENTRY_EFFECT["purge-trash"] is BeatEffect.MUTATING


def test_both_tasks_are_registered_and_routed_to_utility():
    celery_app.loader.import_default_modules()
    for name in NAMES:
        assert name in celery_app.tasks, name
        assert TASK_ROUTES[name] == {"queue": CeleryQueues.UTILITY}, name


def test_neither_task_is_replayed_after_worker_loss():
    for name in NAMES:
        assert not task_replay.is_replayable(name), name
