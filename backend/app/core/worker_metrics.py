"""Per-task Celery metrics, served by each worker on ``WORKER_METRICS_PORT`` (issue #1161).

What is recorded, from Celery signals inside the worker:

* ``celery_task_total{task, outcome}`` -- ``outcome`` is one of ``success`` (``task_success``),
  ``failure`` (``task_failure``, including a prefork child that died mid-task), ``retry``
  (``task_retry``) and ``revoked`` (``task_revoked``). A worker-lost task that
  ``reject_on_worker_lost`` puts back on the queue fires none of these: it has not ended.
* ``celery_task_runtime_seconds{task}`` -- ``task_prerun`` to ``task_postrun``, whatever the
  outcome.

``task`` is the registered task name. A name the app has not registered is counted under
``other`` -- not dropped, and not allowed to mint a series per name -- so the label set is
bounded by the task registry plus one. No ids, users or files ever become labels.

**Off by default.** With ``WORKER_METRICS_PORT`` unset, empty or invalid, nothing is
recorded and no socket is opened. ``.env`` is shared by every service, so the variable is
honoured only in a ``celery ... worker`` process; the API, beat and Flower ignore it.

**Why multiprocess mode, and how prefork is handled.** Under ``--pool=prefork`` the task
signals fire in the forked children, while only the main process can own the listening port
(N children cannot all bind it, and a child's server would vanish when
``--max-tasks-per-child`` recycles it). So a worker with the port set runs
``prometheus_client`` in its documented multiprocess mode: :func:`configure` points
``PROMETHEUS_MULTIPROC_DIR`` at a fresh directory before any collector is built, every
process writes its samples to its own memory-mapped file there, and the single listener in
the main process (started on ``worker_ready``, once logging is configured and the first
children are forked) aggregates all files at scrape time with
``MultiProcessCollector``. Counts from a child that has already exited stay in its file, so a
recycled child's work is not lost from the counters. ``--pool=threads`` and ``--pool=solo``
(the GPU and redaction workers) run everything in the main process and take the same path
with one file.

Consequences an operator should know:

* Because the mode is process-wide, every collector a worker updates (for example
  ``db_query_duration_seconds`` from ``app.core.db_metrics``) is also served on this port.
* The directory gains one small file set per child pid. A worker with a low
  ``--max-tasks-per-child`` accumulates files until it restarts; the directory is wiped at
  every start, so a restarted worker begins from zero like any Prometheus counter.
* By default the files live in a fresh temporary directory that is removed when the worker
  shuts down. Set ``PROMETHEUS_MULTIPROC_DIR`` yourself only to choose where they live (a
  tmpfs, for example); it must belong to one worker, because it is emptied at startup.

Recording must never break or slow a task: every handler is a few dictionary operations, and
any error is logged at DEBUG and swallowed.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import MutableMapping
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)

WORKER_METRICS_PORT_ENV = "WORKER_METRICS_PORT"
MULTIPROC_DIR_ENV = "PROMETHEUS_MULTIPROC_DIR"

OUTCOMES = ("success", "failure", "retry", "revoked")

#: The single series every unregistered task name is folded into.
OTHER_TASK = "other"

#: 0.5 s to 2 h. Short tasks (notifications, waveform, indexing) land in the low buckets;
#: transcription and diarization of long recordings need the 30 min - 2 h tail, or p95 sits
#: in +Inf and says nothing.
RUNTIME_BUCKETS = (
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1200.0,
    1800.0,
    3600.0,
    7200.0,
)

#: Start times kept for runs whose ``task_postrun`` has not arrived. A run whose postrun
#: never fires (its thread was abandoned) would otherwise be held forever on a threads pool.
MAX_TRACKED_RUNS = 10_000


def metrics_port(environ: MutableMapping[str, str] | None = None) -> int | None:
    """The configured listener port, or None when unset, empty or not a valid TCP port."""
    env = os.environ if environ is None else environ
    # A literal name, so the .env.example coverage scanner sees the read.
    raw = (env.get("WORKER_METRICS_PORT") or "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; worker metrics stay off", WORKER_METRICS_PORT_ENV, raw
        )
        return None
    if not 0 < port < 65536:
        logger.warning(
            "%s=%d is not a TCP port; worker metrics stay off", WORKER_METRICS_PORT_ENV, port
        )
        return None
    return port


def is_worker_command(argv: Sequence[str]) -> bool:
    """Whether ``argv`` starts a Celery worker (``celery ... worker`` / ``python -m celery``)."""
    return "worker" in list(argv)[1:]


class WorkerTaskMetrics:
    """The two collectors, on a registry of their own (never the API's default registry)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        from prometheus_client import CollectorRegistry
        from prometheus_client import Counter
        from prometheus_client import Histogram

        self.registry = CollectorRegistry(auto_describe=True)
        self.tasks_total = Counter(
            "celery_task_total",
            "Celery tasks that ended in this worker, by registered task name and outcome "
            "(success, failure, retry, revoked). Unregistered names are counted as 'other'.",
            ["task", "outcome"],
            registry=self.registry,
        )
        self.runtime_seconds = Histogram(
            "celery_task_runtime_seconds",
            "Wall-clock run time of a Celery task in this worker, prerun to postrun, "
            "whatever the outcome.",
            ["task"],
            buckets=RUNTIME_BUCKETS,
            registry=self.registry,
        )
        self._clock = clock
        self._started: dict[str, float] = {}
        self._lock = threading.Lock()

    def record_outcome(self, task: Any, outcome: str) -> None:
        if outcome not in OUTCOMES:
            return
        self.tasks_total.labels(task_label(task), outcome).inc()

    def task_started(self, task_id: str) -> None:
        with self._lock:
            if len(self._started) >= MAX_TRACKED_RUNS:
                self._started.clear()
            self._started[task_id] = self._clock()

    def task_finished(self, task: Any, task_id: str) -> None:
        with self._lock:
            started = self._started.pop(task_id, None)
        if started is None:
            return
        self.runtime_seconds.labels(task_label(task)).observe(max(0.0, self._clock() - started))


def task_label(task: Any) -> str:
    """The task's registered name, or :data:`OTHER_TASK`. Bounded by the registry."""
    name = getattr(task, "name", None)
    app = getattr(task, "app", None)
    if not name or app is None:
        return OTHER_TASK
    try:
        registered = name in app.tasks
    except Exception:  # noqa: BLE001 - a label lookup must never fail a task
        return OTHER_TASK
    return str(name) if registered else OTHER_TASK


_metrics: WorkerTaskMetrics | None = None
_multiproc_dir: str | None = None
_owns_multiproc_dir = False
_listener_started = False


def configure(
    argv: Sequence[str] | None = None, environ: MutableMapping[str, str] | None = None
) -> bool:
    """Turn recording on for this process when it is a worker and the port is set.

    Called first thing when ``app.core.celery`` is imported, in the worker's main process and
    before the pool forks, so the multiprocess setting is inherited by every child.

    Returns:
        True when worker metrics are now enabled in this process.
    """
    global _metrics, _multiproc_dir, _owns_multiproc_dir
    env = os.environ if environ is None else environ
    if metrics_port(env) is None or not is_worker_command(sys.argv if argv is None else argv):
        return False
    try:
        _owns_multiproc_dir = not (env.get(MULTIPROC_DIR_ENV) or "").strip()
        path = _prepare_multiproc_dir(env)
        env[MULTIPROC_DIR_ENV] = path
        os.environ[MULTIPROC_DIR_ENV] = path
        # prometheus_client picks its value class when it is first imported. If anything
        # imported it before this point, re-pick so collectors built from now on are
        # file-backed; ones already built stay process-local and are simply not served.
        from prometheus_client import values

        values.ValueClass = values.get_value_class()
        _multiproc_dir = path
        _metrics = WorkerTaskMetrics()
    except Exception as exc:  # noqa: BLE001 - metrics must never stop a worker starting
        logger.warning("Worker metrics could not be enabled: %s", exc)
        _metrics = None
        return False
    return True


def _prepare_multiproc_dir(env: MutableMapping[str, str]) -> str:
    """The directory for this worker's sample files, emptied of a previous run's."""
    path = (env.get(MULTIPROC_DIR_ENV) or "").strip()
    if not path:
        return tempfile.mkdtemp(prefix="celery-worker-metrics-")
    os.makedirs(path, exist_ok=True)
    for entry in os.listdir(path):
        if entry.endswith(".db"):
            os.unlink(os.path.join(path, entry))
    return path


def _start_http_server(port: int, registry: Any) -> Any:
    from prometheus_client import start_http_server

    return start_http_server(port, registry=registry)


def start_listener(**_: Any) -> Any:
    """``worker_ready`` (main process): serve every process's samples on the port.

    Returns the server handle, or None when disabled or the port could not be bound -- a
    worker without its metrics endpoint still runs every task.
    """
    global _listener_started
    if _metrics is None or _listener_started:
        return None
    port = metrics_port()
    if port is None:
        return None
    try:
        if _multiproc_dir:
            from prometheus_client import CollectorRegistry
            from prometheus_client import multiprocess

            registry: Any = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry, path=_multiproc_dir)
        else:
            registry = _metrics.registry
        server = _start_http_server(port, registry)
    except Exception as exc:  # noqa: BLE001 - e.g. port in use; the worker must still run
        logger.warning("Worker metrics listener could not start on port %s: %s", port, exc)
        return None
    _listener_started = True
    logger.info("Worker metrics listening on port %d (%s)", port, _multiproc_dir or "in-process")
    return server


