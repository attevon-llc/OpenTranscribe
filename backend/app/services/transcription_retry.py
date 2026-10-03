"""The one retry policy for a transcription run that ended without finishing.

Every path that ends a run early comes through here, so the rules below hold no matter how
the run ended:

* A stage caught an exception (``finish_failed_run``, called by the stage failure handlers).
* The worker holding a stage died and the broker still has its message
  (``app/core/broker_orphans.py`` asks :func:`allow_infra_requeue` before putting the
  message back at the front of its queue).
* The worker died and the message is gone too (``recover_lost_run``, the health check's
  fallback).

Two classes of failure, two budgets:

PERMANENT (the input is at fault: corrupt or undecodable media, no audio, no speech, an
unsupported format, removed content). Fails at once with the classified user-facing sentence
and is never retried automatically: running the same input again gives the same answer.

TRANSIENT (the infrastructure is at fault: a worker died, out of memory, a lost connection, a
timeout; and anything unclassified). Requeued within minutes, ahead of newer submissions,
until a budget runs out; only then does the file go to ERROR, with a sentence that says it was
interrupted and retried. There are two budgets because the two causes behave differently:

* ``MediaFile.retry_count`` -- a stage RAISED. Bounded by the admin's retry settings
  (``transcription.max_retries`` / ``transcription.retry_limit_enabled``), with exponential
  backoff and jitter between attempts.
* the infrastructure-requeue counter (``task_liveness.record_infra_requeue``) -- the worker
  VANISHED. Deploys, node loss and SIGKILLs must not spend the file's error budget, but a file
  that kills every worker it reaches (a poison input) must still stop:
  ``TRANSCRIPTION_MAX_INFRA_REQUEUES``.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from enum import StrEnum

from app.core.task_config import task_recovery_config
from app.core.task_liveness import clear_infra_requeues
from app.core.task_liveness import record_infra_requeue
from app.core.task_liveness import supersede_run
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services import system_settings_service
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import FailureClassification
from app.utils.error_classification import INFRASTRUCTURE_CATEGORIES
from app.utils.error_classification import ErrorCategory
from app.utils.error_classification import is_transient
from app.utils.error_classification import transient_retry_delay
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

logger = logging.getLogger(__name__)

#: What a worker-loss recovery stores on the run's Task row.
WORKER_LOST_MESSAGE = "The worker running this file stopped; it was requeued automatically."


class RunOutcome(StrEnum):
    """What the policy did with a run that ended early."""

    RETRIED = "retried"  # a new run was dispatched at retry priority
    FAILED = "failed"  # the file is in ERROR with its reason


def _error_budget_allows(db, media_file: MediaFile, category: ErrorCategory) -> bool:
    if not is_transient(category):
        return False
    retry_count = int(media_file.retry_count or 0)
    if category == ErrorCategory.AUTH_OR_RATE_LIMIT and retry_count >= 2:
        return False
    return system_settings_service.should_retry_file(db, retry_count)


def _retire_run(db, task_id: str, file_uuid: str, message: str) -> None:
    """Mark the run's Task row failed WITHOUT re-deriving the file's status from it.

    ``update_task_status`` would run the file-status aggregate, which turns a PROCESSING file
    whose only transcription just failed into ERROR (or into COMPLETED, when an older
    unrelated task of the file completed) -- that aggregate is how worker loss used to strand
    files in ERROR. The caller sets the file's status explicitly instead.
    """
    supersede_run(task_id, file_uuid)
    task = db.query(Task).filter(Task.id == task_id).first()
    if task is not None and task.status in ("pending", "in_progress"):
        now = datetime.now(UTC)
        task.status = "failed"  # type: ignore[assignment]
        task.error_message = message  # type: ignore[assignment]
        task.completed_at = now  # type: ignore[assignment]
        task.updated_at = now  # type: ignore[assignment]
    db.commit()


def _release_reservation(media_file: MediaFile, task_id: str) -> None:
    """A run that ended without finishing still fires the completion hook (success=False)."""
    try:
        from app.tasks.transcription.hooks import CompletionContext
        from app.tasks.transcription.hooks import fire_transcription_complete

        fire_transcription_complete(
            CompletionContext(
                file_id=int(media_file.id),
                file_uuid=str(media_file.uuid),
                user_id=int(media_file.user_id),
                organization_id=media_file.organization_id,
                audio_duration_s=0.0,
                run_id=task_id,
                provider="local",
                success=False,
            )
        )
    except Exception:  # pragma: no cover - the hook layer already contains failures
        logger.exception("Failure-path completion hook raised (contained)")


def _fail_file(
    db,
    media_file: MediaFile,
    task_id: str,
    category: ErrorCategory,
    message: str,
    *,
    release: bool = True,
) -> RunOutcome:
    file_id = int(media_file.id)
    user_id = int(media_file.user_id)
    file_uuid = str(media_file.uuid)
    update_task_status(db, task_id, "failed", error_message=message, completed=True)
    update_media_file_status(db, file_id, FileStatus.ERROR)
    media_file.last_error_message = message  # type: ignore[assignment]
    media_file.error_category = category.value  # type: ignore[assignment]
    db.commit()
    supersede_run(task_id, file_uuid)
    clear_infra_requeues(file_uuid)
    if release:
        _release_reservation(media_file, task_id)

    from app.tasks.transcription.notifications import send_error_notification

    send_error_notification(user_id, file_id, message)
    return RunOutcome.FAILED


def _dispatch_retry(media_file_id: int, countdown: int | None) -> bool:
    from app.services.task_recovery_service import task_recovery_service

    return task_recovery_service.schedule_file_retry(media_file_id, countdown=countdown)


def _retry_file(
    db,
    media_file: MediaFile,
    task_id: str,
    category: ErrorCategory,
    *,
    task_message: str,
    countdown: int | None,
    attempt_note: str,
) -> RunOutcome:
    """Retire run ``task_id`` and dispatch a replacement at retry priority."""
    file_id = int(media_file.id)
    file_uuid = str(media_file.uuid)
    _retire_run(db, task_id, file_uuid, task_message)
    media_file.error_category = category.value  # type: ignore[assignment]
    media_file.last_recovery_attempt = datetime.now(UTC)  # type: ignore[assignment]
    update_media_file_status(db, file_id, FileStatus.PENDING)
    _release_reservation(media_file, task_id)

    if not _dispatch_retry(file_id, countdown):
        logger.error("Could not dispatch the retry of file %s; failing it", file_id)
        return _fail_file(
            db,
            media_file,
            task_id,
            ErrorCategory.RETRIES_EXHAUSTED,
            ErrorCategorizationService.interrupted_message(),
            release=False,  # released above, before the dispatch was attempted
        )

    logger.warning(
        "Requeued file %s after %s (%s)%s",
        file_id,
        category.value,
        attempt_note,
        f", starting in {countdown}s" if countdown else "",
    )
    try:
        from app.tasks.transcription.notifications import send_progress_notification

        send_progress_notification(
            int(media_file.user_id), file_id, 0.0, "Retrying after a temporary problem"
        )
    except Exception as e:  # noqa: BLE001 - a missed notification must not undo the retry
        logger.debug("Retry notification for file %s failed: %s", file_id, e)
    return RunOutcome.RETRIED


def finish_failed_run(task_id: str, file_id: int, failure: FailureClassification) -> RunOutcome:
    """Decide the fate of a run whose stage raised: fail it, or requeue the file.

    Args:
        task_id: The run (application task id).
        file_id: Its file.
        failure: ``ErrorCategorizationService.classify_failure`` of the raw exception.

    Returns:
        ``RETRIED`` when a replacement run was dispatched (the stage must then leave the file
        and its temp audio to that run), ``FAILED`` when the file is now in ERROR.
    """
    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if media_file is None:
            update_task_status(
                db, task_id, "failed", error_message=failure.user_message, completed=True
            )
            return RunOutcome.FAILED
        return _finish(db, media_file, task_id, failure.retry_category, failure.user_message)


def _finish(
    db, media_file: MediaFile, task_id: str, category: ErrorCategory, user_message: str
) -> RunOutcome:
    if not is_transient(category):
        return _fail_file(db, media_file, task_id, category, user_message)
    retry_count = int(media_file.retry_count or 0)
    if not _error_budget_allows(db, media_file, category):
        logger.warning(
            "File %s failed with %s after %d automatic retries; not retrying again",
            media_file.id,
            category.value,
            retry_count,
        )
        # An infrastructure cause reads as "interrupted and retried"; an unclassified one keeps
        # its own sentence, which says more than a guess about the cause would.
        message = (
            ErrorCategorizationService.interrupted_message()
            if category in INFRASTRUCTURE_CATEGORIES
            else user_message
        )
        return _fail_file(db, media_file, task_id, ErrorCategory.RETRIES_EXHAUSTED, message)
    media_file.retry_count = retry_count + 1  # type: ignore[assignment]
    return _retry_file(
        db,
        media_file,
        task_id,
        category,
        task_message=user_message,
        countdown=transient_retry_delay(category, retry_count),
        attempt_note=f"error retry {retry_count + 1}",
    )


def recover_overrun_run(db, task: Task) -> RunOutcome | None:
    """A run still heartbeating past its duration budget: a hung stage, treated as transient.

    Spends the error budget (with backoff) rather than the infrastructure one: the worker is
    alive, so this is the run failing, not the run being lost. ``supersede_run`` arms the
    cooperative cancel so the hung stage stands down if it ever reaches a checkpoint.
    """
    media_file = (
        db.query(MediaFile).filter(MediaFile.id == task.media_file_id).first()
        if task.media_file_id is not None
        else None
    )
    if media_file is None:
        return None
    return _finish(
        db,
        media_file,
        str(task.id),
        ErrorCategory.SYSTEM_ERROR,
        "Processing took longer than allowed and was stopped.",
    )


def _cancel_requested(media_file: MediaFile | None, run_id: str) -> bool:
    """Whether run ``run_id`` was asked to stop and no worker has confirmed it yet.

    The run's Task row stays ``in_progress`` until a worker confirms the stop or
    ``reconcile_cancellation`` resolves it, so the row alone reads such a run as current.
    Scoped to this run: its own cancellation flag, or the file's cancel state while the file
    still points at this run (the same ownership rule ``finish_cancelled`` applies), so an
    earlier cancel never reads onto a later run of the same file.
    """
    from app.core.task_cancellation import cancel_requested

    if cancel_requested(run_id):
        return True
    if media_file is None:
        return False
    cancelling = bool(media_file.cancellation_requested) or media_file.status in (
        FileStatus.CANCELLING,
        FileStatus.CANCELLED,
    )
    active = media_file.active_task_id
    return cancelling and (active is None or str(active) == run_id)


def run_status(run_id: str) -> str:
    """``"current"``, ``"finished"`` (terminal, or replaced by a newer run), ``"cancelled"``
    (its file is being cancelled) or ``"unknown"``.

    ``unknown`` covers a missing Task row and an unreadable database alike: the broker reaper
    leaves such a message alone rather than act on a guess. ``cancelled`` must never be run
    again: the user asked it to stop.
    """
    try:
        with session_scope() as db:
            task = db.query(Task).filter(Task.id == run_id).first()
            if task is None:
                return "unknown"
            if task.status not in ("pending", "in_progress"):
                return "finished"
            media_file = (
                db.query(MediaFile).filter(MediaFile.id == task.media_file_id).first()
                if task.media_file_id is not None
                else None
            )
            if _cancel_requested(media_file, run_id):
                return "cancelled"
            if task.media_file_id is not None and task.created_at is not None:
                newer = (
                    db.query(Task.id)
                    .filter(
                        Task.media_file_id == task.media_file_id,
                        Task.task_type == "transcription",
                        Task.id != run_id,
                        Task.created_at > task.created_at,
                    )
                    .first()
                )
                if newer is not None:
                    return "finished"
            return "current"
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.warning("Could not read the state of run %s: %s", run_id, e)
        return "unknown"


def allow_infra_requeue(file_uuid: str) -> tuple[bool, int]:
    """Count one infrastructure requeue of ``file_uuid`` and say whether it is within the cap.

    Returns ``(allowed, count)``. An unreadable Redis allows the requeue: the counter lives in
    the broker's own Redis, so if it cannot be read nothing can be requeued either.
    """
    count = record_infra_requeue(file_uuid)
    if count is None:
        return True, 0
    return count <= task_recovery_config.TRANSCRIPTION_MAX_INFRA_REQUEUES, count


def fail_interrupted_run(task_id: str) -> RunOutcome | None:
    """Fail a run whose infrastructure requeues are used up. None if the run is unknown."""
    with session_scope() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None or task.media_file_id is None:
            return None
        media_file = db.query(MediaFile).filter(MediaFile.id == task.media_file_id).first()
        if media_file is None:
            return None
        logger.error(
            "File %s lost its worker %d times; failing it instead of requeueing it again",
            media_file.id,
            task_recovery_config.TRANSCRIPTION_MAX_INFRA_REQUEUES,
        )
        return _fail_file(
            db,
            media_file,
            task_id,
            ErrorCategory.RETRIES_EXHAUSTED,
            ErrorCategorizationService.interrupted_message(),
        )


def recover_lost_run(db, task: Task) -> RunOutcome | None:
    """A run whose worker died and whose broker message is gone: re-dispatch or fail it.

    The health check's fallback for a transcription with no lease and no queued marker. The
    broker-side reaper handles the common case (the message is still in the broker) by putting
    that exact stage back; this covers the rest. Spends the infrastructure-requeue budget, not
    the error budget, and dispatches without backoff: nothing about the file was wrong.

    Returns None when there is nothing to recover: the task has no file, the run is being
    cancelled, or another recovery pass already ended the run (the row is re-read under a
    lock, so the reaper and the health check can never both dispatch a replacement for the
    same run).
    """
    current = db.query(Task).filter(Task.id == task.id).with_for_update().first()
    if current is None or current.status not in ("pending", "in_progress"):
        return None
    if task.media_file_id is None:
        return None
    media_file = db.query(MediaFile).filter(MediaFile.id == task.media_file_id).first()
    if media_file is None:
        return None
    if _cancel_requested(media_file, str(task.id)):
        # A replacement run would carry no cancellation flag and transcribe a file the user
        # stopped. The worker is gone, so ``reconcile_cancellation`` resolves it to CANCELLED.
        return None
    from app.core.broker_orphans import discard_run_deliveries

    # Its message may still sit in the broker's unacked set, where it would inflate the
    # reserved metric and be redelivered when the visibility timeout ends.
    discard_run_deliveries(str(task.id))
    allowed, count = allow_infra_requeue(str(media_file.uuid))
    if not allowed:
        logger.error(
            "File %s lost its worker %d times; failing it instead of requeueing it again",
            media_file.id,
            count - 1,
        )
        return _fail_file(
            db,
            media_file,
            str(task.id),
            ErrorCategory.RETRIES_EXHAUSTED,
            ErrorCategorizationService.interrupted_message(),
        )
    return _retry_file(
        db,
        media_file,
        str(task.id),
        ErrorCategory.WORKER_LOST,
        task_message=WORKER_LOST_MESSAGE,
        countdown=None,
        attempt_note=f"infrastructure requeue {count}",
    )


def recover_lost_runs(exclude: set[str]) -> int:
    """Re-dispatch (or fail) every in-flight transcription with no lease and nothing queued.

    The reaper's second half (``core/broker_orphans.reclaim_orphaned_deliveries``): the run's
    worker is gone and so is its message -- dropped by a cold shutdown's cancel, or by a
    broker that lost its data. ``exclude`` holds the runs that still have a message in the
    broker, which the first half handles by putting that exact stage back. Only rows quiet for
    ``BROKER_ORPHAN_STALE_SECONDS`` are considered, so a run between its dispatch commit and
    its queued marker is never mistaken for a lost one. Returns how many runs it acted on.
    """
    from datetime import timedelta

    from sqlalchemy import or_

    from app.core.task_liveness import TRANSCRIPTION_TASK_TYPE
    from app.core.task_liveness import RunState
    from app.core.task_liveness import probe_runs

    cutoff = datetime.now(UTC) - timedelta(seconds=task_recovery_config.BROKER_ORPHAN_STALE)
    acted = 0
    with session_scope() as db:
        candidates = (
            db.query(Task)
            .filter(
                Task.task_type == TRANSCRIPTION_TASK_TYPE,
                Task.status.in_(["pending", "in_progress"]),
                or_(Task.updated_at.is_(None), Task.updated_at < cutoff),
            )
            .all()
        )
        ids = [str(t.id) for t in candidates if str(t.id) not in exclude]
        runs = probe_runs(ids)
        for task in candidates:
            run = runs.get(str(task.id))
            if run is None or run.state != RunState.DEAD:
                continue
            if recover_lost_run(db, task) is not None:
                acted += 1
    return acted
