"""Error-state and stalled-download detection (mixin of ``TaskDetectionService``).

Split out of ``task_detection_service``.
"""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.task_config import TaskRecoveryConfig
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.task_detection_stuck import _get_container_boot_time
from app.utils.db_helpers import owned_in_tenant
from app.utils.error_classification import ErrorCategory

logger = logging.getLogger(__name__)


class TaskDetectionErrorsMixin:
    """Detection of files in an error or download-stalled state."""

    config: TaskRecoveryConfig

    def identify_stuck_downloading_files(self, db: Session) -> list[MediaFile]:
        """
        Identify files stuck in DOWNLOADING status without active download tasks.

        A file is considered stuck if:
        1. Status is DOWNLOADING
        2. No active Celery task exists for the file
        3. File has been in DOWNLOADING status for > 5 minutes

        Args:
            db: Database session

        Returns:
            List of files stuck in downloading
        """
        from datetime import timedelta

        now = datetime.now(UTC)
        stuck_threshold = now - timedelta(minutes=5)

        # Find files in DOWNLOADING status that were updated > 5 minutes ago
        downloading_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.DOWNLOADING,
                MediaFile.upload_time < stuck_threshold,  # Been downloading for > 5 min
            )
            .all()
        )

        if not downloading_files:
            return []

        boot_time = _get_container_boot_time()
        stuck_files = []

        # Batch-fetch all active download tasks (avoids N+1 per-file queries)
        dl_file_ids = [mf.id for mf in downloading_files]
        active_dl_tasks = (
            db.query(Task)
            .filter(
                Task.media_file_id.in_(dl_file_ids),
                Task.task_type.in_(["youtube_download", "url_download"]),
                Task.status.in_(["pending", "in_progress"]),
            )
            .all()
            if dl_file_ids
            else []
        )
        tasks_by_file: dict[int, list] = {}
        for task in active_dl_tasks:
            tasks_by_file.setdefault(task.media_file_id, []).append(task)  # type: ignore[arg-type]

        for media_file in downloading_files:
            file_tasks = tasks_by_file.get(media_file.id, [])
            if not file_tasks:
                stuck_files.append(media_file)
            else:
                has_post_boot_task = any(
                    task.updated_at and task.updated_at > boot_time for task in file_tasks
                )
                if not has_post_boot_task:
                    stuck_files.append(media_file)

        if stuck_files:
            logger.info(f"Identified {len(stuck_files)} files stuck in DOWNLOADING status")

        return stuck_files

    def identify_oom_error_files(self, db: Session) -> list[MediaFile]:
        """
        Identify ERROR files with OOM errors that are eligible for retry.

        A file is eligible if:
        1. Status is ERROR
        2. The failure site stored the GPU-OOM retry category (``error_category ==
           "gpu_oom"``). Keyed off the stored code, not ``last_error_message`` — that
           column holds a fixed user-facing sentence, not the raw CUDA text (issue #959).
        3. Sufficient time has passed since last_recovery_attempt (exponential backoff)

        Args:
            db: Database session

        Returns:
            List of files eligible for OOM retry
        """
        now = datetime.now(UTC)

        # Query ERROR files the failure site classified as GPU OOM
        oom_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.ERROR,
                MediaFile.error_category == ErrorCategory.GPU_OOM.value,
            )
            .all()
        )

        eligible_files = []
        for media_file in oom_files:
            # Calculate exponential backoff delay: 2^retry_count * 10 minutes.
            # `retry_count` is a nullable Integer whose 0 is a *default*, not a NOT NULL
            # constraint, so a row can legitimately hold NULL. Every other read of it in
            # this module and in task_recovery_service normalizes with `or 0`; this one
            # did not, and `2 ** None` raises TypeError. That exception is uncaught in
            # `periodic_health_check` step 4, so a single NULL row aborted the whole pass
            # and silently disabled steps 5-7 (retriable errors, stuck LLM tasks,
            # false-positive resets, post-transcription recovery) on every cycle.
            retry_count = int(media_file.retry_count or 0)
            backoff_minutes = (2**retry_count) * self.config.OOM_BACKOFF_BASE_MINUTES
            backoff_delay = timedelta(minutes=backoff_minutes)

            # Check if enough time has passed since last recovery attempt
            if media_file.last_recovery_attempt:
                time_since_last_attempt = now - media_file.last_recovery_attempt
                if time_since_last_attempt < backoff_delay:
                    logger.debug(
                        f"Skipping OOM retry for file {media_file.id} - "
                        f"backoff not elapsed ({time_since_last_attempt} < {backoff_delay})"
                    )
                    continue

            eligible_files.append(media_file)

        if eligible_files:
            logger.info(f"Identified {len(eligible_files)} files eligible for OOM retry")

        return eligible_files

    def identify_retriable_error_files(self, db: Session, batch_size: int = 20) -> list[MediaFile]:
        """
        Identify ERROR files eligible for automatic retry.

        Finds files with retriable error categories (auth/rate limit, network,
        temporary, system errors) that haven't exhausted their retries and
        have waited long enough since their last attempt.

        Returns a limited batch to avoid overwhelming external services
        (e.g., YouTube rate limits).

        Args:
            db: Database session
            batch_size: Maximum files to return per check cycle

        Returns:
            List of files eligible for retry (up to batch_size)
        """
        from app.utils.error_classification import get_retry_delay
        from app.utils.error_classification import should_retry

        now = datetime.now(UTC)

        # Retriable error categories (exclude OOM - handled separately)
        retriable_categories = [
            ErrorCategory.AUTH_OR_RATE_LIMIT.value,
            ErrorCategory.NETWORK_ERROR.value,
            ErrorCategory.TEMPORARY_SERVICE_ERROR.value,
            ErrorCategory.SYSTEM_ERROR.value,
            ErrorCategory.WORKER_LOST.value,
            ErrorCategory.DUPLICATE_KEY.value,
            ErrorCategory.UNKNOWN.value,
        ]

        # Query ERROR files with retriable categories
        error_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.ERROR,
                MediaFile.error_category.in_(retriable_categories),
            )
            .order_by(MediaFile.completed_at.asc())  # Oldest errors first
            .all()
        )

        eligible_files: list[MediaFile] = []
        for media_file in error_files:
            if len(eligible_files) >= batch_size:
                break

            # Check retry count against error classification
            error_category = ErrorCategory(media_file.error_category)
            if not should_retry(error_category, int(media_file.retry_count or 0)):
                continue

            # Enforce retry delay based on error category
            if media_file.completed_at:
                time_since_failure = (now - media_file.completed_at).total_seconds()
                required_delay = get_retry_delay(error_category, int(media_file.retry_count or 0))
                if time_since_failure < required_delay:
                    continue

            eligible_files.append(media_file)

        if eligible_files:
            logger.info(
                f"Identified {len(eligible_files)} retriable ERROR files "
                f"(of {len(error_files)} total retriable errors, "
                f"batch limit: {batch_size})"
            )

        return eligible_files

    def identify_retriable_error_files_split(
        self,
        db: Session,
        youtube_batch_size: int = 3,
        transcription_batch_size: int = 20,
    ) -> tuple[list[MediaFile], list[MediaFile]]:
        """Split retriable errors into YouTube downloads vs transcription retries.

        YouTube downloads (source_url set, no storage_path) are throttled to a
        small batch to respect YouTube rate limits.  Transcription retries are
        less sensitive and use a larger batch.

        Returns:
            Tuple of (youtube_files, transcription_files).
        """
        from app.utils.error_classification import ErrorCategory
        from app.utils.error_classification import get_retry_delay
        from app.utils.error_classification import should_retry

        now = datetime.now(UTC)

        retriable_categories = [
            ErrorCategory.AUTH_OR_RATE_LIMIT.value,
            ErrorCategory.NETWORK_ERROR.value,
            ErrorCategory.TEMPORARY_SERVICE_ERROR.value,
            ErrorCategory.SYSTEM_ERROR.value,
            ErrorCategory.WORKER_LOST.value,
            ErrorCategory.DUPLICATE_KEY.value,
            ErrorCategory.UNKNOWN.value,
        ]

        error_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.ERROR,
                MediaFile.error_category.in_(retriable_categories),
            )
            .order_by(MediaFile.completed_at.asc())
            .all()
        )

        youtube_files: list[MediaFile] = []
        transcription_files: list[MediaFile] = []

        for media_file in error_files:
            error_category = ErrorCategory(media_file.error_category)
            if not should_retry(error_category, int(media_file.retry_count or 0)):
                continue

            if media_file.completed_at:
                time_since_failure = (now - media_file.completed_at).total_seconds()
                required_delay = get_retry_delay(error_category, int(media_file.retry_count or 0))
                if time_since_failure < required_delay:
                    continue

            # YouTube download: has source_url but no storage_path (or empty)
            is_youtube = bool(media_file.source_url) and not media_file.storage_path

            if is_youtube:
                if len(youtube_files) < youtube_batch_size:
                    youtube_files.append(media_file)
            else:
                if len(transcription_files) < transcription_batch_size:
                    transcription_files.append(media_file)

            # Stop early if both batches full
            if (
                len(youtube_files) >= youtube_batch_size
                and len(transcription_files) >= transcription_batch_size
            ):
                break

        if youtube_files or transcription_files:
            logger.info(
                f"Retriable split: {len(youtube_files)} YouTube "
                f"(limit {youtube_batch_size}), "
                f"{len(transcription_files)} transcription "
                f"(limit {transcription_batch_size})"
            )

        return youtube_files, transcription_files

    def find_user_problem_files(
        self,
        db: Session,
        user_id: int | None = None,
        *,
        organization_id: OrgScope = UNSCOPED,
    ) -> list[MediaFile]:
        """
        Find files that may need recovery for a specific user or all users.

        Args:
            db: Database session
            user_id: Optional user ID to filter by
            organization_id: With ``user_id``, restrict to that user's files in this
                tenant (an org id, or None for org-less files). UNSCOPED (default)
                applies no tenant gate — the admin sweep.

        Returns:
            List of files that may need recovery
        """
        query = db.query(MediaFile)
        if user_id:
            query = query.filter(
                owned_in_tenant(MediaFile, user_id=user_id, organization_id=organization_id)
            )

        problem_files = query.filter(
            MediaFile.status.in_([FileStatus.PROCESSING, FileStatus.PENDING])
        ).all()

        # Filter by age
        aged_files = []
        age_threshold = timedelta(hours=self.config.FILE_RECOVERY_AGE_THRESHOLD)

        for media_file in problem_files:
            # `server_default=now()` is NOT a NOT NULL constraint — the premise this
            # assert rested on is the one the retry_count bug disproved (issue #431).
            # The query above filters only on status and user_id, and this loop has no
            # try/except, so a single NULL upload_time aborted the whole sweep with an
            # AssertionError (and vanished entirely under `python -O`). A row with no
            # upload time cannot be aged, so it is not yet old enough to recover.
            if media_file.upload_time is None:
                logger.warning("File %s has no upload_time; skipping age check", media_file.id)
                continue
            file_age = datetime.now(UTC) - media_file.upload_time
            if file_age > age_threshold:
                aged_files.append(media_file)

        logger.info(f"Found {len(aged_files)} problem files for user {user_id or 'all'}")
        return aged_files

    def identify_stuck_pending_download_files(self, db: Session) -> list[MediaFile]:
        """
        Identify PENDING files that have download errors and should be marked ERROR.

        These are files stuck in PENDING status with download failure messages
        (e.g., "requires sign-in", "private video", "not available") that will
        never succeed on retry.

        Args:
            db: Database session

        Returns:
            List of PENDING files that should be marked as ERROR
        """
        from sqlalchemy import or_

        # Files stuck in PENDING for > 1 hour with download error messages
        stale_time = datetime.now(UTC) - timedelta(hours=1)

        stuck_pending = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.PENDING,
                MediaFile.upload_time < stale_time,
                MediaFile.file_size == 0,
                or_(MediaFile.storage_path.is_(None), MediaFile.storage_path == ""),
                MediaFile.last_error_message.isnot(None),
            )
            .all()
        )

        # Filter for known unrecoverable error patterns
        unrecoverable_patterns = [
            "sign-in",
            "private video",
            "not available",
            "members-only",
            "removed",
            "deleted",
        ]

        stuck_files = []
        for media_file in stuck_pending:
            if media_file.last_error_message:
                error_lower = media_file.last_error_message.lower()
                if any(pattern in error_lower for pattern in unrecoverable_patterns):
                    stuck_files.append(media_file)

        if stuck_files:
            logger.info(
                f"Identified {len(stuck_files)} PENDING files with unrecoverable download errors"
            )

        return stuck_files
