"""Stand a pipeline stage down when its run has been superseded (issue #1020).

Recovery can fail a transcription and dispatch a replacement while the original message is
still waiting in the broker, and a worker crash can redeliver a message long after its run
was replaced. Without a check, the original stage then set its failed Task back to
``in_progress`` and ran to completion beside the replacement: two transcriptions of one file,
with the loser's status overwriting the winner's.

Every stage that starts real work calls :func:`superseded_result` first and returns its
payload unchanged when it is not ``None``. That return acks the message under
``acks_late=True`` and flows to ``finalize_transcription``, which no-ops on it. It deliberately
does **not** travel the cancellation path: ``finish_cancelled`` and the cancelled finalize
branch release the file's temp audio, which the replacement run is using.

The test is the run's own Task row, not ``MediaFile.active_task_id``. ``create_task_record``
points ``active_task_id`` at *any* task created for the file (a summarization, a speaker
embedding), so comparing against it would stand down a perfectly current transcription.

It also stands down a second DELIVERY of the stage message that is executing right now: two
copies of one message can exist when the broker-side reaper (``app/core/broker_orphans.py``)
puts a message back on its queue while the worker that held it turns out to be alive after
all, or when the visibility timeout redelivers a long run. The run's lease
(``core/task_liveness.py``) records which message holds it; a copy that finds its own
message id there, or finds its message already completed in the result backend, is a
duplicate and does nothing. Run-level fencing by ``task_id`` keeps every other duplicate
(a replaced run, a failed run) harmless.
"""

from __future__ import annotations

import logging

from app.db.session_utils import session_scope
from app.models.media import Task
from app.utils.task_utils import TASK_STATUS_COMPLETED
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import TASK_STATUS_SKIPPED

logger = logging.getLogger(__name__)

SUPERSEDED = "superseded"

_TERMINAL = (TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_SKIPPED)


def _superseded_reason(task_id: str) -> tuple[str | None, int | None]:
    """Why this run must not proceed (``None`` when it is current), plus its file id.

    Only positive evidence stands a run down. A missing Task row or an unreadable database
    lets the stage proceed exactly as it did before this check existed: the stage's own
    lookups then fail loudly, which is better than silently dropping a run on a guess.
    """
    try:
        return _read_superseded_reason(task_id)
    except Exception as e:
        logger.warning("Could not check whether run %s was superseded: %s", task_id, e)
        return None, None


def _duplicate_delivery_reason(task_id: str) -> str | None:
    """Why this delivery is a copy of a stage that is running or already finished, or None.

    Fails open (None) on any error, like the rest of this module: a missed duplicate costs
    compute, a wrongly dropped stage strands a file.
    """
    from app.core.task_liveness import RunState
    from app.core.task_liveness import _current_stage_id
    from app.core.task_liveness import probe_runs

    stage_id = _current_stage_id()
    if not stage_id:
        return None
    run = probe_runs([task_id]).get(task_id)
    if run is not None and run.state == RunState.RUNNING and run.owner == stage_id:
        return "another delivery of this stage is still executing"
    try:
        from celery.result import AsyncResult

        from app.core.celery import celery_app

        if AsyncResult(stage_id, app=celery_app).state == "SUCCESS":
            return "this stage already completed"
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.debug("Could not read the result of stage %s: %s", stage_id, e)
    return None


def replaced_by_newer_run(task_id: str) -> bool:
    """Whether a newer transcription of the same file has replaced run ``task_id``.

    For the failure paths: a run whose failure the retry policy answered with a new run must
    leave the file (status, temp audio) to that run. Fails closed (False) on any error, so an
    unreadable database keeps the old behaviour of handling the failure.
    """
    try:
        with session_scope() as db:
            task = db.query(Task).filter(Task.id == task_id).first()
            if task is None or task.media_file_id is None or task.created_at is None:
                return False
            newer = (
                db.query(Task.id)
                .filter(
                    Task.media_file_id == task.media_file_id,
                    Task.task_type == "transcription",
                    Task.id != task_id,
                    Task.created_at > task.created_at,
                )
                .first()
            )
            return newer is not None
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.warning("Could not check whether run %s was replaced: %s", task_id, e)
        return False


def _read_superseded_reason(task_id: str) -> tuple[str | None, int | None]:
    reason, file_id = _read_db_superseded_reason(task_id)
    if reason is None:
        reason = _duplicate_delivery_reason(task_id)
    return reason, file_id


def _read_db_superseded_reason(task_id: str) -> tuple[str | None, int | None]:
    with session_scope() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            return None, None
        file_id = int(task.media_file_id) if task.media_file_id is not None else None
        if task.status in _TERMINAL:
            return f"its Task is already {task.status}", file_id
        if file_id is None or task.created_at is None:
            return None, file_id
        newer = (
            db.query(Task.id)
            .filter(
                Task.media_file_id == file_id,
                Task.task_type == "transcription",
                Task.id != task_id,
                Task.created_at > task.created_at,
            )
            .first()
        )
        if newer is not None:
            return f"a newer transcription ({newer[0]}) replaced it", file_id
        return None, file_id


def superseded_result(task_id: str, file_uuid: str, *, stage: str) -> dict | None:
    """The chain payload that ends a superseded run, or ``None`` when the run is current.

    Args:
        task_id: The application task id the stage was dispatched with.
        file_uuid: For the payload and the log line.
        stage: Which stage is checking, for the log line.
    """
    reason, file_id = _superseded_reason(task_id)
    if reason is None:
        return None
    logger.warning(
        "%s for file %s: run %s was superseded (%s) -- not running it",
        stage,
        file_uuid,
        task_id,
        reason,
    )
    return {"status": SUPERSEDED, "file_uuid": file_uuid, "file_id": file_id, "task_id": task_id}


def superseded_or_none(context: dict, *, stage: str) -> dict | None:
    """:func:`superseded_result` for a stage that receives the previous stage's payload.

    A payload that is already a superseded marker is passed straight through: the stage
    before this one stood down, and its payload lacks the keys a real run carries.
    """
    if context.get("status") == SUPERSEDED:
        return context
    return superseded_result(context["task_id"], context["file_uuid"], stage=stage)
