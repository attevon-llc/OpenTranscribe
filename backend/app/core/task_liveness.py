"""Queue-aware liveness for transcription runs (issue #1020).

Recovery used to decide that a transcription was dead from two proxies, both wrong for a run
that is still waiting in the broker:

* **The checking process's start time.** A task whose ``updated_at`` predated the module load
  of whichever worker ran the health check was declared dead. Restarting the utility worker
  therefore "killed" every transcription still queued for a GPU worker, and recovery
  dispatched a second pipeline while the first message was still waiting — so both ran.
* **Age since creation.** ``MAX_TASK_DURATIONS`` was measured from ``Task.created_at``, which
  for a transcription is the moment it was *dispatched*. An hour in the queue used up the
  whole budget of a run that had not started.

This module replaces both with two Redis markers per run, keyed by the application task id
(``MediaFile.active_task_id``):

``transcription_queued:<task_id>``
    Set when the pipeline is published and again whenever a stage hands the run back to the
    broker (its successor link, a retry, or a requeue). While it exists the run is waiting,
    and waiting is not failure. It lives in the same Redis as the broker, so if the broker
    loses its messages the marker goes with them and the run correctly reads as lost. Its TTL
    (``TRANSCRIPTION_QUEUE_MAX_WAIT_SECONDS``) is the operator's ceiling on queue wait.

``transcription_heartbeat:<task_id>``
    Refreshed by a daemon thread for as long as a transcription stage is executing. Its value
    is the epoch the stage started, which is what the duration budget is measured from. A
    worker that dies stops refreshing it and the run reads as dead within one TTL.

Every read **fails safe**: if Redis cannot be reached the state is ``UNKNOWN`` and callers must
not fail or re-dispatch the run. Redis is the broker, so an unreachable Redis says nothing about
whether a queued message still exists — and recovering on that guess is exactly the
duplicate-run defect this module exists to remove.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from enum import StrEnum

from app.core.task_config import task_recovery_config

logger = logging.getLogger(__name__)

QUEUED_KEY = "transcription_queued:{task_id}"
HEARTBEAT_KEY = "transcription_heartbeat:{task_id}"

#: The one task type whose Task row is created at DISPATCH rather than when it starts
#: running, and therefore the one whose liveness these markers describe.
TRANSCRIPTION_TASK_TYPE = "transcription"


class RunState(StrEnum):
    """What recovery may conclude about one transcription run."""

    QUEUED = "queued"  # a message for this run is waiting in the broker
    RUNNING = "running"  # a worker is executing a stage and heartbeating
    DEAD = "dead"  # neither: the worker died or the message was lost
    UNKNOWN = "unknown"  # Redis could not be read; conclude nothing


@dataclass(frozen=True)
class RunLiveness:
    """One run's state, plus when its current stage started (RUNNING only)."""

    state: RunState
    started_at: datetime | None = None


def mark_queued(task_id: str) -> None:
    """Record that a message for this run is (again) waiting in the broker.

    Best-effort: a failure is logged and swallowed. A missing marker makes a waiting run
    look lost, which recovery then answers by re-dispatching and superseding the original —
    one wasted queue slot, never two transcriptions.
    """
    from app.core.redis import get_redis

    try:
        get_redis().setex(
            QUEUED_KEY.format(task_id=task_id),
            task_recovery_config.TRANSCRIPTION_QUEUE_MAX_WAIT,
            "1",
        )
    except Exception as e:
        logger.warning("Could not record transcription %s as queued: %s", task_id, e)


def clear_run_markers(task_id: str) -> None:
    """Drop both markers for a run that recovery has retired."""
    from app.core.redis import get_redis

    try:
        get_redis().delete(
            QUEUED_KEY.format(task_id=task_id), HEARTBEAT_KEY.format(task_id=task_id)
        )
    except Exception as e:
        logger.warning("Could not clear the liveness markers of transcription %s: %s", task_id, e)


