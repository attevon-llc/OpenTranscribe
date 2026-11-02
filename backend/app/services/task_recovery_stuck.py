"""Stuck-task and inconsistent-file recovery (mixin of ``TaskRecoveryService``).

Split out of ``task_recovery_service``.
"""

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.task_config import RECOVERED_AFTER_STUCK
from app.core.task_config import RECOVERY_ATTEMPT_MARKER
from app.core.task_liveness import TRANSCRIPTION_TASK_TYPE
from app.core.task_liveness import RunState
from app.core.task_liveness import probe_runs
from app.core.task_liveness import supersede_run
from app.db.session_utils import get_refreshed_object
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services import system_settings_service
from app.services.task_recovery_retry import TaskRecoveryRetryMixin
from app.utils.error_classification import get_retry_delay
from app.utils.error_classification import should_retry
from app.utils.error_classification import stored_category
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

logger = logging.getLogger(__name__)


def _recovery_attempts(error_message: str | None) -> int:
    """The Step 5.7 reset counter recorded in a task's error message, or 0."""
    if not error_message or RECOVERY_ATTEMPT_MARKER not in error_message:
        return 0
    try:
        return int(error_message.split(RECOVERY_ATTEMPT_MARKER)[1].split("_")[0].rstrip(")"))
    except (ValueError, IndexError):
        return 0


def _retire_if_transcription(task: Task) -> None:
    """Stop a transcription run that recovery has just failed (#1020).

    Without this the original message -- still waiting in the broker, or redelivered after a
    worker crash -- ran later beside the replacement recovery dispatches.
    """
    if task.task_type == TRANSCRIPTION_TASK_TYPE:
        media_file = task.media_file if task.media_file_id is not None else None
        supersede_run(str(task.id), str(media_file.uuid) if media_file is not None else "")


