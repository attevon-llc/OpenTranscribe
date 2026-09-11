import contextlib
import logging
import os
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from celery.result import AsyncResult
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.session_utils import get_refreshed_object
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services import system_settings_service

logger = logging.getLogger(__name__)

# Task status constants
TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
#: A terminal, non-error outcome: the task ran but deliberately did no real work
#: (e.g. an idempotency guard found the result already present, or a duplicate
#: dispatch was caught mid-flight). Distinct from COMPLETED so the Tasks UI can
#: tell "did the work" apart from "correctly decided not to" — see issue #622.
TASK_STATUS_SKIPPED = "skipped"


def create_task_record(
    db: Session, celery_task_id: str, user_id: int, media_file_id: int | None, task_type: str
) -> Task:
    """Create a new task record in the database.

    ``media_file_id`` is ``None`` for a corpus-wide job with no single owning file (e.g.
    issue #626's operator-triggered re-embed, which can span many files across many
    owners) — the column is already ``nullable=True`` and ``update_task_status`` already
    guards its own media-file lookup on ``if media_file_id:``.
    """
    task = Task(
        id=celery_task_id,
        user_id=user_id,
        media_file_id=media_file_id,
        task_type=task_type,
        status=TASK_STATUS_PENDING,
        progress=0.0,
    )
    db.add(task)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Task already exists (race condition or retry), fetch and return it
        task = db.query(Task).filter(Task.id == celery_task_id).first()  # type: ignore[assignment]
        if not task:
            raise ValueError(f"Failed to create or find task: {celery_task_id}") from None
        logger.info(f"Task {celery_task_id} already exists, reusing existing record")

    db.refresh(task)

    # Update the media file with active task tracking — only when this task has one.
    if media_file_id is not None:
        media_file = get_refreshed_object(db, MediaFile, media_file_id)
        if media_file:
            media_file.active_task_id = celery_task_id
            media_file.task_started_at = datetime.now(UTC)
            media_file.task_last_update = datetime.now(UTC)
            media_file.cancellation_requested = False
            if media_file.status == FileStatus.PENDING:
                media_file.status = FileStatus.PROCESSING
            db.commit()

    return task