def _beat(task_id: str, started_epoch: float, *, first: bool) -> None:
    from app.core.redis import get_redis

    try:
        pipe = get_redis().pipeline()
        if first:
            pipe.delete(QUEUED_KEY.format(task_id=task_id))
        pipe.setex(
            HEARTBEAT_KEY.format(task_id=task_id),
            task_recovery_config.TRANSCRIPTION_HEARTBEAT_TTL,
            repr(started_epoch),
        )
        pipe.execute()
    except Exception as e:
        logger.warning("Transcription %s heartbeat failed: %s", task_id, e)


def _hand_back(task_id: str) -> None:
    """The stage ended: the run is back in the broker's hands until its next stage starts.

    Done unconditionally rather than only on success. A retry, a ``Reject(requeue=True)`` and
    the chain's successor link all put a message back on a queue; a genuine failure marks the
    Task row terminal, after which recovery never consults these markers, and the marker then
    expires on its own.
    """
    from app.core.redis import get_redis

    try:
        pipe = get_redis().pipeline()
        pipe.delete(HEARTBEAT_KEY.format(task_id=task_id))
        pipe.setex(
            QUEUED_KEY.format(task_id=task_id),
            task_recovery_config.TRANSCRIPTION_QUEUE_MAX_WAIT,
            "1",
        )
        pipe.execute()
    except Exception as e:
        logger.warning("Could not hand transcription %s back to the queue: %s", task_id, e)


@contextmanager
def run_heartbeat(task_id: str) -> Iterator[None]:
    """Heartbeat this run for as long as the enclosed stage executes.

    A daemon thread rather than calls sprinkled through the stage: the GPU decode can sit in
    one native call for minutes, and a heartbeat that depends on the stage's own progress
    callbacks would lapse exactly when the stage is busiest.
    """
    started_epoch = time.time()
    interval = task_recovery_config.TRANSCRIPTION_HEARTBEAT_INTERVAL
    stop = threading.Event()

    _beat(task_id, started_epoch, first=True)

    def _loop() -> None:
        while not stop.wait(interval):
            _beat(task_id, started_epoch, first=False)

    thread = threading.Thread(target=_loop, name=f"heartbeat-{task_id[:8]}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)
        _hand_back(task_id)


def _parse_started(raw: bytes | str | None) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC) if raw is not None else None
    except (TypeError, ValueError):
        return None


def probe_runs(task_ids: Iterable[str]) -> dict[str, RunLiveness]:
    """Classify each run with a single Redis round trip.

    Returns ``UNKNOWN`` for every run when Redis cannot be read (see the module docstring).
    """
    ids = [str(t) for t in task_ids]
    if not ids:
        return {}
    from app.core.redis import get_redis

    keys: list[str] = []
    for task_id in ids:
        keys.append(HEARTBEAT_KEY.format(task_id=task_id))
        keys.append(QUEUED_KEY.format(task_id=task_id))
    try:
        values = get_redis().mget(keys)
    except Exception as e:
        logger.warning(
            "Could not read transcription liveness (treating %d as unknown): %s", len(ids), e
        )
        return {task_id: RunLiveness(RunState.UNKNOWN) for task_id in ids}

    result: dict[str, RunLiveness] = {}
    for index, task_id in enumerate(ids):
        heartbeat, queued = values[2 * index], values[2 * index + 1]
        if heartbeat is not None:
            result[task_id] = RunLiveness(RunState.RUNNING, _parse_started(heartbeat))
        elif queued is not None:
            result[task_id] = RunLiveness(RunState.QUEUED)
        else:
            result[task_id] = RunLiveness(RunState.DEAD)
    return result


def supersede_run(task_id: str, file_uuid: str = "") -> None:
    """Retire a run that recovery has failed or replaced.

    Arms the cooperative cancel so a stage still executing stops at its next checkpoint, and
    drops the markers so the run is not reported as queued or running. A message for it that
    is still waiting is stopped at pickup by the stage's ownership check, which reads the now
    terminal Task row.
    """
    from app.core.task_cancellation import request_cancel

    request_cancel(task_id, file_uuid)
    clear_run_markers(task_id)
