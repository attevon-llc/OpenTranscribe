"""Per-task Celery worker metrics (issue #1161): ``app/core/worker_metrics.py``.

Contract pinned here:

* ``WORKER_METRICS_PORT`` unset/empty/invalid = no listener, no recording.
* ``celery_task_total{task, outcome}`` with outcome in success/failure/retry/revoked, and
  ``celery_task_runtime_seconds{task}``; ``task`` is the registered task name, and any name
  the app does not know is folded into one ``other`` series rather than dropped.
* Prefork children's counts reach the ONE listener in the worker's main process, including
  children that have already exited (``--max-tasks-per-child`` recycling).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest
from celery import Celery
from celery import signals

from app.core import worker_metrics

WORKER_ARGV = ["celery", "-A", "app.core.celery", "worker", "-Q", "cpu"]
BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _wired():
    worker_metrics.connect_signals()


@pytest.fixture
def demo_app():
    app = Celery("worker-metrics-test", set_as_current=False)

    @app.task(name="demo.transcribe")
    def transcribe():  # pragma: no cover - never executed, only registered
        return None

    @app.task(name="demo.summarize")
    def summarize():  # pragma: no cover - never executed, only registered
        return None

    return app


@pytest.fixture
def recorder(monkeypatch):
    """A live recorder on its own registry, installed as the module's active one."""
    clock = SimpleNamespace(now=0.0)
    rec = worker_metrics.WorkerTaskMetrics(clock=lambda: clock.now)
    monkeypatch.setattr(worker_metrics, "_metrics", rec)
    rec.clock_state = clock  # type: ignore[attr-defined]
    return rec


class _UnregisteredTask:
    """A sender the app has not registered (signal senders must be hashable, like tasks)."""

    def __init__(self, name, app):
        self.name = name
        self.app = app


def _count(rec, task: str, outcome: str) -> float:
    value = rec.registry.get_sample_value("celery_task_total", {"task": task, "outcome": outcome})
    return value or 0.0


