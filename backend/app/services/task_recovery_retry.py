"""Retry scheduling and error-file recovery (mixin of ``TaskRecoveryService``).

Split out of ``task_recovery_service``.
"""

import logging
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.enums import TranscriptionStage
from app.core.task_config import TaskRecoveryConfig
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services import system_settings_service
from app.utils.task_utils import update_media_file_status

logger = logging.getLogger(__name__)


class TaskRecoveryRetryMixin:
    """Retry scheduling and recovery of user-visible error / download / OOM files."""

    config: TaskRecoveryConfig

    def _cleanup_transcript_segments(self, db: Session, file_id: int) -> int:
        """
        Delete all transcript segments for a file before retry.

        This prevents duplicate segments when transcription is retried due to
        recovery, errors, or manual reprocessing.

        Args:
            db: Database session
            file_id: Media file ID

        Returns:
            Number of segments deleted
        """
        from app.models.media import TranscriptSegment

        segment_count: int = (
            db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == file_id).count()
        )

        if segment_count > 0:
            db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == file_id).delete(
                synchronize_session=False
            )
            logger.info(
                f"Deleted {segment_count} existing segments for file {file_id} before retry"
            )
            db.flush()

        return segment_count

    def _is_transcription_complete(self, db: Session, media_file_id: int) -> bool:
        """
        Check if transcription has actually completed for a file.

        A file is considered to have completed transcription if it has:
        1. Transcript segments in the database
        2. A completed_at timestamp set

        Args:
            db: Database session
            media_file_id: ID of the file to check

        Returns:
            True if transcription is complete, False otherwise
        """
        from app.models.media import TranscriptSegment

        segment_count = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file_id)
            .count()
        )

        if segment_count == 0:
            return False

        media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
        if not media_file or not media_file.completed_at:
            return False

        logger.info(
            f"File {media_file_id} has {segment_count} segments and completed_at={media_file.completed_at} - transcription is complete"
        )
        return True

    @staticmethod
    @contextmanager
    def _session_scope():
        """Provide a transactional scope around a series of operations."""
        from app.db.base import SessionLocal

        db = SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def schedule_file_retry(self, media_file_id: int, countdown: int | None = None) -> bool:
        """
        Schedule a retry for a file, dispatching the appropriate task type.

        For files that failed during YouTube download (have source_url but no
        storage_path), dispatches process_youtube_url_task. For files that have
        been downloaded, dispatches the 3-stage transcription pipeline at RETRY
        priority, so an automatically retried file runs ahead of submissions that
        arrived after it.

        Args:
            media_file_id: ID of the media file to retry
            countdown: Seconds to hold the retry before it starts (backoff)

        Returns:
            bool: True if retry was scheduled successfully
        """
        try:
            from app.models.media import MediaFile

            with self._session_scope() as db:
                media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
                if not media_file:
                    logger.error(f"File {media_file_id} not found for retry")
                    return False

                file_uuid = str(media_file.uuid)
                source_url = str(media_file.source_url) if media_file.source_url else None
                storage_path = str(media_file.storage_path) if media_file.storage_path else None
                user_id = int(media_file.user_id)

                # Dispatch task before closing session to prevent stale data
                if source_url and not storage_path:
                    from app.tasks.youtube_processing import process_youtube_url_task

                    yt_result = process_youtube_url_task.delay(
                        url=source_url,
                        user_id=user_id,
                        file_uuid=file_uuid,
                    )
                    task_id = yt_result.id if yt_result else None
                else:
                    from app.tasks.transcription import dispatch_transcription_pipeline

                    task_id = dispatch_transcription_pipeline(
                        file_uuid=file_uuid,
                        retry=True,
                        countdown=countdown,
                        reuse_requested_options=True,
                    )

            # Validate dispatch succeeded
            if not task_id:
                logger.error(f"Celery dispatch returned no task ID for file {media_file_id}")
                return False

            logger.info(f"Scheduled retry for file {media_file_id}, task ID: {task_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to schedule retry for file {media_file_id}: {e}")
            return False

    def recover_user_files(self, db: Session, problem_files: list[MediaFile]) -> dict[str, int]:
        """
        Recover problem files for users.

        This method intelligently handles file recovery based on actual progress:
        - If transcription completed but file stuck in PROCESSING: Mark as COMPLETED
        - If transcription incomplete: Reset to PENDING for full retry

        Args:
            db: Database session
            problem_files: List of problem files to recover

        Returns:
            Dict with recovery statistics
        """
        stats = {"files_recovered": 0, "tasks_retried": 0, "files_completed": 0}

        for media_file in problem_files:
            try:
                # Check if retry is allowed based on system settings
                if not system_settings_service.should_retry_file(
                    db, int(media_file.retry_count or 0)
                ):
                    logger.info(
                        f"Skipping retry for file {media_file.id} - retry limit reached "
                        f"(count: {media_file.retry_count})"
                    )
                    continue

                active_tasks = (
                    db.query(Task)
                    .filter(
                        Task.media_file_id == media_file.id,
                        Task.status.in_(["pending", "in_progress"]),
                    )
                    .count()
                )

                # `upload_time` is nullable: its server_default only applies to an INSERT
                # that omits the column, so a backfill or an explicit UPDATE can leave NULL.
                # An unmeasurable age is treated as "not yet old enough", which keeps the
                # PENDING-too-long branch below from firing on a row it knows nothing about.
                file_age = (
                    datetime.now(UTC) - media_file.upload_time
                    if media_file.upload_time is not None
                    else timedelta(0)
                )

                if active_tasks == 0 and media_file.status == FileStatus.PROCESSING:
                    # Check if transcription actually completed
                    if self._is_transcription_complete(db, int(media_file.id)):
                        # Transcription completed - mark as COMPLETED instead of resetting
                        logger.info(
                            f"File {media_file.id} stuck in PROCESSING but transcription complete - "
                            f"marking as COMPLETED"
                        )
                        update_media_file_status(db, int(media_file.id), FileStatus.COMPLETED)
                        stats["files_completed"] += 1
                        # Post-transcription tasks will be handled by periodic health check Step 6
                    else:
                        # Transcription incomplete - reset and retry
                        logger.info(
                            f"File {media_file.id} stuck in PROCESSING with incomplete transcription - "
                            f"resetting to PENDING"
                        )
                        media_file.retry_count = int(media_file.retry_count or 0) + 1
                        update_media_file_status(db, int(media_file.id), FileStatus.PENDING)
                        stats["files_recovered"] += 1

                        if self.schedule_file_retry(int(media_file.id)):
                            stats["tasks_retried"] += 1

                elif media_file.status == FileStatus.PENDING and file_age > timedelta(
                    hours=self.config.PENDING_FILE_RETRY_THRESHOLD
                ):
                    # File has been pending too long, retry it
                    media_file.retry_count = int(media_file.retry_count or 0) + 1
                    if self.schedule_file_retry(int(media_file.id)):
                        stats["tasks_retried"] += 1

            except Exception as e:
                logger.error(f"Error recovering file {media_file.id}: {e}")

        return stats

    def recover_stuck_downloading_files(
        self, db: Session, stuck_files: list[MediaFile]
    ) -> dict[str, int]:
        """
        Recover files stuck in DOWNLOADING status without active tasks.

        These files had their download task crash or time out without properly
        setting ERROR status. We reset them to QUEUED and retry the download.

        Args:
            db: Database session
            stuck_files: List of files stuck in DOWNLOADING status

        Returns:
            Dict with recovery statistics: files_recovered, tasks_retried
        """
        stats = {"files_recovered": 0, "tasks_retried": 0}

        for media_file in stuck_files:
            try:
                logger.warning(
                    f"Recovering stuck download for file {media_file.id}: {media_file.filename}"
                )

                # Delete any partial data from failed download
                self._cleanup_transcript_segments(db, int(media_file.id))

                # Reset to QUEUED so download task can retry
                update_media_file_status(db, int(media_file.id), FileStatus.QUEUED)
                stats["files_recovered"] += 1

                # Schedule retry of the download task
                if self.schedule_file_retry(int(media_file.id)):
                    stats["tasks_retried"] += 1
                    logger.info(f"Scheduled download retry for file {media_file.id}")

            except Exception as e:
                logger.error(f"Error recovering stuck download for file {media_file.id}: {e}")
                db.rollback()

        return stats

    def recover_oom_error_files(self, db: Session, oom_files: list[MediaFile]) -> dict[str, int]:
        """
        Recover files that failed with OOM errors by retrying them.

        Args:
            db: Database session
            oom_files: List of files with OOM errors identified by detection service

        Returns:
            Dict with recovery statistics: files_checked, files_retried, files_exhausted
        """
        stats = {
            "files_checked": 0,
            "files_retried": 0,
            "files_exhausted": 0,
        }

        for media_file in oom_files:
            stats["files_checked"] += 1

            # The effective ceiling is the admin-tunable SystemSettings value, never
            # `MediaFile.max_retries` — nothing writes that column (see A3 in
            # management.py), so logging/notifying from it always reported the ORM
            # default of 3 regardless of the actual configured limit.
            effective_max_retries = system_settings_service.get_retry_config(db)["max_retries"]

            # Check if retry is allowed based on system settings
            if not system_settings_service.should_retry_file(db, int(media_file.retry_count or 0)):
                logger.warning(
                    f"OOM retry limit exhausted for file {media_file.id} ({media_file.filename}) - "
                    f"retry_count: {media_file.retry_count}, max_retries: {effective_max_retries}"
                )
                stats["files_exhausted"] += 1
                # File stays in ERROR status (already there)
                continue

            try:
                logger.info(
                    f"Retrying OOM error for file {media_file.id} ({media_file.filename}) - "
                    f"attempt {(media_file.retry_count or 0) + 1}/{effective_max_retries}"
                )

                # Update recovery tracking fields. Both counters are nullable Integers
                # whose 0 is a default rather than a constraint, so `+= 1` on a NULL row
                # raises TypeError — normalize the same way every other read here does.
                media_file.retry_count = int(media_file.retry_count or 0) + 1  # type: ignore[assignment]
                media_file.recovery_attempts = int(media_file.recovery_attempts or 0) + 1  # type: ignore[assignment]
                media_file.last_recovery_attempt = datetime.now(UTC)  # type: ignore[assignment]
                db.commit()

                # Reset status to PENDING for retry
                update_media_file_status(db, int(media_file.id), FileStatus.PENDING)

                # Schedule new transcription task
                if self.schedule_file_retry(int(media_file.id)):
                    stats["files_retried"] += 1

                    # Send notification to user about retry attempt
                    try:
                        from app.tasks.transcription.notifications import (
                            send_notification_via_redis,
                        )

                        send_notification_via_redis(
                            int(media_file.user_id),
                            int(media_file.id),
                            FileStatus.PROCESSING,
                            f"Retrying after GPU memory error (attempt {media_file.retry_count}/{effective_max_retries})",
                            progress=0,
                            stage=TranscriptionStage.PREPARE,
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to send notification for OOM retry {media_file.id}: {e}"
                        )

                    logger.info(
                        f"Successfully scheduled OOM retry for file {media_file.id} - "
                        f"next backoff delay: {2 ** int(media_file.retry_count or 0) * 10} minutes"
                    )
                else:
                    logger.error(f"Failed to schedule OOM retry for file {media_file.id}")

            except Exception as e:
                logger.error(f"Error recovering OOM file {media_file.id}: {e}")

        return stats

    def recover_retriable_error_files(
        self, db: Session, error_files: list[MediaFile]
    ) -> dict[str, int]:
        """
        Recover ERROR files with retriable errors by resetting and re-queuing.

        Uses staggered processing (called with batch-limited lists) to avoid
        overwhelming external services like YouTube.

        Args:
            db: Database session
            error_files: List of retriable ERROR files from detection service

        Returns:
            Dict with recovery statistics
        """
        stats = {"files_checked": 0, "files_retried": 0, "files_failed": 0}

        for media_file in error_files:
            stats["files_checked"] += 1

            try:
                logger.info(
                    f"Retrying ERROR file {media_file.id} ({media_file.filename}) - "
                    f"category: {media_file.error_category}, "
                    f"attempt {int(media_file.retry_count or 0) + 1}"
                )

                # Increment retry count and reset status. Both counters are NULL-normalized
                # for the same reason the log line above reads `retry_count or 0`.
                media_file.retry_count = int(media_file.retry_count or 0) + 1
                media_file.recovery_attempts = int(media_file.recovery_attempts or 0) + 1
                media_file.last_recovery_attempt = datetime.now(UTC)  # type: ignore[assignment]

                # Clear old task records and reset to PENDING
                stale_tasks = db.query(Task).filter(Task.media_file_id == media_file.id).all()
                for task in stale_tasks:
                    task.status = "failed"  # type: ignore[assignment]
                    task.error_message = "Reset for automatic retry"  # type: ignore[assignment]
                    task.completed_at = datetime.now(UTC)  # type: ignore[assignment]

                media_file.active_task_id = None  # type: ignore[assignment]
                media_file.task_started_at = None  # type: ignore[assignment]
                update_media_file_status(db, int(media_file.id), FileStatus.PENDING)

                # Schedule the appropriate retry task
                if self.schedule_file_retry(int(media_file.id)):
                    stats["files_retried"] += 1
                    logger.info(
                        f"Scheduled retry for ERROR file {media_file.id} "
                        f"(category: {media_file.error_category})"
                    )
                else:
                    stats["files_failed"] += 1
                    logger.error(f"Failed to schedule retry for ERROR file {media_file.id}")

            except Exception as e:
                stats["files_failed"] += 1
                logger.error(f"Error recovering ERROR file {media_file.id}: {e}")

        return stats

    def recover_stuck_pending_download_files(
        self, db: Session, stuck_files: list[MediaFile]
    ) -> dict[str, int]:
        """
        Mark PENDING files with unrecoverable download errors as ERROR.

        These files have been stuck in PENDING with download failures that
        will never succeed (e.g., "requires sign-in", "private video").

        Args:
            db: Database session
            stuck_files: List of PENDING files with download errors

        Returns:
            Dict with recovery statistics: files_marked_error
        """
        stats = {"files_marked_error": 0}

        for media_file in stuck_files:
            try:
                logger.warning(
                    f"Marking PENDING file {media_file.id} as ERROR - "
                    f"unrecoverable download failure: {media_file.last_error_message}"
                )

                update_media_file_status(db, int(media_file.id), FileStatus.ERROR)
                stats["files_marked_error"] += 1

            except Exception as e:
                logger.error(f"Error marking stuck PENDING file {media_file.id} as ERROR: {e}")
                db.rollback()

        if stats["files_marked_error"] > 0:
            logger.info(f"Marked {stats['files_marked_error']} stuck PENDING files as ERROR")

        return stats
