"""Task-layer outcome for a user-cancelled transcription (issue #823).

``app/core/task_cancellation.py`` owns the SIGNAL (the Redis flag, the per-thread scope and
the checkpoint). This module owns what happens when that checkpoint fires inside a Celery
task, and it is deliberately **not** ``context.requeue_after_abort``:

======================  ==========================================  =========================
trigger                 outcome                                     why
======================  ==========================================  =========================
worker shutdown (#809)  ``Reject(requeue=True)`` — run it elsewhere  the work is still wanted
user cancel (#823)      :func:`finish_cancelled` — stop, for good    the user asked it to stop
======================  ==========================================  =========================

Requeueing a cancelled job would resurrect it on the next worker, so the UI would say
"Cancelled" and the file would then go back to processing — strictly worse than #823's original
symptom, where at least the *status* stayed put.

**The two-phase status machine.** ``cancel_active_task`` no longer flips the file straight to
``CANCELLED``; it arms the flag and moves the file to ``FileStatus.CANCELLING`` — a value the
enum, the API filter list, ``formatting_service`` ("Cancelling", ``status-cancelling``) and the
frontend's ``MediaFileStatus`` union have all carried since before this change and which
nothing had ever written. So:

* ``CANCELLING`` — "we asked it to stop", the honest state while the worker has not answered.
* ``CANCELLED`` — written HERE and only here, i.e. "it actually stopped". ``Task.completed_at``
  is the timestamp of the confirmed stop.

:func:`reconcile_cancellation` is what keeps ``CANCELLING`` from being a state a file can wedge
in — see its docstring for the one case it exists for and the trade it makes.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from app.core.celery import celery_app
from app.core.task_cancellation import clear_cancel
from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import update_media_file_status

if TYPE_CHECKING:
    # Under TYPE_CHECKING only: `context` imports nothing from here, but keeping the runtime
    # import surface of this module to what it actually executes is what lets `core.py` import
    # `finish_cancelled` before `context` without an ordering question.
    from .context import TranscriptionContext

logger = logging.getLogger(__name__)

#: Message recorded on the ``Task`` row, and the string the pre-#823 ``cancel_active_task``
#: already wrote. Kept identical so existing rows and new ones read the same in the Tasks UI.
CANCELLED_BY_USER = "Task cancelled by user"

#: How long ``reconcile_cancellation`` waits before resolving a file the worker never answered
#: for.
#:
#: Derived from what the cooperative checkpoints actually bound, not picked:
#:
#: * A **queued** run stands down on pickup — ``_GpuStage.run entry`` and its siblings fire
#:   before the model is warmed, so this window only has to exceed normal queue wait.
#: * A **running** run stands down at the next checkpoint. The widest gap between two
#:   checkpoints is the diarization call itself, which is opaque and can run for minutes on a
#:   multi-hour file.
#:
#: 600 s comfortably covers the first and most of the second while keeping the state
#: observably terminal. ⚠️ It is a **bounded trade, not a guarantee**: if a diarization pass
#: outlives the window, this resolves the file to ``CANCELLED`` while the GPU is still inside
#: that call — i.e. the pre-#823 behaviour, now capped at ten minutes and logged at WARNING
#: instead of unbounded and silent. The work still stops at the next checkpoint, and
#: :func:`finish_cancelled` is idempotent when it gets there.
CANCEL_RECONCILE_DELAY_S = 600


def _owns_the_file(media_file: MediaFile, task_id: str) -> bool:
    """Whether ``task_id`` is still the run this file's status belongs to.

    Two ways a cancelled run can find the file no longer its to write, and both are reachable
    through documented product flows:

    * The file reached a terminal state on its own. A run that finished before the cancel
      landed is legitimately ``COMPLETED``; overwriting that with ``CANCELLED`` would discard a
      transcript the user has.
    * **A NEW run has claimed the file.** ``POST /files/{uuid}/reprocess`` cancels the active
      task and immediately dispatches a fresh pipeline (``api/endpoints/files/reprocess.py``),
      which stamps a new ``active_task_id`` and puts the file back to ``PROCESSING``. The old
      run's checkpoint then fires — potentially minutes later, mid-decode — and without this
      check it would mark the file ``CANCELLED`` and null out the *new* run's
      ``active_task_id``, killing a reprocess the user just asked for.

    ``active_task_id is None`` reads as "not superseded": the run's own terminal bookkeeping
    clears it, so the file is this run's to finish.
    """
    if media_file.status in (FileStatus.COMPLETED, FileStatus.ERROR, FileStatus.CANCELLED):
        return False
    active = media_file.active_task_id
    return active is None or str(active) == task_id


def finish_cancelled(
    ctx: TranscriptionContext,
    task_id: str,
    file_uuid: str,
    cancelled: Exception,
    *,
    stage: str,
) -> dict:
    """Terminal handler for a ``TranscriptionCancelledError`` reaching the task layer (#823).

    THE single implementation, shared by all three tasks that can reach a cooperative
    checkpoint — ``transcribe_gpu_task`` (``core.py``), ``diarize_gpu_task``
    (``diarize_task.py``) and ``transcribe_cpu_task`` (``cpu_task.py``) — for the same reason
    ``requeue_after_abort`` is: three copies of a "do NOT travel the failure path, do NOT
    requeue" rule is three chances for one of them to drift.

    Three things it must not do, each of which was a live wrong answer:

    * **Not ``_handle_transcription_failure``.** A cancel is not broken work; marking the file
      ERROR and pushing an error notification tells the user their file failed when they are
      the one who stopped it. The ``Task`` row is written inline here rather than through
      ``update_task_status`` for the same reason — that helper's terminal branch re-derives the
      file status from the task rows and lands on ``ERROR``, and it copies ``error_message``
      onto ``MediaFile.last_error_message``, which is for failures.
    * **Not ``Reject(requeue=True)``.** See the module docstring.
    * **Not a bare ``raise``.** Returning normally is what makes ``acks_late=True`` ack the
      message, which is precisely the goal: the job must not come back. The successor link in
      the chain (``finalize_transcription``) is dispatched by that normal return and no-ops on
      ``status == "cancelled"``, which is also what cleans up the temp audio.

    Idempotent: a file already ``CANCELLED``/``COMPLETED``/``ERROR`` is left alone, so a late
    checkpoint arriving after :func:`reconcile_cancellation` has resolved the file is a no-op
    rather than a second set of notifications.

    Args:
        ctx: The stage's ``TranscriptionContext`` (used for ``file_id``/``user_id``).
        task_id: The run that was cancelled — the key the flag was armed under.
        file_uuid: For the log line and the returned payload.
        cancelled: The ``TranscriptionCancelledError`` that reached the task layer, for the log.
        stage: Which leg stood down ("GPU transcription", "GPU diarization", ...). Named
            explicitly rather than derived, so the log says which leg stopped when several are
            draining at once.

    Returns:
        The chain payload ``finalize_transcription`` recognises as "stop here".
    """
    logger.info(
        "%s for file %s stood down for a user cancellation (%s) -- not requeueing",
        stage,
        file_uuid,
        cancelled,
    )

    write_status = False
    with session_scope() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is not None:
            task.status = TASK_STATUS_FAILED  # type: ignore[assignment]
            task.error_message = CANCELLED_BY_USER  # type: ignore[assignment]
            # The confirmed-stop timestamp: this row is written only once the checkpoint has
            # actually fired, so "when did it really stop" is answerable rather than inferred.
            task.completed_at = datetime.now(UTC)  # type: ignore[assignment]
            task.updated_at = datetime.now(UTC)  # type: ignore[assignment]

        media_file = get_refreshed_object(db, MediaFile, ctx.file_id)
        if media_file is not None:
            write_status = _owns_the_file(media_file, task_id)
            if write_status:
                media_file.active_task_id = None
                media_file.task_started_at = None
                media_file.cancellation_requested = False
        db.commit()

        if media_file is not None and write_status:
            # Through update_media_file_status, not a direct assignment: it carries the
            # quarantine/legal-hold gate (issue #824), and a cancelled file may well be one
            # that was quarantined mid-processing.
            update_media_file_status(db, ctx.file_id, FileStatus.CANCELLED)

        # Cloud-edition seam, identical to the failure path's: a run that ends without
        # producing a transcript must still fire the completion hook (success=False) so the
        # quota layer releases the reservation taken at dispatch. No-op in community.
        try:
            from .hooks import CompletionContext
            from .hooks import fire_transcription_complete

            fire_transcription_complete(
                CompletionContext(
                    file_id=ctx.file_id,
                    file_uuid=str(file_uuid),
                    user_id=ctx.user_id,
                    organization_id=media_file.organization_id if media_file else None,
                    audio_duration_s=0.0,
                    run_id=task_id,
                    provider="local",
                    success=False,
                )
            )
        except Exception:  # pragma: no cover — hook layer already contains
            logger.exception("Cancellation completion hook raised (contained)")

    # Outside the session on purpose (the package's session-lifetime rule): this reaches Redis
    # and opens its own session for the file metadata.
    if write_status:
        from .notifications import send_notification_via_redis

        send_notification_via_redis(
            ctx.user_id, ctx.file_id, FileStatus.CANCELLED, "Processing cancelled", 0
        )

    clear_cancel(task_id)

    return {
        "status": "cancelled",
        "file_uuid": file_uuid,
        "file_id": ctx.file_id,
        "task_id": task_id,
    }


@celery_app.task(name="transcription.reconcile_cancellation", ignore_result=True)
def reconcile_cancellation(file_id: int, task_id: str) -> dict:
    """Resolve a file left in ``CANCELLING`` because no worker ever confirmed the stop.

    Dispatched by ``cancel_active_task`` with ``countdown=CANCEL_RECONCILE_DELAY_S``. In the
    normal case it finds the file already ``CANCELLED`` (the checkpoint fired, seconds later)
    and does nothing; it exists for the case a checkpoint can never reach, namely a run whose
    worker died — or, on a ``--lite`` deployment, a run published into a queue nothing drains
    (issue #865). Without it ``CANCELLING`` would be a state a file can sit in indefinitely,
    and the existing stuck-file sweeps cannot help: they key on ``PROCESSING``.

    A no-op for any file that has since reached a terminal state on its own — a run that
    finished before the cancel landed is legitimately ``COMPLETED``, and overwriting that with
    ``CANCELLED`` would destroy a transcript the user has.

    ⚠️ It resolves to ``CANCELLED`` **without** proof the work stopped, and logs at WARNING
    saying so. See :data:`CANCEL_RECONCILE_DELAY_S` for why that trade is bounded and why it is
    still strictly better than the unbounded, silent version #823 was filed about.

    Args:
        file_id: The file whose cancellation is being reconciled.
        task_id: The run the cancellation was armed against.

    Returns:
        ``{"outcome": ...}`` — ``"confirmed"`` (a worker answered), ``"not_cancelling"`` (the
        file moved on), or ``"forced"`` (this task resolved it unconfirmed).
    """
    user_id: int | None = None
    outcome = "confirmed"

    with session_scope() as db:
        media_file = get_refreshed_object(db, MediaFile, file_id)
        if media_file is None:
            return {"outcome": "missing"}
        if media_file.status != FileStatus.CANCELLING:
            # Either finish_cancelled already ran (the common case) or the run reached a
            # terminal state of its own.
            confirmed = media_file.status == FileStatus.CANCELLED
            return {"outcome": "confirmed" if confirmed else "not_cancelling"}

        if not _owns_the_file(media_file, task_id):
            # A reprocess re-dispatched while this file sat in CANCELLING — the status now
            # belongs to that run, not to this backstop. Same guard, same reason, as
            # finish_cancelled's.
            return {"outcome": "superseded"}

        user_id = int(media_file.user_id)
        outcome = "forced"
        logger.warning(
            "File %s is still CANCELLING %ds after the request — no worker confirmed the stop "
            "for task %s. Resolving to CANCELLED unconfirmed; if a stage is still running it "
            "will stand down at its next checkpoint (issue #823).",
            file_id,
            CANCEL_RECONCILE_DELAY_S,
            task_id,
        )

        task = db.query(Task).filter(Task.id == task_id).first()
        if task is not None and task.completed_at is None:
            task.status = TASK_STATUS_FAILED  # type: ignore[assignment]
            task.error_message = CANCELLED_BY_USER  # type: ignore[assignment]
            task.completed_at = datetime.now(UTC)  # type: ignore[assignment]
            task.updated_at = datetime.now(UTC)  # type: ignore[assignment]

        media_file.active_task_id = None
        media_file.task_started_at = None
        media_file.cancellation_requested = False
        db.commit()

        update_media_file_status(db, file_id, FileStatus.CANCELLED)

    if outcome == "forced" and user_id is not None:
        from .notifications import send_notification_via_redis

        send_notification_via_redis(
            user_id, file_id, FileStatus.CANCELLED, "Processing cancelled", 0
        )

    # ⚠️ The flag is deliberately LEFT ARMED. Clearing it here would let a stage that is still
    # running sail past its next checkpoint, finish, save segments and mark the file COMPLETED
    # — resurrecting the very job this task has just reported as cancelled. It expires on its
    # own TTL, and `finish_cancelled` clears it if the stage does reach a checkpoint.
    return {"outcome": outcome}