def mark_child_dead(pid: int | None = None, **_: Any) -> None:
    """``worker_process_shutdown``: retire an exiting child's live-gauge files.

    Counter and histogram files are kept on purpose: they hold the child's completed work.
    """
    if _metrics is None or not _multiproc_dir:
        return
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid or os.getpid(), _multiproc_dir)
    except Exception as exc:  # noqa: BLE001 - see module docstring
        logger.debug("Could not mark worker child %s dead for metrics: %s", pid, exc)


def remove_owned_dir(**_: Any) -> None:
    """``worker_shutdown`` (main process, pool already joined): drop the temporary directory.

    Only a directory :func:`configure` created itself; an operator-chosen one is left alone.
    """
    if not (_owns_multiproc_dir and _multiproc_dir):
        return
    shutil.rmtree(_multiproc_dir, ignore_errors=True)


def _record(outcome: str, sender: Any) -> None:
    metrics = _metrics
    if metrics is None:
        return
    try:
        metrics.record_outcome(sender, outcome)
    except Exception as exc:  # noqa: BLE001 - see module docstring
        logger.debug(
            "Could not record a %s for %s: %s", outcome, getattr(sender, "name", None), exc
        )


def on_task_success(sender: Any = None, **_: Any) -> None:
    _record("success", sender)


