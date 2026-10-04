"""Celery queue wait (issue #1172): the publish stamp and the per-task histogram.

Contract pinned here:

* Every publish carries ``x-ot-published-at`` (wall clock), stamped by the ONE existing
  ``before_task_publish`` handler in ``app/core/celery.py``. A stamp already on the same
  message is kept; a retry is a new message (same id, ``retries`` + 1) and gets a fresh stamp
  even though Celery copies the previous message's custom headers onto it.
* At ``task_prerun`` the worker observes ``max(0, now - published_at)`` into
  ``celery_task_queue_wait_seconds{queue, task}``. A countdown/ETA message is measured from
  when it became due, not from when it was published.
* No stamp (an older producer) -> no observation, one ``celery_task_queue_wait_missing_total``.
* ``queue`` is a configured queue name or ``other``; ``task`` reuses ``task_label``. Both
  label sets are bounded no matter what arrives.
"""

from __future__ import annotations

import weakref
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest
from celery import Celery
from celery import signals

from app.core import queue_wait
from app.core import worker_metrics
from app.core.constants import CeleryQueues

HEADER = "x-ot-published-at"


@pytest.fixture(autouse=True)
def _wired():
    worker_metrics.connect_signals()


@pytest.fixture
def demo_app():
    app = Celery("queue-wait-test", set_as_current=False)

    @app.task(name="demo.transcribe")
    def transcribe():  # pragma: no cover - never executed, only registered
        return None

    return app


@pytest.fixture
def recorder(monkeypatch):
    """A recorder on its own registry with a controllable wall clock."""
    wall = SimpleNamespace(now=1_000.0)
    rec = worker_metrics.WorkerTaskMetrics(wall_clock=lambda: wall.now)
    monkeypatch.setattr(worker_metrics, "_metrics", rec)
    rec.wall_state = wall  # type: ignore[attr-defined]
    return rec


def _start(task, *, routing_key="cpu", headers=None, eta=None, task_id="t-1"):
    """Run ``task_prerun`` with ``task.request`` shaped like a worker delivery."""
    request = {
        "id": task_id,
        "called_directly": False,  # what the worker's tracer sets; Context defaults to True
        "delivery_info": {"routing_key": routing_key, "exchange": ""},
        "eta": eta,
        **(headers or {}),
    }
    task.push_request(**request)
    try:
        worker_metrics.on_task_prerun(sender=task, task_id=task_id, task=task)
    finally:
        task.pop_request()


def _wait(rec, suffix: str, labels: dict[str, str]) -> float | None:
    value = rec.registry.get_sample_value(f"celery_task_queue_wait_seconds_{suffix}", labels)
    return None if value is None else float(value)


def _missing(rec, queue: str) -> float:
    value = rec.registry.get_sample_value("celery_task_queue_wait_missing_total", {"queue": queue})
    return value or 0.0


