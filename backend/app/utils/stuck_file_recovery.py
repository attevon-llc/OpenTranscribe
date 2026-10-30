"""Detection and recovery of media files stuck in processing, pending, error or orphaned."""

import contextlib
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from celery.result import AsyncResult
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.env import env_bool
from app.db.session_utils import get_refreshed_object
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services import system_settings_service
from app.utils.file_retry import reset_file_for_retry
from app.utils.task_cancel import cancel_active_task

logger = logging.getLogger(__name__)


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

    if not env_bool("SKIP_CELERY"):
        from app.tasks.transcription import dispatch_transcription_pipeline

        task_id = dispatch_transcription_pipeline(file_uuid=file_uuid, reuse_requested_options=True)
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
