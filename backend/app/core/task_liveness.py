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
    The run's LEASE. Refreshed by a daemon thread for as long as a transcription stage is
    executing (every ``TRANSCRIPTION_HEARTBEAT_INTERVAL_SECONDS``, valid for
    ``TRANSCRIPTION_HEARTBEAT_TTL_SECONDS``). Its value records when the stage started, which
    is what the duration budget is measured from, and which broker message holds it (the
    Celery task id of the executing stage), which is how a second delivery of that same
    message recognises that the first is still running. A worker that dies stops refreshing
    it and the run reads as dead within one TTL. The broker-side reaper
    (``app/core/broker_orphans.py``) acts on exactly this signal.

Every read **fails safe**: if Redis cannot be reached the state is ``UNKNOWN`` and callers must
not fail or re-dispatch the run. Redis is the broker, so an unreachable Redis says nothing about
whether a queued message still exists — and recovering on that guess is exactly the
duplicate-run defect this module exists to remove.
"""

from __future__ import annotations

import json
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
    """One run's state, plus when its current stage started and who holds it (RUNNING only)."""

    state: RunState
    started_at: datetime | None = None
    owner: str | None = None  # the Celery task id of the stage message holding the lease


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


def _lease_value(started_epoch: float, owner: str | None) -> str:
    return json.dumps({"started": started_epoch, "owner": owner})


def _current_stage_id() -> str | None:
    """The Celery task id of the stage executing on this thread, if any."""
    try:
        from celery import current_task

        request = getattr(current_task, "request", None)
        task_id = getattr(request, "id", None) if request is not None else None
        return str(task_id) if task_id else None
    except Exception:  # noqa: BLE001 - outside a worker there is simply no owner
        return None


def _beat(task_id: str, started_epoch: float, *, first: bool, owner: str | None = None) -> None:
    from app.core.redis import get_redis

    try:
        pipe = get_redis().pipeline()
        if first:
            pipe.delete(QUEUED_KEY.format(task_id=task_id))
        pipe.setex(
            HEARTBEAT_KEY.format(task_id=task_id),
            task_recovery_config.TRANSCRIPTION_HEARTBEAT_TTL,
            _lease_value(started_epoch, owner),
        )
        pipe.execute()
    except Exception as e:
        logger.warning("Transcription %s heartbeat failed: %s", task_id, e)


def _hands_back_to_broker(exc: BaseException | None) -> bool:
    """Whether a stage that ended this way left a message for the run on a queue.

    A normal return publishes the chain's successor link; a Celery retry or a
    ``Reject(requeue=True)`` puts this stage's own message back. Anything else -- a failure the
    retry policy has already answered (its Task row is terminal), or an exception from being
    torn down (a cold shutdown's cancel, which on the Redis transport ACKS the message) --
    leaves nothing waiting, and claiming otherwise would hide a lost run as "queued" until
    ``TRANSCRIPTION_QUEUE_MAX_WAIT_SECONDS``.
    """
    if exc is None:
        return True
    from celery.exceptions import Reject
    from celery.exceptions import Retry

    if isinstance(exc, Retry):
        return True
    return isinstance(exc, Reject) and bool(getattr(exc, "requeue", False))


def _release(task_id: str) -> None:
    """The stage ended and left nothing queued: drop its lease so the run reads as it is."""
    from app.core.redis import get_redis

    try:
        get_redis().delete(HEARTBEAT_KEY.format(task_id=task_id))
    except Exception as e:
        logger.warning("Could not release the lease of transcription %s: %s", task_id, e)


def _hand_back(task_id: str) -> None:
    """The stage handed the run back to the broker until its next stage starts.

    Only called when :func:`_hands_back_to_broker` says a message is waiting.
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
    owner = _current_stage_id()
    stop = threading.Event()

    _beat(task_id, started_epoch, first=True, owner=owner)

    def _loop() -> None:
        while not stop.wait(interval):
            _beat(task_id, started_epoch, first=False, owner=owner)

    thread = threading.Thread(target=_loop, name=f"heartbeat-{task_id[:8]}", daemon=True)
    thread.start()
    ended_by: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        ended_by = exc
        raise
    finally:
        stop.set()
        thread.join(timeout=5)
        if _hands_back_to_broker(ended_by):
            _hand_back(task_id)
        else:
            _release(task_id)


def _parse_lease(raw: bytes | str | None) -> tuple[datetime | None, str | None]:
    """``(started_at, owner)`` from a lease value.

    Also reads the bare-epoch value written before the owner was recorded, so a run that
    straddles an upgrade is still judged by when it started.
    """
    if raw is None:
        return None, None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    started: object = text
    owner: str | None = None
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict):
        started = decoded.get("started")
        owner = decoded.get("owner") or None
    try:
        return datetime.fromtimestamp(float(started), tz=UTC), owner  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, owner


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
            started_at, owner = _parse_lease(heartbeat)
            result[task_id] = RunLiveness(RunState.RUNNING, started_at, owner)
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


#: Per-file count of infrastructure requeues (a dead worker's stage put back on its queue, or
#: a dead run re-dispatched). A sorted set rather than one key per file so the "files requeued
#: more than N times" gauge is a single ZCOUNT. Kept apart from ``MediaFile.retry_count`` on
#: purpose: a deploy or a node loss must not spend the budget that exists for the file's own
#: failures, but a file that kills every worker it lands on must still stop eventually.
INFRA_REQUEUES_KEY = "transcription_infra_requeues"


def record_infra_requeue(file_uuid: str) -> int | None:
    """Count one more infrastructure requeue of ``file_uuid``; None when Redis is unreadable."""
    from app.core.redis import get_redis

    try:
        return int(get_redis().zincrby(INFRA_REQUEUES_KEY, 1, file_uuid))
    except Exception as e:
        logger.warning("Could not count an infrastructure requeue of %s: %s", file_uuid, e)
        return None


def infra_requeues(file_uuid: str) -> int:
    """How many infrastructure requeues ``file_uuid`` has had (0 when unknown)."""
    from app.core.redis import get_redis

    try:
        score = get_redis().zscore(INFRA_REQUEUES_KEY, file_uuid)
    except Exception as e:
        logger.warning("Could not read the infrastructure requeues of %s: %s", file_uuid, e)
        return 0
    return int(score or 0)


def clear_infra_requeues(file_uuid: str) -> None:
    """Forget a file's infrastructure requeues once its run reached a terminal state."""
    from app.core.redis import get_redis

    try:
        get_redis().zrem(INFRA_REQUEUES_KEY, file_uuid)
    except Exception as e:
        logger.warning("Could not clear the infrastructure requeues of %s: %s", file_uuid, e)


def count_files_requeued_at_least(threshold: int) -> int | None:
    """Files with at least ``threshold`` infrastructure requeues; None when Redis is unreadable."""
    from app.core.redis import get_redis

    try:
        return int(get_redis().zcount(INFRA_REQUEUES_KEY, threshold, "+inf"))
    except Exception as e:
        logger.warning("Could not count requeued files: %s", e)
        return None
