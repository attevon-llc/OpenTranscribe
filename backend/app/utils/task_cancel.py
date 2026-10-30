"""Cooperative cancellation of a media file's active task (issue #823)."""

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.env import env_bool
from app.db.session_utils import get_refreshed_object
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import update_media_file_status

logger = logging.getLogger(__name__)


def _finalize_unconfirmed_cancellation(db: Session, media_file: MediaFile, task_id: str) -> None:
    """Flip a file straight to CANCELLED when no cooperative stop could be armed.

    The pre-#823 behaviour, kept for the one case that still needs it: Redis was unreachable,
    so ``request_cancel`` could not write the flag and no checkpoint will ever fire. Leaving
    the file ``CANCELLING`` then would wedge it until the reconciliation task ran, for a stop
    that was never actually requested of the worker.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.status = TASK_STATUS_FAILED  # type: ignore[assignment]
        task.error_message = "Task cancelled by user"  # type: ignore[assignment]
        task.completed_at = datetime.now(UTC)  # type: ignore[assignment]

    media_file.status = FileStatus.CANCELLED
    media_file.active_task_id = None  # type: ignore[assignment]
    media_file.task_started_at = None  # type: ignore[assignment]
    media_file.cancellation_requested = False


def cancel_active_task(db: Session, file_id: int) -> bool:
    """Ask the active task for a media file to stop, cooperatively (issue #823).

    **What this used to do, and why none of it worked.** It called
    ``celery_app.control.revoke(active_task_id, terminate=True)`` and flipped the file to
    ``CANCELLED`` in the same breath. ``terminate=True`` signals the pool worker running the
    task, which does nothing under the ``--pool=threads`` every GPU queue runs — and, worse,
    ``active_task_id`` holds the *application* task id ``dispatch.py`` mints, never the celery
    message id, so the revoke targeted an id no worker has ever seen on ANY pool. The GPU kept
    decoding while the UI said the job had stopped; on a single-GPU host that blocks the user's
    own next upload. The ``revoke()`` call is gone rather than repaired: threading celery's ids
    through would buy only the prefork legs, and the cooperative path below covers every leg.

    **What it does now — two phases.** ``request_cancel`` arms a per-run flag the running task
    polls at its own checkpoints (``app/core/task_cancellation.py``), and the file moves to
    ``FileStatus.CANCELLING`` — "we asked". The worker writes ``CANCELLED`` when it has
    actually stood down (``tasks/transcription/cancellation.finish_cancelled``), so the status
    distinguishes the request from the confirmed stop instead of asserting the stop optimistically.
    ``reconcile_cancellation`` is queued with a countdown so ``CANCELLING`` cannot wedge.

    ``active_task_id`` is deliberately **retained** while ``CANCELLING``: it is the handle the
    reconciliation task and the worker's own confirmation both key on, and clearing it here is
    what would make the two phases unable to find each other.

    Args:
        db: Database session
        file_id: ID of the media file

    Returns:
        True when the cancellation was recorded — either armed cooperatively (file is now
        ``CANCELLING``) or, when the flag could not be armed, finalized outright (file is now
        ``CANCELLED``). False when there is nothing to cancel or the write failed.
    """
    from app.core.task_cancellation import request_cancel

    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file or not media_file.active_task_id:
        return False

    task_id = str(media_file.active_task_id)
    file_uuid = str(media_file.uuid)

    try:
        armed = request_cancel(task_id, file_uuid)

        if not armed:
            _finalize_unconfirmed_cancellation(db, media_file, task_id)
            db.commit()
            logger.error(
                f"Could not arm the cooperative cancellation for file {file_id} "
                f"(task {task_id}) -- Redis is unreachable. The file is marked CANCELLED but "
                f"the worker was never told to stop, so the work may still be running "
                f"(issue #823)."
            )
            return True

        media_file.cancellation_requested = True
        # Through update_media_file_status for the quarantine/legal-hold gate (issue #824),
        # rather than assigning `.status` directly.
        update_media_file_status(db, file_id, FileStatus.CANCELLING)

        logger.info(
            f"Cancellation armed for file {file_id} (task {task_id}): status CANCELLING. "
            f"The running stage stands down at its next checkpoint and confirms by writing "
            f"CANCELLED (issue #823)."
        )
    except Exception as e:
        logger.error(f"Failed to cancel task for file {file_id}: {e}")
        return False

    _schedule_cancellation_reconcile(file_id, task_id)
    return True


def _schedule_cancellation_reconcile(file_id: int, task_id: str) -> None:
    """Queue the bounded backstop that resolves a file the worker never answers for.

    Dispatch failures are logged, not raised: the cooperative flag is already armed and the
    cancellation is genuinely under way, so reporting the whole request as failed because a
    *backstop* could not be queued would be the wrong answer. Skipped under ``SKIP_CELERY``
    like every other dispatch site in this module's callers.
    """
    if env_bool("SKIP_CELERY"):
        return
    try:
        from app.core.queues import CeleryQueues
        from app.tasks.transcription.cancellation import CANCEL_RECONCILE_DELAY_S
        from app.tasks.transcription.cancellation import reconcile_cancellation

        reconcile_cancellation.apply_async(
            args=[file_id, task_id],
            countdown=CANCEL_RECONCILE_DELAY_S,
            queue=CeleryQueues.UTILITY,
        )
    except Exception as e:
        logger.warning(
            f"Could not queue the cancellation reconciliation for file {file_id} "
            f"(task {task_id}): {e}. The cooperative stop is still armed; the file will stay "
            f"CANCELLING if no worker confirms it."
        )
