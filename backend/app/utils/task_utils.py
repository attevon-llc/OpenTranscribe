import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from celery.result import AsyncResult
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.celery import celery_app
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


def create_task_record(
    db: Session, celery_task_id: str, user_id: int, media_file_id: int, task_type: str
) -> Task:
    """Create a new task record in the database."""
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

    # Update the media file with active task tracking
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

            # Clear active task if completed or failed
            if status in [TASK_STATUS_COMPLETED, TASK_STATUS_FAILED]:
                media_file.active_task_id = None
                media_file.task_started_at = None

    db.commit()
    db.refresh(task)

    # If task is completed or failed, check if we need to update the media file status
    task_media_file_id = task.media_file_id
    if status in [TASK_STATUS_COMPLETED, TASK_STATUS_FAILED] and task_media_file_id:
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

    # Update status and timestamps
    media_file.status = status

    # Set completed_at timestamp if status is COMPLETED or ERROR
    if status in [FileStatus.COMPLETED, FileStatus.ERROR] and not media_file.completed_at:
        media_file.completed_at = datetime.now(UTC)

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


def cancel_active_task(db: Session, file_id: int) -> bool:
    """Cancel the active task for a media file.

    Args:
        db: Database session
        file_id: ID of the media file

    Returns:
        True if task was cancelled, False otherwise
    """
    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file or not media_file.active_task_id:
        return False

    try:
        # Revoke the Celery task
        celery_app.control.revoke(media_file.active_task_id, terminate=True)

        # Update task status in database
        task = db.query(Task).filter(Task.id == media_file.active_task_id).first()
        if task:
            task.status = TASK_STATUS_FAILED  # type: ignore[assignment]
            task.error_message = "Task cancelled by user"  # type: ignore[assignment]
            task.completed_at = datetime.now(UTC)  # type: ignore[assignment]

        # Update media file status
        media_file.status = FileStatus.CANCELLED
        media_file.active_task_id = None
        media_file.task_started_at = None
        media_file.cancellation_requested = False

        db.commit()
        logger.info(f"Successfully cancelled task for file {file_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to cancel task for file {file_id}: {e}")
        return False


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

        # Handle files with transcript data
        if media_file.transcript_segments:
            media_file.status = FileStatus.COMPLETED
            media_file.completed_at = datetime.now(UTC)
        else:
            # No transcript data, mark as orphaned for potential retry
            media_file.status = FileStatus.ORPHANED
            media_file.force_delete_eligible = True

        # Update recovery tracking
        _update_recovery_tracking(db, media_file)
        logger.info(f"Recovered stuck file {file_id}, new status: {media_file.status}")
        return True

    except Exception as e:
        logger.error(f"Failed to recover stuck file {file_id}: {e}")
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


def reset_file_for_retry(db: Session, file_id: int, reset_retry_count: bool = False) -> bool:
    """Reset a file for retry processing.

    Args:
        db: Database session
        file_id: ID of the file to reset
        reset_retry_count: Whether to reset the retry count to 0

    Returns:
        True if reset was successful, False otherwise
    """
    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file:
        return False

    try:
        # Cancel any active task first
        if media_file.active_task_id:
            cancel_active_task(db, file_id)

        # Reset retry count if requested. `retry_count` is nullable with a Python-side
        # default of 0, so a NULL row must be normalized rather than incremented in place —
        # this is the manual-retry entry point (POST /my-files/{uuid}/retry), and a bare
        # `+= 1` here turns a NULL row into a 500 the user cannot get past.
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

    except Exception as e:
        logger.error(f"Failed to reset file {file_id} for retry: {e}")
        db.rollback()
        return False