def on_task_failure(sender: Any = None, **_: Any) -> None:
    _record("failure", sender)


def on_task_retry(sender: Any = None, **_: Any) -> None:
    _record("retry", sender)


def on_task_revoked(sender: Any = None, **_: Any) -> None:
    _record("revoked", sender)


def on_task_prerun(sender: Any = None, task_id: str | None = None, **_: Any) -> None:
    metrics = _metrics
    if metrics is None or task_id is None:
        return
    try:
        metrics.task_started(task_id)
    except Exception as exc:  # noqa: BLE001 - see module docstring
        logger.debug("Could not record the start of %s: %s", task_id, exc)


def on_task_postrun(
    sender: Any = None, task_id: str | None = None, task: Any = None, **_: Any
) -> None:
    metrics = _metrics
    if metrics is None or task_id is None:
        return
    try:
        metrics.task_finished(task if task is not None else sender, task_id)
    except Exception as exc:  # noqa: BLE001 - see module docstring
        logger.debug("Could not record the run time of %s: %s", task_id, exc)


def connect_signals() -> None:
    """Wire the handlers to Celery's signals. Idempotent (fixed ``dispatch_uid``s)."""
    from celery import signals

    wiring = (
        (signals.task_prerun, on_task_prerun),
        (signals.task_postrun, on_task_postrun),
        (signals.task_success, on_task_success),
        (signals.task_failure, on_task_failure),
        (signals.task_retry, on_task_retry),
        (signals.task_revoked, on_task_revoked),
        (signals.worker_ready, start_listener),
        (signals.worker_process_shutdown, mark_child_dead),
        (signals.worker_shutdown, remove_owned_dir),
    )
    for signal, handler in wiring:
        signal.connect(handler, weak=False, dispatch_uid=f"worker_metrics.{handler.__name__}")
