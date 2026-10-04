"""Structured per-stage timing for the processing pipeline (#1134).

Every pipeline stage runs inside :func:`stage`, which records exactly one observation per
run, whether the stage returns or raises:

* one log line, greppable by the ``TIMING:`` marker the free-text timing lines already use::

      TIMING: stage=asr outcome=success seconds=12.345 task_id=<id> file_id=<id>

  ``task_id`` / ``file_id`` are ``-`` when the stage cannot see them. They appear in the log
  only, never in a metric label.

* two Prometheus collectors, served on a worker's ``WORKER_METRICS_PORT`` alongside the
  per-task metrics (``app/core/worker_metrics.py``, multiprocess mode):

  - ``pipeline_stage_duration_seconds{stage}`` — histogram of wall time;
  - ``pipeline_stage_total{stage, outcome}`` — counter, ``outcome`` is ``success`` or
    ``failure``.

  ``stage`` is always one of :data:`STAGES`; any other name is recorded as ``other``. So the
  label set is fixed by this file: at most ``len(STAGES) + 1`` stage values and two outcomes.

The stages, in pipeline order (see ``docs-site/docs/operations/monitoring.md``):

=====================  ===============================================================
``preprocess``         CPU task: fetch the media, extract 16 kHz audio, stage it
``vad``                voice-activity detection and feature extraction, up to the first
                       decoded batch (faster-whisper's batched pipeline does both
                       before decoding)
``asr``                decoding (local Whisper), or the cloud ASR provider call
``diarization``        speaker diarization; with diarization overlapped with ASR, the
                       time the pipeline still waited for it
``speaker_assignment`` assigning diarized speakers to transcript words
``finalize``           resegment/merge and writing segments and speakers to the database
``speaker_embedding``  speaker embedding extraction and profile matching
``postprocess``        CPU task: completion, speaker matching, downstream dispatch
``search_indexing``    chunk-level search indexing task
=====================  ===============================================================

There is no separate alignment stage: word timestamps come out of the decoder itself.

Recording never breaks or slows a stage: a failure to log or observe is logged at DEBUG and
swallowed, and the collectors are built on first use (after a worker has switched
``prometheus_client`` to multiprocess mode), costing a few dictionary operations per stage.
With worker metrics off the observations go to a private registry nothing serves.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

STAGES: tuple[str, ...] = (
    "preprocess",
    "vad",
    "asr",
    "diarization",
    "speaker_assignment",
    "finalize",
    "speaker_embedding",
    "postprocess",
    "search_indexing",
)
OUTCOMES: tuple[str, ...] = ("success", "failure")
OTHER_STAGE = "other"

#: 50 ms to 2 h: VAD and speaker assignment take well under a second on short files, while
#: ASR and diarization of multi-hour recordings need the long tail.
DURATION_BUCKETS = (
    0.05,
    0.1,
    0.25,
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

_ids: contextvars.ContextVar[tuple[str | None, str | None]] = contextvars.ContextVar(
    "stage_timing_ids", default=(None, None)
)


class _Collectors:
    def __init__(self) -> None:
        from prometheus_client import CollectorRegistry
        from prometheus_client import Counter
        from prometheus_client import Histogram

        self.registry = CollectorRegistry(auto_describe=True)
        self.duration = Histogram(
            "pipeline_stage_duration_seconds",
            "Wall time of one run of a processing-pipeline stage, whatever the outcome.",
            ["stage"],
            buckets=DURATION_BUCKETS,
            registry=self.registry,
        )
        self.total = Counter(
            "pipeline_stage_total",
            "Processing-pipeline stage runs, by stage and outcome (success, failure).",
            ["stage", "outcome"],
            registry=self.registry,
        )


_collectors: _Collectors | None = None
_collectors_lock = threading.Lock()


def _get_collectors() -> _Collectors:
    global _collectors
    if _collectors is None:
        with _collectors_lock:
            if _collectors is None:
                _collectors = _Collectors()
    return _collectors


def stage_label(name: str) -> str:
    """``name`` when it is a known stage, else :data:`OTHER_STAGE`."""
    return name if name in STAGES else OTHER_STAGE


@contextmanager
def bind(task_id: Any = None, file_id: Any = None) -> Iterator[None]:
    """Attach ids to every stage recorded inside the block (log line only, never a label)."""
    token = _ids.set((str(task_id) if task_id else None, str(file_id) if file_id else None))
    try:
        yield
    finally:
        _ids.reset(token)


def record(
    name: str,
    seconds: float,
    outcome: str,
    *,
    task_id: Any = None,
    file_id: Any = None,
) -> None:
    """Log and observe one finished stage run. Never raises."""
    label = stage_label(name)
    outcome = outcome if outcome in OUTCOMES else "failure"
    bound_task, bound_file = _ids.get()
    try:
        logger.info(
            "TIMING: stage=%s outcome=%s seconds=%.3f task_id=%s file_id=%s",
            label,
            outcome,
            seconds,
            task_id or bound_task or "-",
            file_id or bound_file or "-",
        )
    except Exception as exc:  # noqa: BLE001 - timing must never break a stage
        logger.debug("Could not log stage timing for %s: %s", label, exc)
    try:
        collectors = _get_collectors()
        collectors.duration.labels(label).observe(max(0.0, seconds))
        collectors.total.labels(label, outcome).inc()
    except Exception as exc:  # noqa: BLE001 - timing must never break a stage
        logger.debug("Could not observe stage timing for %s: %s", label, exc)


class StageRun:
    """Handle yielded by :func:`stage`, for a stage that reports failure without raising."""

    __slots__ = ("failed",)

    def __init__(self) -> None:
        self.failed = False

    def fail(self) -> None:
        """Record this run as ``failure`` even though the block completes."""
        self.failed = True


@contextmanager
def stage(name: str, *, task_id: Any = None, file_id: Any = None) -> Iterator[StageRun]:
    """Time the block as one run of pipeline stage ``name``.

    Exactly one :func:`record` per entry: ``success`` when the block completes, ``failure``
    when it raises (the exception propagates unchanged) or calls ``run.fail()``. Usable as a
    decorator too, recording one run per call. ``task_id`` / ``file_id`` also bind for
    nested stages.
    """
    start = time.perf_counter()
    run = StageRun()
    raised = True
    bound_task, bound_file = _ids.get()
    with bind(task_id or bound_task, file_id or bound_file):
        try:
            yield run
            raised = False
        finally:
            outcome = "failure" if raised or run.failed else "success"
            record(name, time.perf_counter() - start, outcome, task_id=task_id, file_id=file_id)