class TestPublishStamp:
    def test_a_publish_is_stamped_with_the_wall_clock(self, monkeypatch):
        monkeypatch.setattr(queue_wait.time, "time", lambda: 1234.5)
        headers = {"id": "abc", "retries": 0}

        queue_wait.stamp_published_at(headers)

        assert headers[HEADER] == 1234.5

    def test_the_existing_handler_stamps_every_publish(self, monkeypatch):
        from app.core.celery import inject_request_id_header

        monkeypatch.setattr(queue_wait.time, "time", lambda: 50.0)
        headers = {"id": "abc", "retries": 0}

        signals.before_task_publish.send(sender="demo.transcribe", headers=headers, body=None)

        assert headers[HEADER] == 50.0
        # Extended, not duplicated: app.core.celery owns ONE before_task_publish receiver.
        receivers = [
            ref() if isinstance(ref, weakref.ReferenceType) else ref
            for _key, ref in signals.before_task_publish.receivers
        ]
        ours = [r for r in receivers if getattr(r, "__module__", "") == "app.core.celery"]
        assert ours == [inject_request_id_header]

    def test_a_stamp_already_on_the_same_message_is_kept(self, monkeypatch):
        headers = {"id": "abc", "retries": 0}
        monkeypatch.setattr(queue_wait.time, "time", lambda: 10.0)
        queue_wait.stamp_published_at(headers)
        monkeypatch.setattr(queue_wait.time, "time", lambda: 99.0)

        queue_wait.stamp_published_at(headers)

        assert headers[HEADER] == 10.0

    def test_a_retry_is_a_new_message_and_gets_a_fresh_stamp(self, monkeypatch, demo_app):
        """Celery's retry re-sends ``request.headers``, i.e. the previous message's stamp."""
        task = demo_app.tasks["demo.transcribe"]
        monkeypatch.setattr(queue_wait.time, "time", lambda: 10.0)
        first = {"id": "abc", "retries": 0}
        queue_wait.stamp_published_at(first)

        task.push_request(
            id="abc",
            retries=0,
            delivery_info={"routing_key": "cpu", "exchange": ""},
            **{k: v for k, v in first.items() if k.startswith("x-ot-")},
        )
        try:
            inherited = task.signature_from_request().options["headers"]
        finally:
            task.pop_request()
        assert inherited[HEADER] == 10.0, "precondition: retry carries the old stamp"

        monkeypatch.setattr(queue_wait.time, "time", lambda: 70.0)
        retry_headers = {"id": "abc", "retries": 1, **inherited}
        queue_wait.stamp_published_at(retry_headers)

        assert retry_headers[HEADER] == 70.0

    def test_a_child_task_inheriting_headers_gets_its_own_stamp(self, monkeypatch):
        monkeypatch.setattr(queue_wait.time, "time", lambda: 10.0)
        parent = {"id": "parent", "retries": 0}
        queue_wait.stamp_published_at(parent)
        monkeypatch.setattr(queue_wait.time, "time", lambda: 30.0)
        child = {**parent, "id": "child"}

        queue_wait.stamp_published_at(child)

        assert child[HEADER] == 30.0


