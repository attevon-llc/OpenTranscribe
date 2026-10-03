"""Replay idempotent tasks whose worker died mid-run (issue #1067).

Celery alone can't do this for a task whose worker is killed with SIGKILL, the OOM killer
or a node loss:

* A task with the default early ack is acked when the worker *receives* it, so the message
  is gone the moment the process dies.
* ``acks_late=True, reject_on_worker_lost=True`` only requeues when the prefork *parent*
  survives to see its child die. A container-level OOM kill, a SIGKILL or a node loss takes
  the parent too, and the un-acked message waits in the Redis transport's ``unacked`` hash
  for ``visibility_timeout``: six hours, because that has to outlast the longest
  transcription.

So an allowlisted task records how to re-send itself when it starts, and heartbeats while it
runs:

``task_replay`` (hash, field = task id)
    ``{name, args, kwargs, queue, priority, attempt, recorded_at}``, written by
    ``task_prerun`` and removed by ``task_postrun``. A record whose heartbeat has lapsed
    belongs to a run that never finished.

``task_heartbeat:<task_id>``
    Refreshed every ``TASK_HEARTBEAT_INTERVAL_SECONDS`` by one daemon thread per worker
    process, for every allowlisted task that process is executing.

``reclaim_lost_tasks`` (the ``system.reclaim_lost_tasks`` beat task) re-sends each dead
run **under the same task id**. Its Task row carries on in place, and a dedupe guard
keyed by that id recognises the replay as its own. After ``TASK_REPLAY_MAX_ATTEMPTS``
replays it gives up and fails the row. That stops a poison message (a task that OOMs
every time) from looping forever.

Only tasks that are safe to run twice belong in ``REPLAYABLE_TASKS``: a slow worker whose
heartbeat lapses gets a duplicate run, and so can a task whose ``acks_late`` message is also
restored when the visibility timeout passes. Transcription is **never** replayable: its
duplicate costs a GPU run, and it has its own queue-aware liveness (``task_liveness.py``).

Every Redis failure fails safe. The worker side logs and carries on, and the sweep does
nothing. An unreadable heartbeat says nothing about whether the run is alive.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from typing import Any

from app.core.task_config import task_recovery_config

logger = logging.getLogger(__name__)

REPLAY_HASH = "task_replay"
HEARTBEAT_KEY = "task_heartbeat:{task_id}"
CLAIM_KEY = "task_replay_claim:{task_id}"

#: Tasks that are safe to run again after their worker died. Each is keyed by a file (or
#: a file plus fixed arguments) and overwrites its own output, so a second run reaches the
#: same end state. The cost of a duplicate is compute (or one LLM call), never data.
#: Every task the per-file pipeline dispatches is either here or excluded with a reason in
#: ``tests/unit/test_lost_task_replay.py`` (``_NOT_REPLAYED_ON_PURPOSE``).
REPLAYABLE_TASKS: frozenset[str] = frozenset(
    {
        "detect_speaker_attributes",
        "speaker.cluster_for_file",
        "analytics.analyze_transcript",
        # The pipeline's per-file waveform (dispatched by preprocess) and the bulk backfill.
        "media.generate_waveform",
        "media.generate_waveform_data",
        # Redaction spans, recomputed per segment; a lost run left the file's redaction status
        # in progress, so the LLM tasks that wait for it deferred until they gave up.
        "redaction.detect",
        "media.create_playback_rendition",
        "generate_thumbnail",
        "index_transcript_search",
        "artifacts.generate_file_facts",
        "ai.generate_summary",
        "ai.extract_topics",
        "ai.identify_speakers",
    }
)

#: Never replayed even if someone adds them above: a duplicate transcription is a second GPU
#: run, and the transcription pipeline already has its own recovery.
_NEVER_REPLAY_PREFIXES = ("transcription.",)


def is_replayable(task_name: str | None) -> bool:
    return (
        bool(task_name)
        and task_name in REPLAYABLE_TASKS
        and not str(task_name).startswith(_NEVER_REPLAY_PREFIXES)
    )


def _redis():
    from app.core.redis import get_redis

    return get_redis()


def _decode(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw


# --- worker side ---------------------------------------------------------------------------

_running: set[str] = set()
_running_lock = threading.Lock()
_beat_thread: threading.Thread | None = None
_beat_thread_pid: int | None = None


def _beat_all() -> None:
    with _running_lock:
        ids = list(_running)
    if not ids:
        return
    ttl = task_recovery_config.TASK_HEARTBEAT_TTL
    try:
        pipe = _redis().pipeline()
        for task_id in ids:
            pipe.setex(HEARTBEAT_KEY.format(task_id=task_id), ttl, "1")
        pipe.execute()
    except Exception as e:  # noqa: BLE001 — a missed beat must never fail the task itself
        logger.warning("Task heartbeat failed for %d task(s): %s", len(ids), e)


def _beat_loop() -> None:
    while True:
        time.sleep(task_recovery_config.TASK_HEARTBEAT_INTERVAL)
        _beat_all()


def _ensure_beat_thread() -> None:
    """Start this process's heartbeat thread, once per process.

    Checked against the pid because threads don't survive ``fork``: a prefork child
    inherits the parent's module state but not its running thread.
    """
    global _beat_thread, _beat_thread_pid
    pid = os.getpid()
    if _beat_thread is not None and _beat_thread_pid == pid and _beat_thread.is_alive():
        return
    _beat_thread = threading.Thread(target=_beat_loop, name="task-heartbeat", daemon=True)
    _beat_thread_pid = pid
    _beat_thread.start()


def _delivery_route(task: Any) -> tuple[str | None, int | None]:
    delivery = getattr(task.request, "delivery_info", None) or {}
    queue = delivery.get("routing_key") or None
    priority = delivery.get("priority")
    if priority is None:
        priority = getattr(task, "priority", None)
    return queue, priority


def on_task_start(task: Any, task_id: str, args: Iterable | None, kwargs: dict | None) -> None:
    """``task_prerun``: record how to re-send this run, and start heartbeating it."""
    if not is_replayable(getattr(task, "name", None)) or getattr(task.request, "is_eager", False):
        return
    queue, priority = _delivery_route(task)
    try:
        client = _redis()
        existing = _decode(client.hget(REPLAY_HASH, task_id))
        # A replay (or a Celery retry) of the same id keeps the attempt count the sweep gave
        # it, which is what bounds a poison message.
        attempt = json.loads(existing).get("attempt", 0) if existing else 0
        record = {
            "name": task.name,
            "args": list(args or ()),
            "kwargs": dict(kwargs or {}),
            "queue": queue,
            "priority": priority,
            "attempt": attempt,
            "recorded_at": time.time(),
        }
        pipe = client.pipeline()
        pipe.hset(REPLAY_HASH, task_id, json.dumps(record))
        pipe.setex(
            HEARTBEAT_KEY.format(task_id=task_id), task_recovery_config.TASK_HEARTBEAT_TTL, "1"
        )
        pipe.execute()
    except Exception as e:  # noqa: BLE001 — replay is a safety net; the task must still run
        logger.warning("Could not record %s[%s] for replay: %s", task.name, task_id, e)
        return
    with _running_lock:
        _running.add(task_id)
    _ensure_beat_thread()


def on_task_end(task: Any, task_id: str) -> None:
    """``task_postrun``: the run ended (any outcome), so it no longer needs replaying.

    A failure is the task's own verdict, and a Celery retry has already published its next
    message under the same id, so neither needs this record.
    """
    if not is_replayable(getattr(task, "name", None)):
        return
    with _running_lock:
        was_running = task_id in _running
        _running.discard(task_id)
    if not was_running:
        return
    try:
        pipe = _redis().pipeline()
        pipe.hdel(REPLAY_HASH, task_id)
        pipe.delete(HEARTBEAT_KEY.format(task_id=task_id))
        pipe.execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not clear the replay record of %s[%s]: %s", task.name, task_id, e)


def is_alive(task_id: str) -> bool | None:
    """Whether ``task_id`` is heartbeating right now; None when Redis can't be read."""
    try:
        return bool(_redis().exists(HEARTBEAT_KEY.format(task_id=task_id)))
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read the heartbeat of %s: %s", task_id, e)
        return None