def update_task_status(
    db: Session,
    task_id: str,
    status: str,
    progress: float | None = None,
    error_message: str | None = None,
    completed: bool = False,
) -> Task | None:
    """Update task status in the database."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        logger.warning(f"Task {task_id} not found")
        return None

    # Log state transition for debugging
    logger.debug(f"Task {task_id} state change: {task.status} -> {status}")

    # Update task fields
    task.status = status  # type: ignore[assignment]
    if progress is not None:
        task.progress = progress  # type: ignore[assignment]
    if error_message:
        task.error_message = error_message  # type: ignore[assignment]
    if completed:
        task.completed_at = datetime.now(UTC)  # type: ignore[assignment]

    # Always update the timestamp for task state changes
    task.updated_at = datetime.now(UTC)  # type: ignore[assignment]

    # Update media file task tracking
    media_file_id = task.media_file_id
    if media_file_id:
        media_file = get_refreshed_object(db, MediaFile, int(media_file_id))
        if media_file:
            media_file.task_last_update = datetime.now(UTC)
            if error_message:
                media_file.last_error_message = error_message

            # Clear active task once it reaches any terminal state — completed,
            # failed, or skipped. Leaving this set after a skip would misreport
            # the file as still busy to `is_file_safe_to_delete` and friends,
            # even though nothing is running any more (issue #622).
            if status in [TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_SKIPPED]:
                media_file.active_task_id = None
                media_file.task_started_at = None

    db.commit()
    db.refresh(task)

    # Terminal states re-check the media file's aggregate status.
    task_media_file_id = task.media_file_id
    if (
        status in [TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_SKIPPED]
        and task_media_file_id
    ):
        update_media_file_from_task_status(db, int(task_media_file_id))

    return task  # type: ignore[no-any-return]


def update_media_file_status(db: Session, file_id: int, status: FileStatus) -> MediaFile | None:
    """Update media file status."""
    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file:
        logger.warning(f"Media file {file_id} not found")
        return None

    # Log state transition for debugging
    logger.debug(f"Media file {file_id} state change: {media_file.status} -> {status}")

    # Issue #824: this is the SECOND writer of status/completed_at (the first is
    # transcription/storage.py's update_media_file_transcription_status) and it
    # used to write both fields unconditionally -- with no quarantine/legal-hold
    # awareness at all -- reached via a separate chain
    # (postprocess.py -> update_task_status -> update_media_file_from_task_status
    # -> here). That made the first writer's guard vacuous: it would decline to
    # write, and this function would clobber QUARANTINED with COMPLETED/PROCESSING
    # moments later on the very same request. Function-local import: see
    # storage.py's identical note on why takedown_service isn't a module-level import.
    from app.services.takedown_service import apply_processing_status
    from app.services.takedown_service import stamp_completion_time

    apply_processing_status(media_file, status)

    # Set completed_at timestamp if status is COMPLETED or ERROR
    if status in [FileStatus.COMPLETED, FileStatus.ERROR]:
        stamp_completion_time(media_file, datetime.now(UTC))

    db.commit()
    db.refresh(media_file)
    return media_file  # type: ignore[no-any-return]


def update_media_file_from_task_status(db: Session, file_id: int) -> MediaFile | None:
    """Check task statuses and update media file status accordingly.

    Uses a single SQL query with conditional COUNT to determine the correct
    file status instead of loading every Task object into Python.

    Args:
        db: Database session
        file_id: ID of the media file to check

    Returns:
        Updated MediaFile object or None if not found
    """
    from sqlalchemy import func as sa_func

    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file:
        logger.warning(f"Media file {file_id} not found")
        return None

    # Skip if the file is already in a terminal state
    if media_file.status in [FileStatus.COMPLETED, FileStatus.ERROR]:
        return media_file  # type: ignore[no-any-return]

    # Single SQL query: conditional counts per status
    row = (
        db.query(
            sa_func.count().label("total"),
            sa_func.count().filter(Task.status == TASK_STATUS_PENDING).label("pending"),
            sa_func.count().filter(Task.status == TASK_STATUS_IN_PROGRESS).label("in_progress"),
            sa_func.count().filter(Task.status == TASK_STATUS_COMPLETED).label("completed"),
            sa_func.count().filter(Task.status == TASK_STATUS_FAILED).label("failed"),
        )
        .filter(Task.media_file_id == file_id)
        .first()
    )

    if not row or row.total == 0:
        logger.warning(f"No tasks found for media file {file_id}")
        return media_file  # type: ignore[no-any-return]

    # Determine the appropriate media file status
    if row.pending > 0 or row.in_progress > 0:
        new_status = FileStatus.PROCESSING
    elif row.failed > 0 and row.completed == 0:
        new_status = FileStatus.ERROR
    else:
        new_status = FileStatus.COMPLETED

    # Only update if status is changing
    if media_file.status != new_status:
        return update_media_file_status(db, file_id, new_status)

    return media_file  # type: ignore[no-any-return]


def get_task_summary_for_media_file(db: Session, file_id: int) -> dict[str, Any]:
    """Get a summary of task statuses for a media file.

    Uses a single SQL query with conditional COUNT and AVG instead of
    loading every Task row into Python.

    Args:
        db: Database session
        file_id: ID of the media file to check

    Returns:
        Dictionary with task status counts and overall progress
    """
    from sqlalchemy import func as sa_func

    row = (
        db.query(
            sa_func.count().label("total"),
            sa_func.count().filter(Task.status == TASK_STATUS_PENDING).label("pending"),
            sa_func.count().filter(Task.status == TASK_STATUS_IN_PROGRESS).label("in_progress"),
            sa_func.count().filter(Task.status == TASK_STATUS_COMPLETED).label("completed"),
            sa_func.count().filter(Task.status == TASK_STATUS_FAILED).label("failed"),
            sa_func.coalesce(sa_func.avg(Task.progress), 0.0).label("avg_progress"),
        )
        .filter(Task.media_file_id == file_id)
        .first()
    )

    if not row or row.total == 0:
        return {
            "total": 0,
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "overall_progress": 0.0,
        }

    return {
        "total": row.total,
        "pending": row.pending,
        "in_progress": row.in_progress,
        "completed": row.completed,
        "failed": row.failed,
        "overall_progress": float(row.avg_progress),
    }


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
    if os.environ.get("SKIP_CELERY", "False").lower() == "true":
        return
    try:
        from app.core.constants import CeleryQueues
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


def check_for_stuck_files(db: Session, stuck_threshold_hours: float = 2.0) -> list[int]:
    """Find files that appear to be stuck in processing or pending.

    Uses a single SQL query with OR conditions instead of 4 separate queries,
    and loads only the columns needed for the Celery status check.

    Args:
        db: Database session
        stuck_threshold_hours: Hours (float) after which a file is considered stuck

    Returns:
        List of file IDs that appear stuck
    """
    from sqlalchemy.orm import load_only

    threshold_time = datetime.now(UTC) - timedelta(hours=stuck_threshold_hours)

    # Single query: all candidate stuck files (processing, pending, error, orphaned)
    candidates = (
        db.query(MediaFile)
        .options(
            load_only(
                MediaFile.id,  # type: ignore[arg-type]
                MediaFile.status,  # type: ignore[arg-type]
                MediaFile.active_task_id,  # type: ignore[arg-type]
                MediaFile.retry_count,  # type: ignore[arg-type]
            )
        )
        .filter(
            or_(
                # Stuck processing files
                (MediaFile.status == FileStatus.PROCESSING)
                & or_(
                    MediaFile.task_last_update < threshold_time,
                    MediaFile.task_last_update.is_(None),
                ),
                # Stuck pending files
                (MediaFile.status == FileStatus.PENDING) & (MediaFile.upload_time < threshold_time),
                # Failed files (filter retryable ones in Python — needs system settings)
                MediaFile.status == FileStatus.ERROR,
                # Orphaned files
                MediaFile.status == FileStatus.ORPHANED,
            )
        )
        .all()
    )

    stuck_file_ids: list[int] = []
    for file in candidates:
        # For failed files, check retry eligibility
        if file.status == FileStatus.ERROR and not system_settings_service.should_retry_file(
            db, int(file.retry_count or 0)
        ):
            continue

        # Double-check by querying Celery task status if we have an active task ID
        active_task_id = file.active_task_id
        if active_task_id:
            try:
                task_result = AsyncResult(str(active_task_id))
                if task_result.state in ["FAILURE", "REVOKED", "RETRY"]:
                    stuck_file_ids.append(int(file.id))
                elif task_result.state == "SUCCESS":
                    # Task completed but file status wasn't updated
                    file.status = FileStatus.COMPLETED  # type: ignore[assignment]
                    file.active_task_id = None  # type: ignore[assignment]
                    file.task_started_at = None  # type: ignore[assignment]
                    db.commit()
            except Exception as e:
                logger.warning(f"Could not check task status for {active_task_id}: {e}")
                stuck_file_ids.append(int(file.id))
        else:
            stuck_file_ids.append(int(file.id))

    return stuck_file_ids


def _start_transcription_task(file_uuid: str, file_id: int, task_description: str) -> None:
    """Start a new transcription task for a file.

    Args:
        file_uuid: UUID of the media file
        file_id: ID of the media file (for logging)
        task_description: Description of the task type (e.g., "pending", "failed", "orphaned")
    """
    import os

    if os.environ.get("SKIP_CELERY", "False").lower() != "true":
        from app.tasks.transcription import dispatch_transcription_pipeline

        task_id = dispatch_transcription_pipeline(file_uuid=file_uuid)
        logger.info(f"Started recovery task {task_id} for {task_description} file {file_id}")


def _recover_pending_file(db: Session, media_file: MediaFile) -> bool:
    """Recover a file stuck in pending status.

    Args:
        db: Database session
        media_file: The media file to recover

    Returns:
        True if recovery was successful
    """
    logger.info(f"File {media_file.id} stuck in pending, restarting processing")
    _start_transcription_task(str(media_file.uuid), int(media_file.id), "pending")
    return True


def _recover_failed_file(db: Session, media_file: MediaFile) -> bool:
    """Recover a file in error status that can be retried.

    Args:
        db: Database session
        media_file: The media file to recover

    Returns:
        True if recovery was successful, False otherwise
    """
    logger.info(
        f"File {media_file.id} failed, attempting retry (attempt {int(media_file.retry_count or 0) + 1})"
    )

    success = reset_file_for_retry(db, int(media_file.id), reset_retry_count=False)
    if success:
        _start_transcription_task(str(media_file.uuid), int(media_file.id), "failed")
        return True
    return False


def _recover_orphaned_file(db: Session, media_file: MediaFile) -> bool:
    """Recover an orphaned file.

    Args:
        db: Database session
        media_file: The media file to recover

    Returns:
        True if recovery was successful
    """
    logger.info(f"File {media_file.id} orphaned, attempting recovery restart")

    # Reset status to pending and try again
    media_file.status = FileStatus.PENDING  # type: ignore[assignment]
    media_file.active_task_id = None  # type: ignore[assignment]
    media_file.task_started_at = None  # type: ignore[assignment]
    db.commit()

    _start_transcription_task(str(media_file.uuid), int(media_file.id), "orphaned")
    return True


def _update_recovery_tracking(db: Session, media_file: MediaFile) -> None:
    """Update recovery tracking fields for a file.

    Args:
        db: Database session
        media_file: The media file to update
    """
    # `recovery_attempts` is a nullable Integer whose 0 is a PYTHON-side default, so any
    # row written by raw SQL, a migration backfill or an explicit UPDATE holds NULL and a
    # bare `+= 1` is a TypeError. Normalize on read the way every other counter read here does.
    media_file.recovery_attempts = int(media_file.recovery_attempts or 0) + 1
    media_file.last_recovery_attempt = datetime.now(UTC)  # type: ignore[assignment]
    media_file.active_task_id = None  # type: ignore[assignment]
    media_file.task_started_at = None  # type: ignore[assignment]
    db.commit()


def recover_stuck_file(db: Session, file_id: int) -> bool:
    """Attempt to recover a stuck file.

    Args:
        db: Database session
        file_id: ID of the stuck file

    Returns:
        True if recovery was successful, False otherwise
    """
    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file:
        return False

    try:
        # Cancel any active task
        if media_file.active_task_id:
            cancel_active_task(db, file_id)

        # Handle different file statuses
        if media_file.status == FileStatus.PENDING:
            return _recover_pending_file(db, media_file)

        if media_file.status == FileStatus.ERROR and system_settings_service.should_retry_file(
            db, int(media_file.retry_count or 0)
        ):
            return _recover_failed_file(db, media_file)

        if media_file.status == FileStatus.ORPHANED:
            return _recover_orphaned_file(db, media_file)

        # Handle files with transcript data. Routed through the same takedown-aware
        # helpers as the two pipeline writers above for consistency -- not reachable
        # for a quarantined row today (this branch only runs for a PROCESSING file
        # with no live task), but a direct `media_file.status =` here would be the
        # same footgun issue #824 was about if that ever changes.
        if media_file.transcript_segments:
            from app.services.takedown_service import apply_processing_status
            from app.services.takedown_service import stamp_completion_time

            apply_processing_status(media_file, FileStatus.COMPLETED)
            stamp_completion_time(media_file, datetime.now(UTC))
        else:
            # No transcript data, mark as orphaned for potential retry
            media_file.status = FileStatus.ORPHANED
            media_file.force_delete_eligible = True

        # Update recovery tracking
        _update_recovery_tracking(db, media_file)
        logger.info(f"Recovered stuck file {file_id}, new status: {media_file.status}")
        return True

    except Exception as e:
        logger.error(f"Failed to recover stuck file {file_id}: {e}", exc_info=True)
        # Recovery branches above COMMIT before dispatching, so landing here means
        # the file was already changed and the restart never happened. Record the
        # ATTEMPT so a repeatedly-failing file shows up in recovery_attempts instead
        # of looking untried. Status/last_error_message are written by
        # dispatch_transcription_pipeline itself when dispatch was the failure (#906).
        with contextlib.suppress(Exception):
            db.rollback()
            failed_file = get_refreshed_object(db, MediaFile, file_id)
            if failed_file is not None:
                _update_recovery_tracking(db, failed_file)
        return False


def is_file_safe_to_delete(db: Session, file_id: int) -> tuple[bool, str]:
    """Check if a file is safe to delete (no active processing).

    Args:
        db: Database session
        file_id: ID of the file to check

    Returns:
        Tuple of (is_safe, reason)
    """
    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file:
        return False, "File not found"

    # Check if file has an active task. Deliberately NOT gated on
    # `media_file.status == FileStatus.PROCESSING` — a selective reprocess of an
    # already-completed file (search_indexing/analytics/speaker_llm/summarization/
    # topic_extraction/speaker_clustering) sets `active_task_id` via
    # `create_task_record`, but that function only flips status to PROCESSING when
    # the file was previously PENDING, so an in-flight downstream stage on a
    # completed file leaves status="completed" with a genuinely live task. Requiring
    # both conditions let `purge_media_file` run concurrently with that task, which
    # could delete the file (and its OpenSearch chunks, via a delete-by-query that
    # finds nothing yet) moments before the task's own async write landed — leaving
    # a permanently orphaned chunk with no MediaFile or Speaker row left to clean it
    # up. `active_task_id` alone is enough to warrant the Celery check below; the
    # check itself is what actually confirms liveness, so this only adds coverage,
    # it never skips a check that used to run.
    active_task_id = media_file.active_task_id
    if active_task_id:
        # Double-check with Celery
        try:
            task_result = AsyncResult(str(active_task_id))
            if task_result.state in ["PENDING", "STARTED", "RETRY"]:
                return (
                    False,
                    f"File is currently being processed (task: {active_task_id})",
                )
        except Exception as e:
            # If we can't check task status, assume it's safe
            logger.warning(
                f"Could not check Celery task status for {active_task_id}: {e}. "
                "Assuming file is safe to delete."
            )

    # Safe to delete in these states
    safe_states = [
        FileStatus.COMPLETED,
        FileStatus.ERROR,
        FileStatus.CANCELLED,
        FileStatus.ORPHANED,
    ]
    if media_file.status in safe_states:
        return True, "File is not actively processing"

    # Pending files might be picked up soon
    if media_file.status == FileStatus.PENDING:
        return True, "File is pending but not yet processing"

    return False, f"File status is {media_file.status}"


#: SQLSTATE 40P01 (deadlock_detected) — the standard Postgres code, checked instead of a
#: driver-specific exception class so this survives a DB-driver swap. A deadlock is a
#: transient, order-of-execution artifact (Postgres picks a victim transaction to abort so
#: the others can proceed) — retrying it is the correct response, not a workaround.
_DEADLOCK_SQLSTATE = "40P01"
_DEADLOCK_RETRY_ATTEMPTS = 3
_DEADLOCK_RETRY_DELAY_S = 0.5


def media_object_is_resolvable(media_file: MediaFile) -> bool:
    """Whether this row's media actually resolves to an object in storage.

    Fails **closed**: only a positive "the object is there" answers True. The
    caller uses this to decide whether deleting a transcript is recoverable, and
    the two error directions are not symmetric — reading a storage outage as
    "present" destroys the transcript, reading it as "absent" merely refuses a
    reprocess the user can repeat once storage is back.

    Args:
        media_file: The file whose ``storage_path`` should be resolved.

    Returns:
        True only when the object was confirmed present.
    """
    storage_path = media_file.storage_path
    if not storage_path:
        # The repo's own "this row has no media" convention (four writers, eight
        # readers) — there is nothing to look up.
        return False

    if os.environ.get("SKIP_S3", "False").lower() == "true":
        # No object storage is in play in this deployment mode, so there is no
        # absence to confirm and nothing this check could learn. Same switch the
        # upload/streaming/thumbnail paths already read.
        return True

    from app.services.minio_service import object_exists_and_size

    try:
        return object_exists_and_size(str(storage_path)) is not None
    except Exception as e:
        # object_exists_and_size only returns None for a genuine "no such key";
        # everything else (outage, credentials, network) raises, and none of
        # those is evidence the object is gone.
        logger.warning(
            f"Could not confirm media object {storage_path!r} for file {media_file.id}: "
            f"{type(e).__name__}: {e}. Treating it as unresolvable."
        )
        return False


def transcript_is_regenerable(db: Session, media_file: MediaFile) -> bool:
    """Whether re-running the pipeline could reproduce this file's transcript.

    False only when the file **has** transcript segments and its media object
    cannot be found — the state in which clearing the transcript destroys the
    only copy (issue #872). A file with no segments has nothing to lose, so it
    stays retryable: rows created by the upload, URL-ingestion, media-download
    and watch-source paths legitimately carry no storage object yet, and
    retrying one is how the download re-runs.

    Args:
        db: Database session.
        media_file: The file about to have its transcript cleared.

    Returns:
        True when clearing the transcript is safe.
    """
    from app.models.media import TranscriptSegment

    has_segments = (
        db.query(TranscriptSegment.id)
        .filter(TranscriptSegment.media_file_id == media_file.id)
        .first()
        is not None
    )
    if not has_segments:
        return True

    return media_object_is_resolvable(media_file)


def reset_file_for_retry(db: Session, file_id: int, reset_retry_count: bool = False) -> bool:
    """Reset a file for retry processing.

    Args:
        db: Database session
        file_id: ID of the file to reset
        reset_retry_count: Whether to reset the retry count to 0

    Returns:
        True if reset was successful, False otherwise
    """
    for attempt in range(1, _DEADLOCK_RETRY_ATTEMPTS + 1):
        media_file = get_refreshed_object(db, MediaFile, file_id)
        if not media_file:
            return False

        # Issue #872: the deletes below are unconditional and COMMIT, so a file
        # whose media cannot be found loses its only copy of the transcript with
        # nothing left to regenerate it from. The refusal lives here rather than
        # at the callers because every reprocess/retry/recovery entry point
        # reaches this one function.
        if not transcript_is_regenerable(db, media_file):
            logger.error(
                f"Refusing to reset file {file_id} for retry: it has transcript segments "
                f"and its media object ({media_file.storage_path!r}) could not be resolved "
                "in storage, so re-running the pipeline could not reproduce them."
            )
            return False

        try:
            # Cancel any active task first
            if media_file.active_task_id:
                cancel_active_task(db, file_id)

            # Reset retry count if requested. `retry_count` is nullable with a Python-side
            # default of 0, so a NULL row must be normalized rather than incremented in
            # place — this is the manual-retry entry point (POST /my-files/{uuid}/retry),
            # and a bare `+= 1` here turns a NULL row into a 500 the user cannot get past.
            if reset_retry_count:
                media_file.retry_count = 0
            else:
                media_file.retry_count = int(media_file.retry_count or 0) + 1

            # Reset file state
            media_file.status = FileStatus.PENDING
            media_file.active_task_id = None
            media_file.task_started_at = None
            media_file.task_last_update = None
            media_file.cancellation_requested = False
            media_file.last_error_message = None
            # A retry that clears the message but keeps the retry-policy classification
            # would strand a `worker_lost` category on a freshly-PENDING row.
            media_file.error_category = None
            media_file.force_delete_eligible = False

            # Clear existing transcript data for clean retry
            from app.models.media import Analytics
            from app.models.media import Speaker
            from app.models.media import TranscriptSegment

            # Delete existing transcript segments
            db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == file_id).delete()

            # Delete existing speakers
            db.query(Speaker).filter(Speaker.media_file_id == file_id).delete()

            # Delete existing analytics
            db.query(Analytics).filter(Analytics.media_file_id == file_id).delete()

            # Clear related task records for clean slate
            db.query(Task).filter(Task.media_file_id == file_id).delete()

            # Reset summary fields so auto-summary triggers correctly after transcription retry
            media_file.summary_data = None
            media_file.summary_opensearch_id = None
            media_file.summary_status = "pending"

            db.commit()
            logger.info(f"Reset file {file_id} for retry (attempt {media_file.retry_count})")
            return True

        except OperationalError as e:
            db.rollback()
            is_deadlock = getattr(getattr(e, "orig", None), "pgcode", None) == _DEADLOCK_SQLSTATE
            if is_deadlock and attempt < _DEADLOCK_RETRY_ATTEMPTS:
                logger.warning(
                    f"Deadlock resetting file {file_id} for retry "
                    f"(attempt {attempt}/{_DEADLOCK_RETRY_ATTEMPTS}); retrying"
                )
                time.sleep(_DEADLOCK_RETRY_DELAY_S)
                continue
            logger.error(f"Failed to reset file {file_id} for retry: {e}")
            return False

        except Exception as e:
            logger.error(f"Failed to reset file {file_id} for retry: {e}")
            db.rollback()
            return False

    return False