class TaskRecoveryStuckMixin(TaskRecoveryRetryMixin):
    """Recovery of stuck tasks and of files with inconsistent or missing tasks.

    Builds on ``TaskRecoveryRetryMixin``: a recovery that gives up on a stuck run hands
    the file to ``schedule_file_retry``."""

    def recover_stuck_task(self, db: Session, task: Task, requeue: bool = True) -> bool:
        """
        Attempt to recover a stuck task.

        Args:
            db: Database session
            task: The stuck task to recover
            requeue: For a transcription, apply the retry policy (requeue the file, or fail it
                once its budget is spent). False only for a caller that dispatches the
                replacement itself (the admin recover endpoints); the run is then just
                retired.

        Returns:
            bool: True if recovery was successful, False otherwise
        """
        try:
            logger.info(
                f"Recovering stuck task {task.id} of type {task.task_type} "
                f"for media file {task.media_file_id}"
            )

            if requeue and task.task_type == TRANSCRIPTION_TASK_TYPE:
                return self._recover_stuck_transcription(db, task)

            # Carry forward a Step 5.7 reset counter: overwriting it is what made the
            # stuck -> reset -> stuck cycle repeat forever (#1020).
            attempts = _recovery_attempts(task.error_message)
            message = RECOVERED_AFTER_STUCK
            if attempts:
                message = f"{message} ({RECOVERY_ATTEMPT_MARKER}{attempts})"
            _retire_if_transcription(task)
            update_task_status(
                db=db,
                task_id=str(task.id),
                status="failed",
                error_message=message,
                completed=True,
            )

            # Check if we need to update the media file status
            assert task.media_file_id is not None  # media-processing task always has a file
            media_file = get_refreshed_object(db, MediaFile, int(task.media_file_id))
            if not media_file:
                logger.error(f"Media file {task.media_file_id} not found for task {task.id}")
                return False

            # Update media file status if no other active tasks
            self._update_media_file_if_no_active_tasks(db, media_file)

            return True

        except Exception as e:
            logger.error(f"Error recovering stuck task {task.id}: {str(e)}")
            return False

    @staticmethod
    def _recover_stuck_transcription(db: Session, task: Task) -> bool:
        """Requeue (or, past its budget, fail) a transcription the detector found stuck.

        Never the generic path below: failing the Task row there ran the file-status
        aggregate, which turned the file ERROR before the retry branch could see it was still
        PROCESSING -- every file whose worker died ended in ERROR with no retry.
        """
        from app.services.transcription_retry import recover_lost_run
        from app.services.transcription_retry import recover_overrun_run

        # Re-read the lease now: the broker reaper may have put the stage back on its queue
        # (QUEUED) since detection, and an unreadable Redis means "conclude nothing".
        run = probe_runs([str(task.id)]).get(str(task.id))
        state = run.state if run is not None else RunState.UNKNOWN
        if state == RunState.RUNNING:
            outcome = recover_overrun_run(db, task)  # alive, but past its duration budget
        elif state == RunState.DEAD:
            outcome = recover_lost_run(db, task)  # declines a run first in line on its queue
        else:
            return False
        return outcome is not None

    def fix_inconsistent_media_file(self, db: Session, media_file: MediaFile) -> bool:
        """
        Fix inconsistent media file state.

        Args:
            db: Database session
            media_file: The media file with inconsistent state

        Returns:
            bool: True if fix was successful, False otherwise
        """
        try:
            logger.info(f"Fixing inconsistent state for media file {media_file.id}")

            # Get all tasks for this media file
            tasks = db.query(Task).filter(Task.media_file_id == media_file.id).all()

            if not tasks:
                return self._handle_file_with_no_tasks(db, media_file)

            # Analyze task statuses and update file accordingly
            return self._update_file_based_on_tasks(db, media_file, tasks)

        except Exception as e:
            logger.error(f"Error fixing inconsistent media file {media_file.id}: {str(e)}")
            return False

    def recover_orphaned_tasks(self, db: Session, orphaned_tasks: list[Task]) -> int:
        """
        Recover multiple orphaned tasks.

        Args:
            db: Database session
            orphaned_tasks: List of orphaned tasks

        Returns:
            int: Number of successfully recovered tasks
        """
        recovered_count = 0

        for task in orphaned_tasks:
            try:
                logger.info(f"Marking orphaned task {task.id} as failed")

                _retire_if_transcription(task)
                task.status = "failed"  # type: ignore[assignment]
                task.error_message = "Task interrupted by system restart"  # type: ignore[assignment]
                task.completed_at = datetime.now(UTC)  # type: ignore[assignment]
                task.updated_at = datetime.now(UTC)  # type: ignore[assignment]

                recovered_count += 1

            except Exception as e:
                logger.error(f"Error recovering orphaned task {task.id}: {e}")

        try:
            db.commit()
        except Exception as e:
            logger.error(f"Error committing orphaned task recovery: {e}")
            db.rollback()
            return 0

        return recovered_count

    def reset_abandoned_files(
        self, db: Session, abandoned_files: list[MediaFile]
    ) -> dict[str, int]:
        """
        Reset abandoned files intelligently based on their actual progress.

        For files stuck in PROCESSING:
        - If transcription completed: Mark as COMPLETED (no segment deletion)
        - If transcription incomplete: Reset to PENDING and delete segments

        Args:
            db: Database session
            abandoned_files: List of abandoned files

        Returns:
            Dict with statistics: files_reset, files_completed
        """
        stats = {"files_reset": 0, "files_completed": 0}

        for media_file in abandoned_files:
            try:
                # Check if transcription actually completed
                if self._is_transcription_complete(db, int(media_file.id)):
                    # Transcription completed - mark as COMPLETED instead of resetting
                    logger.info(
                        f"Abandoned file {media_file.id} has completed transcription - "
                        f"marking as COMPLETED: {media_file.filename}"
                    )
                    update_media_file_status(db, int(media_file.id), FileStatus.COMPLETED)
                    stats["files_completed"] += 1
                else:
                    # Transcription incomplete - reset and delete segments for clean retry
                    logger.info(
                        f"Resetting abandoned file {media_file.id} with incomplete transcription: "
                        f"{media_file.filename}"
                    )

                    # Delete existing transcript segments to prevent duplicates on retry
                    self._cleanup_transcript_segments(db, int(media_file.id))

                    update_media_file_status(db, int(media_file.id), FileStatus.PENDING)
                    stats["files_reset"] += 1

            except Exception as e:
                logger.error(f"Error handling abandoned file {media_file.id}: {e}")
                db.rollback()

        return stats

    def recover_stuck_files_without_celery_tasks(
        self, db: Session, stuck_files: list[MediaFile]
    ) -> dict[str, int]:
        """
        Recover files that are stuck in processing state without active Celery tasks.

        Args:
            db: Database session
            stuck_files: List of stuck files identified by detection service

        Returns:
            Dict with recovery statistics
        """
        stats = {"files_recovered": 0, "tasks_retried": 0, "tasks_failed": 0}

        for media_file in stuck_files:
            try:
                # The category the failure site stored — never re-derived from prose (#959)
                error_category = stored_category(media_file.error_category)
                media_file.error_category = error_category.value  # type: ignore[assignment]

                # Enforce retry delay based on error category
                if media_file.completed_at:
                    time_since_failure = (
                        datetime.now(UTC) - media_file.completed_at
                    ).total_seconds()
                    required_delay = get_retry_delay(
                        error_category, int(media_file.retry_count or 0)
                    )
                    if time_since_failure < required_delay:
                        logger.debug(
                            f"Skipping file {media_file.id} - retry delay not met "
                            f"({time_since_failure:.0f}s < {required_delay}s)"
                        )
                        db.commit()
                        continue

                # Check if retry is allowed based on error classification
                if not should_retry(error_category, int(media_file.retry_count or 0)):
                    logger.info(
                        f"Skipping retry for file {media_file.id} - "
                        f"error category {error_category.value}, "
                        f"retry count: {media_file.retry_count}"
                    )
                    update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                    db.commit()
                    continue

                # Also check system-level retry settings
                if not system_settings_service.should_retry_file(
                    db, int(media_file.retry_count or 0)
                ):
                    logger.info(
                        f"Skipping retry for file {media_file.id} - system retry limit reached "
                        f"(count: {media_file.retry_count})"
                    )
                    update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                    db.commit()
                    continue

                logger.info(
                    f"Recovering stuck file {media_file.id} ({media_file.filename}) "
                    f"- error: {error_category.value}"
                )

                # Mark any existing tasks as failed
                stale_tasks = (
                    db.query(Task)
                    .filter(
                        Task.media_file_id == media_file.id,
                        Task.status.in_(["pending", "in_progress"]),
                    )
                    .all()
                )

                for task in stale_tasks:
                    _retire_if_transcription(task)
                    update_task_status(
                        db=db,
                        task_id=str(task.id),
                        status="failed",
                        error_message="Task recovered - no active Celery worker found",
                        completed=True,
                    )
                    stats["tasks_failed"] += 1

                # Increment retry count and reset file status to pending for retry.
                # NULL-normalized: `retry_count`'s 0 is a Python-side default, not a
                # constraint, so `+= 1` on a NULL row raises TypeError.
                media_file.retry_count = int(media_file.retry_count or 0) + 1
                update_media_file_status(db, int(media_file.id), FileStatus.PENDING)
                stats["files_recovered"] += 1

                # Schedule new transcription task
                if self.schedule_file_retry(int(media_file.id)):
                    stats["tasks_retried"] += 1
                    logger.info(f"Successfully scheduled retry for stuck file {media_file.id}")
                else:
                    logger.error(f"Failed to schedule retry for stuck file {media_file.id}")

            except Exception as e:
                logger.error(f"Error recovering stuck file {media_file.id}: {e}")

        return stats

    def _update_media_file_if_no_active_tasks(self, db: Session, media_file: MediaFile):
        """Update media file status if no active tasks remain."""
        active_tasks = (
            db.query(Task)
            .filter(
                Task.media_file_id == media_file.id,
                Task.status.in_(["pending", "in_progress"]),
            )
            .count()
        )

        if active_tasks == 0 and media_file.status == FileStatus.PROCESSING:
            # Decide from the category the failure site stored — never from prose (#959)
            error_category = stored_category(media_file.error_category)
            media_file.error_category = error_category.value  # type: ignore[assignment]

            if should_retry(error_category, int(media_file.retry_count or 0)):
                logger.info(
                    f"Retriable error for file {media_file.id} "
                    f"({error_category.value}), resetting to PENDING"
                )
                # NULL-normalized, matching the `retry_count or 0` read in the
                # `should_retry` call that gates this branch.
                media_file.retry_count = int(media_file.retry_count or 0) + 1
                update_media_file_status(db, int(media_file.id), FileStatus.PENDING)
                if not self.schedule_file_retry(int(media_file.id)):
                    logger.warning(
                        f"Retry dispatch failed for file {media_file.id}, reverting to ERROR"
                    )
                    update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                    media_file.last_error_message = (
                        "Retry dispatch failed - Celery may be unavailable"  # type: ignore[assignment]
                    )
                    db.commit()
            else:
                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                db.commit()
                logger.info(
                    f"Updated media file {media_file.id} status to ERROR "
                    f"after task recovery ({error_category.value})"
                )

    def _handle_file_with_no_tasks(self, db: Session, media_file: MediaFile) -> bool:
        """Handle media file that has no associated tasks."""
        if media_file.status == FileStatus.PROCESSING:
            update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
            logger.info(
                f"Media file {media_file.id} had no tasks but was PROCESSING - marked as ERROR"
            )
        return True

    def _update_file_based_on_tasks(
        self, db: Session, media_file: MediaFile, tasks: list[Task]
    ) -> bool:
        """Update file status based on its associated tasks."""
        from app.models.media import TranscriptSegment

        task_counts = {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
        for task in tasks:
            task_counts[str(task.status)] = task_counts.get(str(task.status), 0) + 1

        # Decision logic based on task statuses
        if task_counts["pending"] == 0 and task_counts["in_progress"] == 0:
            # No active tasks — check if file actually has transcript segments
            # before marking as COMPLETED (a completed download task alone
            # doesn't mean transcription succeeded)
            has_segments = (
                db.query(TranscriptSegment)
                .filter(TranscriptSegment.media_file_id == media_file.id)
                .limit(1)
                .count()
                > 0
            )

            if task_counts["completed"] > 0 and has_segments:
                # Has completed tasks AND transcript segments = truly complete
                update_media_file_status(db, int(media_file.id), FileStatus.COMPLETED)
                logger.info(
                    f"Media file {media_file.id} had completed tasks with segments - marked as COMPLETED"
                )
            elif task_counts["failed"] > 0:
                # Has failed tasks (and either no completed tasks or no segments)
                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                logger.info(f"Media file {media_file.id} has failed tasks - marked as ERROR")
            elif task_counts["completed"] > 0 and not has_segments:
                # Completed tasks but no segments = partial completion, needs retry
                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                logger.info(
                    f"Media file {media_file.id} had completed tasks but no transcript "
                    f"segments - marked as ERROR for retry"
                )
        elif media_file.status == FileStatus.PENDING and (
            task_counts["in_progress"] > 0 or task_counts["completed"] > 0
        ):
            # Tasks are running but file still shows pending
            update_media_file_status(db, int(media_file.id), FileStatus.PROCESSING)
            logger.info(
                f"Media file {media_file.id} had active tasks but was PENDING - marked as PROCESSING"
            )

        return True