def replay_tracked_ids(task_ids: Iterable[str]) -> set[str]:
    """The subset of ``task_ids`` this module is responsible for right now.

    A task that is heartbeating, or has a replay record waiting for the sweep, is not
    "stuck" in the health check's sense: either it is running, or the sweep will re-send
    it. Empty when Redis can't be read, so callers fall back to their own rules.
    """
    ids = [str(t) for t in task_ids]
    if not ids:
        return set()
    try:
        client = _redis()
        beats = client.mget([HEARTBEAT_KEY.format(task_id=t) for t in ids])
        pipe = client.pipeline()
        for task_id in ids:
            pipe.hexists(REPLAY_HASH, task_id)
        recorded = pipe.execute()
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read replay state for %d task(s): %s", len(ids), e)
        return set()
    return {t for t, beat, rec in zip(ids, beats, recorded, strict=True) if beat or rec}


# --- the sweep -----------------------------------------------------------------------------


def _mark_task_row(task_id: str, status: str, message: str | None) -> None:
    """Move a non-terminal Task row (if the task keeps one under this id) to ``status``."""
    from app.db.session_utils import session_scope
    from app.models.media import Task
    from app.utils.task_utils import update_task_status

    with session_scope() as db:
        row = db.query(Task).filter(Task.id == task_id).first()
        if row is None or row.status not in ("pending", "in_progress"):
            return
        update_task_status(
            db,
            task_id,
            status,
            error_message=message,
            completed=status == "failed",
        )


