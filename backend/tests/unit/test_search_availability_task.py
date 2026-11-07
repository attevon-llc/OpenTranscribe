"""``search.availability_reconcile`` is wired everywhere a task must be, and converges (#1101).

It is what makes a failed trash/restore stamp converge, so it has to be reachable by every
path that could lose one: registered and routed (a scoped dispatch after a failed stamp),
scheduled by beat and classified as mutating (the guarantee when the dispatch also failed),
and replayed after worker loss (each run recomputes from Postgres and assigns the value whole,
so a duplicate run reaches the same end state; pinned below).
"""

from __future__ import annotations

from typing import Any

import pytest
from celery.exceptions import Retry
from celery.schedules import crontab

from app.core import task_replay
from app.core.celery import celery_app
from app.core.celery_beat import BEAT_ENTRY_EFFECT
from app.core.celery_beat import BEAT_SCHEDULE
from app.core.celery_beat import BeatEffect
from app.core.celery_routing import TASK_ROUTES
from app.core.queues import CeleryQueues
from app.services.search import availability_stamp
from app.tasks import search_availability_task as mod

NAME = "search.availability_reconcile"


def test_it_is_registered_and_routed_to_utility():
    celery_app.loader.import_default_modules()
    assert NAME in celery_app.tasks
    assert TASK_ROUTES[NAME] == {"queue": CeleryQueues.UTILITY}


def test_beat_runs_it_quarter_hourly_on_utility_and_it_is_mutating():
    entry = BEAT_SCHEDULE["availability-reconcile"]
    assert entry["task"] == NAME
    assert entry["schedule"] == crontab(minute="8,23,38,53")
    assert entry["options"]["queue"] == "utility"
    assert "args" not in entry and "kwargs" not in entry, "beat must reconcile the whole trash"
    assert BEAT_ENTRY_EFFECT["availability-reconcile"] is BeatEffect.MUTATING


def test_it_is_replayed_after_worker_loss():
    assert task_replay.is_replayable(NAME)


def _outcome(incomplete: list[str] | None = None) -> availability_stamp.ReconcileOutcome:
    return availability_stamp.ReconcileOutcome(stamped_trashed=1, incomplete=incomplete or [])


def test_a_replay_issues_the_same_stamps(monkeypatch):
    """Replay idempotence: the run is a function of the rows, not of what a lost run did."""
    calls: list[Any] = []

    def _reconcile(file_ids=None, **_kw):
        calls.append(sorted(file_ids) if file_ids is not None else None)
        return _outcome()

    monkeypatch.setattr(availability_stamp, "reconcile", _reconcile)

    first = mod.availability_reconcile.run(file_ids=[3, 1])
    second = mod.availability_reconcile.run(file_ids=[3, 1])

    assert calls == [[1, 3], [1, 3]]
    assert first == second


def test_a_scoped_run_retries_while_opensearch_is_down(monkeypatch):
    def _down(*_a, **_k):
        raise ConnectionError("opensearch down")

    monkeypatch.setattr(availability_stamp, "reconcile", _down)
    with pytest.raises(Retry):
        mod.availability_reconcile.apply(kwargs={"file_ids": [1]}, throw=True)


def test_a_scoped_run_retries_while_documents_are_left_unstamped(monkeypatch):
    monkeypatch.setattr(
        availability_stamp, "reconcile", lambda *_a, **_k: _outcome(["transcript_chunks"])
    )
    with pytest.raises(Retry):
        mod.availability_reconcile.apply(kwargs={"file_ids": [1]}, throw=True)


def test_a_beat_run_fails_loudly_instead_of_retrying(monkeypatch):
    """The next tick is the beat run's retry; a failure must be recorded, not hidden."""

    def _down(*_a, **_k):
        raise ConnectionError("opensearch down")

    monkeypatch.setattr(availability_stamp, "reconcile", _down)
    with pytest.raises(ConnectionError):
        mod.availability_reconcile.apply(throw=True)


def test_a_failed_dispatch_is_left_to_the_beat_tick(monkeypatch, caplog):
    attempts: list[tuple[str, dict[str, Any]]] = []

    def _broker_down(name, **kw):
        attempts.append((name, kw["kwargs"]))
        raise ConnectionError("broker down")

    monkeypatch.setattr(celery_app, "send_task", _broker_down)
    with caplog.at_level("WARNING", logger=availability_stamp.logger.name):
        availability_stamp.dispatch_reconcile([1, 2])  # must not raise: the trash is committed

    assert attempts == [(NAME, {"file_ids": [1, 2]})]
    assert "the beat tick will converge" in caplog.text


def test_the_retry_backoff_is_capped():
    assert [mod._countdown(n) for n in range(7)] == [30, 60, 120, 240, 480, 900, 900]
