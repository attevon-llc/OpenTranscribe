"""Every celery-beat entry says what it does to persisted state (issue #898).

``BEAT_ENTRY_EFFECT`` is what the inertness checker (``app/core/inert.py``) reads to tell a
deployment that must stay unchanged, such as the public demo, which scheduled jobs would
change it. A new entry without a classification fails here.
"""

from __future__ import annotations

from celery.beat import ScheduleEntry

from app.core.celery_beat import BEAT_ENTRY_EFFECT
from app.core.celery_beat import BEAT_SCHEDULE
from app.core.celery_beat import BeatEffect


def test_every_beat_entry_is_classified_and_nothing_else_is():
    assert sorted(BEAT_SCHEDULE) == sorted(BEAT_ENTRY_EFFECT)
    assert {type(effect) for effect in BEAT_ENTRY_EFFECT.values()} == {BeatEffect}


def test_the_classification_does_not_leak_into_the_schedule_celery_builds():
    from app.core.celery import celery_app

    entries = [
        ScheduleEntry(name=name, app=celery_app, **entry) for name, entry in BEAT_SCHEDULE.items()
    ]

    assert [entry.name for entry in entries] == list(BEAT_SCHEDULE)


def test_only_the_cache_refresh_is_read_only():
    read_only = sorted(n for n, e in BEAT_ENTRY_EFFECT.items() if e is BeatEffect.READ_ONLY)

    assert read_only == ["gpu-stats-update"]