class TestQueueWaitHistogram:
    def test_the_wait_from_publish_to_start_is_observed(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        recorder.wall_state.now = 1_007.0

        _start(task, headers={HEADER: 1_000.0})

        labels = {"queue": "cpu", "task": "demo.transcribe"}
        assert _wait(recorder, "count", labels) == 1
        assert _wait(recorder, "sum", labels) == pytest.approx(7.0)
        assert _wait(recorder, "bucket", {**labels, "le": "5.0"}) == 0
        assert _wait(recorder, "bucket", {**labels, "le": "10.0"}) == 1
        assert _missing(recorder, "cpu") == 0

    def test_a_stamp_in_the_future_is_clamped_to_zero(self, recorder, demo_app):
        """Producer clock ahead of the worker's: never a negative observation."""
        task = demo_app.tasks["demo.transcribe"]
        recorder.wall_state.now = 1_000.0

        _start(task, headers={HEADER: 1_030.0})

        labels = {"queue": "cpu", "task": "demo.transcribe"}
        assert _wait(recorder, "sum", labels) == 0.0
        assert _wait(recorder, "bucket", {**labels, "le": "0.1"}) == 1

    def test_a_message_without_a_stamp_is_counted_not_observed(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]

        _start(task, routing_key="gpu")
        _start(task, routing_key="gpu", headers={HEADER: "not-a-number"})

        assert _wait(recorder, "count", {"queue": "gpu", "task": "demo.transcribe"}) is None
        assert _missing(recorder, "gpu") == 2

    def test_a_stamp_serialized_as_text_is_still_read(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        recorder.wall_state.now = 1_002.0

        _start(task, headers={HEADER: "1000.5"})

        labels = {"queue": "cpu", "task": "demo.transcribe"}
        assert _wait(recorder, "sum", labels) == pytest.approx(1.5)

    def test_a_countdown_is_measured_from_when_the_message_became_due(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        due = datetime.fromtimestamp(1_060.0, tz=UTC).isoformat()
        recorder.wall_state.now = 1_064.0

        _start(task, headers={HEADER: 1_000.0}, eta=due)

        labels = {"queue": "cpu", "task": "demo.transcribe"}
        assert _wait(recorder, "sum", labels) == pytest.approx(4.0)

    def test_the_queue_label_comes_from_the_routing_key(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        recorder.wall_state.now = 1_001.0

        _start(task, routing_key="gpu-diarize", headers={HEADER: 1_000.0})
        _start(task, routing_key="made-up-queue", headers={HEADER: 1_000.0})
        _start(task, routing_key=None, headers={HEADER: 1_000.0})

        assert _wait(recorder, "count", {"queue": "gpu-diarize", "task": "demo.transcribe"}) == 1
        assert _wait(recorder, "count", {"queue": "other", "task": "demo.transcribe"}) == 2

    def test_an_unregistered_task_is_labelled_other(self, recorder, demo_app):
        stranger = demo_app.tasks["demo.transcribe"]
        recorder.wall_state.now = 1_001.0
        stranger.push_request(
            id="s", called_directly=False, delivery_info={"routing_key": "cpu"}, **{HEADER: 1_000.0}
        )
        try:
            fake = SimpleNamespace(name="never.registered", app=demo_app, request=stranger.request)
            worker_metrics.on_task_prerun(sender=fake, task_id="s", task=fake)
        finally:
            stranger.pop_request()

        assert _wait(recorder, "count", {"queue": "cpu", "task": "other"}) == 1

    def test_an_eager_run_is_not_a_queue_wait(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        task.push_request(id="e", called_directly=False, is_eager=True, delivery_info=None)
        try:
            worker_metrics.on_task_prerun(sender=task, task_id="e", task=task)
        finally:
            task.pop_request()

        assert _missing(recorder, "other") == 0

    def test_label_cardinality_is_bounded(self, recorder, demo_app):
        registered = demo_app.tasks["demo.transcribe"]
        recorder.wall_state.now = 1_001.0
        for i in range(300):
            _start(registered, routing_key=f"queue-{i}", headers={HEADER: 1_000.0})
            _start(registered, routing_key=f"queue-{i}")
            fake = SimpleNamespace(name=f"generated.{i}", app=demo_app, request=None)
            registered.push_request(
                id="x",
                called_directly=False,
                delivery_info={"routing_key": CeleryQueues.ALL[i % len(CeleryQueues.ALL)]},
                **{HEADER: 1.0},
            )
            try:
                fake.request = registered.request
                worker_metrics.on_task_prerun(sender=fake, task_id="x", task=fake)
            finally:
                registered.pop_request()

        wait_labels = set()
        missing_labels = set()
        for metric in recorder.registry.collect():
            for sample in metric.samples:
                if sample.name == "celery_task_queue_wait_seconds_count":
                    wait_labels.add((sample.labels["queue"], sample.labels["task"]))
                elif sample.name == "celery_task_queue_wait_missing_total":
                    missing_labels.add(sample.labels["queue"])

        allowed_queues = set(CeleryQueues.ALL) | {"other"}
        assert {q for q, _ in wait_labels} <= allowed_queues
        assert {t for _, t in wait_labels} == {"demo.transcribe", "other"}
        assert missing_labels == {"other"}
        assert len(wait_labels) <= len(allowed_queues) * 2

    def test_buckets_are_a_tenth_of_a_second_to_two_hours(self):
        assert worker_metrics.QUEUE_WAIT_BUCKETS == (
            0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0,
            300.0, 600.0, 1200.0, 1800.0, 3600.0, 7200.0,
        )  # fmt: skip

    def test_a_recording_error_never_reaches_the_task(self, recorder, demo_app, monkeypatch):
        calls: list[object] = []

        def explode(task):
            calls.append(task)
            raise RuntimeError("registry is broken")

        monkeypatch.setattr(recorder, "task_dequeued", explode)
        task = demo_app.tasks["demo.transcribe"]

        _start(task, headers={HEADER: 1.0})  # returns normally although the recorder raised

        assert calls == [task]
        assert "t-1" in recorder._started, "the run-time start is recorded regardless"

    def test_nothing_is_recorded_while_disabled(self, monkeypatch, demo_app):
        idle = worker_metrics.WorkerTaskMetrics()
        monkeypatch.setattr(worker_metrics, "_metrics", None)

        _start(demo_app.tasks["demo.transcribe"], headers={HEADER: 1.0})

        assert _wait(idle, "count", {"queue": "cpu", "task": "demo.transcribe"}) is None


class TestHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(12.5, 12.5), (7, 7.0), ("3.25", 3.25), (None, None), ("x", None), (True, None),
         (float("nan"), None), (float("inf"), None)],
    )  # fmt: skip
    def test_published_at_parsing(self, raw, expected):
        assert queue_wait.parse_published_at(raw) == expected

    def test_queue_label_folding(self):
        for name in CeleryQueues.ALL:
            assert queue_wait.queue_label(name) == name
        assert queue_wait.queue_label("nope") == "other"
        assert queue_wait.queue_label(None) == "other"
        assert queue_wait.queue_label(b"cpu") == "other"