class TestConfiguration:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_the_listener_is_off_by_default(self, raw, monkeypatch):
        environ = {} if raw is None else {"WORKER_METRICS_PORT": raw}
        started = []
        monkeypatch.setattr(worker_metrics, "_metrics", None)
        monkeypatch.setattr(worker_metrics, "_start_http_server", lambda *a, **k: started.append(a))

        assert worker_metrics.metrics_port(environ) is None
        assert worker_metrics.configure(argv=WORKER_ARGV, environ=environ) is False
        assert worker_metrics.start_listener() is None
        assert started == []
        assert "PROMETHEUS_MULTIPROC_DIR" not in environ

    @pytest.mark.parametrize("raw", ["abc", "0", "-1", "65536", "80.5"])
    def test_an_invalid_port_disables_rather_than_crashing_the_worker(self, raw):
        assert worker_metrics.metrics_port({"WORKER_METRICS_PORT": raw}) is None

    def test_a_valid_port_is_parsed(self):
        assert worker_metrics.metrics_port({"WORKER_METRICS_PORT": " 9808 "}) == 9808

    @pytest.mark.parametrize(
        "argv",
        [
            ["uvicorn", "app.main:app"],
            ["celery", "-A", "app.core.celery", "beat"],
            ["celery", "-A", "app.core.celery", "flower"],
            ["celery", "-A", "app.core.celery", "inspect", "ping"],
            ["pytest", "tests/unit/test_worker_task_metrics.py"],
        ],
    )
    def test_only_a_celery_worker_process_enables_it(self, argv, monkeypatch, tmp_path):
        # .env is shared by every service, so the API and beat see the same variable.
        monkeypatch.setattr(worker_metrics, "_metrics", None)
        environ = {"WORKER_METRICS_PORT": "9808", "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
        assert worker_metrics.configure(argv=argv, environ=environ) is False
        assert worker_metrics._metrics is None

    def test_python_dash_m_celery_worker_counts_as_a_worker(self):
        assert worker_metrics.is_worker_command(
            ["/usr/lib/python3/site-packages/celery/__main__.py", "-A", "x", "worker"]
        )


class TestOutcomeCounting:
    def test_each_signal_increments_its_own_outcome_for_the_registered_name(
        self, recorder, demo_app
    ):
        task = demo_app.tasks["demo.transcribe"]
        other = demo_app.tasks["demo.summarize"]

        signals.task_success.send(sender=task, result=None)
        signals.task_success.send(sender=task, result=None)
        signals.task_failure.send(sender=task, task_id="t2", exception=ValueError("x"))
        signals.task_retry.send(sender=task, request=None, reason="later", einfo=None)
        signals.task_revoked.send(
            sender=other, request=None, terminated=False, signum=None, expired=False
        )

        assert _count(recorder, "demo.transcribe", "success") == 2
        assert _count(recorder, "demo.transcribe", "failure") == 1
        assert _count(recorder, "demo.transcribe", "retry") == 1
        assert _count(recorder, "demo.transcribe", "revoked") == 0
        assert _count(recorder, "demo.summarize", "revoked") == 1
        assert _count(recorder, "demo.summarize", "success") == 0

    def test_an_unregistered_task_name_is_counted_as_other_not_dropped(self, recorder, demo_app):
        stranger = _UnregisteredTask("something.never.registered", demo_app)
        nameless = _UnregisteredTask(None, demo_app)

        signals.task_failure.send(sender=stranger, task_id="t", exception=RuntimeError())
        signals.task_failure.send(sender=nameless, task_id="u", exception=RuntimeError())

        assert _count(recorder, "other", "failure") == 2
        assert _count(recorder, "something.never.registered", "failure") == 0

    def test_label_cardinality_is_bounded_by_the_task_registry(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        for i in range(500):
            signals.task_success.send(
                sender=_UnregisteredTask(f"generated.{i}", demo_app), result=None
            )
            signals.task_success.send(sender=task, result=None)

        label_sets = {
            tuple(sorted(sample.labels.items()))
            for metric in recorder.registry.collect()
            if metric.name == "celery_task"
            for sample in metric.samples
            if sample.name == "celery_task_total"
        }
        tasks = {dict(labels)["task"] for labels in label_sets}
        assert tasks == {"demo.transcribe", "other"}
        assert _count(recorder, "other", "success") == 500
        assert _count(recorder, "demo.transcribe", "success") == 500

    def test_nothing_is_recorded_while_disabled(self, monkeypatch, demo_app):
        idle = worker_metrics.WorkerTaskMetrics()  # built, but never installed
        monkeypatch.setattr(worker_metrics, "_metrics", None)
        task = demo_app.tasks["demo.transcribe"]

        signals.task_success.send(sender=task, result=None)
        worker_metrics.on_task_prerun(sender=task, task_id="t", task=task)

        assert worker_metrics._metrics is None
        assert _count(idle, "demo.transcribe", "success") == 0
        assert idle._started == {}

    def test_a_recording_error_never_reaches_the_task(self, recorder, demo_app, monkeypatch):
        calls: list[str] = []

        def explode(name):
            def _raise(*_a, **_k):
                calls.append(name)
                raise RuntimeError("registry is broken")

            return _raise

        monkeypatch.setattr(recorder, "record_outcome", explode("outcome"))
        monkeypatch.setattr(recorder, "task_started", explode("started"))
        monkeypatch.setattr(recorder, "task_finished", explode("finished"))
        task = demo_app.tasks["demo.transcribe"]

        # Each call returns normally even though the recorder raised inside it.
        signals.task_failure.send(sender=task, task_id="t", exception=ValueError())
        worker_metrics.on_task_prerun(sender=task, task_id="t", task=task)
        worker_metrics.on_task_postrun(sender=task, task_id="t", task=task, state="SUCCESS")

        assert calls == ["outcome", "started", "finished"]


class TestRuntime:
    def test_the_histogram_observes_prerun_to_postrun(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        recorder.clock_state.now = 100.0
        worker_metrics.on_task_prerun(sender=task, task_id="run-1", task=task)
        recorder.clock_state.now = 103.25
        worker_metrics.on_task_postrun(sender=task, task_id="run-1", task=task, state="SUCCESS")

        sample = recorder.registry.get_sample_value
        labels = {"task": "demo.transcribe"}
        assert sample("celery_task_runtime_seconds_count", labels) == 1
        assert sample("celery_task_runtime_seconds_sum", labels) == pytest.approx(3.25)
        assert sample("celery_task_runtime_seconds_bucket", {**labels, "le": "2.5"}) == 0
        assert sample("celery_task_runtime_seconds_bucket", {**labels, "le": "5.0"}) == 1

    def test_buckets_span_half_a_second_to_two_hours(self):
        assert worker_metrics.RUNTIME_BUCKETS[0] == 0.5
        assert worker_metrics.RUNTIME_BUCKETS[-1] == 7200.0
        assert list(worker_metrics.RUNTIME_BUCKETS) == sorted(worker_metrics.RUNTIME_BUCKETS)

    def test_a_postrun_without_a_prerun_observes_nothing(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        worker_metrics.on_task_postrun(sender=task, task_id="never-started", task=task)
        assert (
            recorder.registry.get_sample_value(
                "celery_task_runtime_seconds_count", {"task": "demo.transcribe"}
            )
            is None
        )

    def test_start_times_cannot_grow_without_bound(self, recorder, demo_app):
        task = demo_app.tasks["demo.transcribe"]
        for i in range(worker_metrics.MAX_TRACKED_RUNS + 50):
            worker_metrics.on_task_prerun(sender=task, task_id=f"lost-{i}", task=task)
        assert len(recorder._started) <= worker_metrics.MAX_TRACKED_RUNS


class TestWiring:
    def test_the_celery_app_connects_every_handler(self):
        import app.core.celery  # noqa: F401 - importing wires the signals

        expected = {
            signals.task_prerun: worker_metrics.on_task_prerun,
            signals.task_postrun: worker_metrics.on_task_postrun,
            signals.task_success: worker_metrics.on_task_success,
            signals.task_failure: worker_metrics.on_task_failure,
            signals.task_retry: worker_metrics.on_task_retry,
            signals.task_revoked: worker_metrics.on_task_revoked,
            signals.worker_ready: worker_metrics.start_listener,
            signals.worker_process_shutdown: worker_metrics.mark_child_dead,
            signals.worker_shutdown: worker_metrics.remove_owned_dir,
        }
        for signal, handler in expected.items():
            live = [
                ref() if isinstance(ref, weakref.ReferenceType) else ref
                for _key, ref in signal.receivers
            ]
            assert handler in live, f"{handler.__name__} is not connected to {signal.name}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_prefork_children_including_recycled_ones_reach_the_one_listener(tmp_path):
    """Real fork, real files, real HTTP scrape -- in a subprocess, because multiprocess
    mode is a process-global switch the pytest process must not inherit."""
    port = _free_port()
    script = textwrap.dedent(
        f"""
        import os, sys, urllib.request
        from celery import Celery, signals
        from app.core import worker_metrics

        environ = {{"WORKER_METRICS_PORT": "{port}", "PROMETHEUS_MULTIPROC_DIR": {str(tmp_path)!r}}}
        stale = os.path.join({str(tmp_path)!r}, "counter_999999.db")
        open(stale, "wb").write(b"left over from the previous container run")
        assert worker_metrics.configure(argv={WORKER_ARGV!r}, environ=environ)
        assert not os.path.exists(stale), "stale files must be wiped at startup"
        os.environ.update(environ)
        worker_metrics.connect_signals()

        app = Celery("t", set_as_current=False)
        @app.task(name="demo.transcribe")
        def transcribe():
            return None
        task = app.tasks["demo.transcribe"]

        # Two prefork children: each records, then exits (max-tasks-per-child recycling).
        for _child in range(2):
            pid = os.fork()
            if pid == 0:
                signals.task_success.send(sender=task, result=None)
                signals.task_failure.send(sender=task, task_id="x", exception=ValueError())
                signals.worker_process_shutdown.send(sender=None, pid=os.getpid(), exitcode=0)
                os._exit(0)
            os.waitpid(pid, 0)

        # The main process records revocations itself.
        signals.task_revoked.send(sender=task, request=None, terminated=False, signum=None, expired=False)
        assert worker_metrics.start_listener() is not None
        body = urllib.request.urlopen("http://127.0.0.1:{port}/metrics", timeout=10).read().decode()
        print(body)
        # An operator-chosen directory is never removed at shutdown, only emptied at start.
        worker_metrics.remove_owned_dir()
        assert os.path.isdir({str(tmp_path)!r})
        """
    )
    env = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_MULTIPROC_DIR"}
    env.pop("WORKER_METRICS_PORT", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    body = result.stdout
    assert 'celery_task_total{outcome="success",task="demo.transcribe"} 2.0' in body
    assert 'celery_task_total{outcome="failure",task="demo.transcribe"} 2.0' in body
    assert 'celery_task_total{outcome="revoked",task="demo.transcribe"} 1.0' in body