def _task_row_is_terminal(task_id: str) -> bool:
    from app.db.session_utils import session_scope
    from app.models.media import Task

    with session_scope() as db:
        row = db.query(Task.status).filter(Task.id == task_id).first()
        return row is not None and row[0] not in ("pending", "in_progress")


def reclaim_lost_tasks(send_task=None) -> dict[str, int]:
    """Re-send every recorded run whose heartbeat has lapsed. Returns counts by outcome.

    ``send_task`` defaults to ``celery_app.send_task`` and is injectable for tests.
    """
    summary = {"records": 0, "alive": 0, "replayed": 0, "exhausted": 0, "dropped": 0}
    try:
        client = _redis()
        records = client.hgetall(REPLAY_HASH)
    except Exception as e:  # noqa: BLE001 — conclude nothing from an unreadable Redis
        logger.warning("Lost-task sweep skipped, Redis unreadable: %s", e)
        summary["error"] = 1
        return summary
    if not records:
        return summary

    if send_task is None:
        from app.core.celery import celery_app

        send_task = celery_app.send_task

    decoded = {_decode(k): _decode(v) for k, v in records.items()}
    ids = [t for t in decoded if t]
    summary["records"] = len(ids)
    try:
        beats = client.mget([HEARTBEAT_KEY.format(task_id=t) for t in ids])
    except Exception as e:  # noqa: BLE001
        logger.warning("Lost-task sweep skipped, heartbeats unreadable: %s", e)
        summary["error"] = 1
        return summary

    config = task_recovery_config
    now = time.time()
    for task_id, beat in zip(ids, beats, strict=True):
        if beat is not None:
            summary["alive"] += 1
            continue
        try:
            record = json.loads(decoded[task_id] or "{}")
        except ValueError:
            record = {}
        recorded_at = float(record.get("recorded_at") or 0)
        if now - recorded_at < config.TASK_HEARTBEAT_TTL:
            # Recorded and beaten in one pipeline, so this is only a clock-skew guard.
            summary["alive"] += 1
            continue
        # One sweep acts on a given run at a time.
        if not client.set(
            CLAIM_KEY.format(task_id=task_id), "1", nx=True, ex=config.TASK_HEARTBEAT_TTL
        ):
            continue

        name = record.get("name")
        attempt = int(record.get("attempt") or 0)
        too_old = now - recorded_at > config.TASK_REPLAY_MAX_AGE
        if not is_replayable(name) or too_old or _task_row_is_terminal(task_id):
            client.hdel(REPLAY_HASH, task_id)
            summary["dropped"] += 1
            continue
        if attempt >= config.TASK_REPLAY_MAX_ATTEMPTS:
            client.hdel(REPLAY_HASH, task_id)
            logger.error(
                "%s[%s] lost its worker %d time(s); not replaying it again",
                name,
                task_id,
                attempt + 1,
            )
            _mark_task_row(
                task_id,
                "failed",
                f"Worker lost {attempt + 1} time(s) while running this task (e.g. out of memory)",
            )
            summary["exhausted"] += 1
            continue

        record["attempt"] = attempt + 1
        client.hset(REPLAY_HASH, task_id, json.dumps(record))
        options: dict[str, Any] = {"task_id": task_id}
        if record.get("queue"):
            options["queue"] = record["queue"]
        if record.get("priority") is not None:
            options["priority"] = record["priority"]
        send_task(name, args=record.get("args") or [], kwargs=record.get("kwargs") or {}, **options)
        _mark_task_row(task_id, "pending", None)
        logger.warning(
            "Replayed %s[%s] after its worker was lost (replay %d of %d)",
            name,
            task_id,
            attempt + 1,
            config.TASK_REPLAY_MAX_ATTEMPTS,
        )
        summary["replayed"] += 1
    return summary
