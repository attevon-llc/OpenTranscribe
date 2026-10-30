"""Stuck / inconsistent / orphaned / abandoned task detection (mixin of ``TaskDetectionService``).

Split out of ``task_detection_service``; ``_MODULE_LOAD_TIME`` is still captured at import, i.e.
at process start.
"""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.broker_orphans import dead_unqueued_run_ids
from app.core.task_config import TaskRecoveryConfig
from app.core.task_liveness import TRANSCRIPTION_TASK_TYPE
from app.core.task_liveness import RunState
from app.core.task_liveness import probe_runs
from app.core.task_replay import replay_tracked_ids
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.utils.task_utils import update_media_file_from_task_status

logger = logging.getLogger(__name__)

# When this Python process started. Only the task types that do NOT carry liveness markers
# still use it (see ``_dead_task_ids``): for a transcription it was the #1020 defect, because
# a restart of whichever worker runs the health check made every transcription still queued
# for a GPU worker look like it predated the restart.
_MODULE_LOAD_TIME = datetime.now(UTC)


def _get_container_boot_time() -> datetime:
    """Returns the time this Python process started."""
    return _MODULE_LOAD_TIME


class TaskDetectionStuckMixin:
    """Detection of stuck, inconsistent, orphaned and abandoned tasks and files."""

    config: TaskRecoveryConfig

    def identify_stuck_tasks(self, db: Session) -> list[Task]:
        """
        Identify tasks that appear to be stuck in processing or pending state.

        A task is considered stuck if:
        1. It's in "pending" or "in_progress" state
        2. It was last updated longer ago than the STALENESS_THRESHOLD
        3. It's been running longer than its MAX_TASK_DURATION

        Args:
            db: Database session

        Returns:
            List of stuck tasks
        """
        now = datetime.now(UTC)
        stale_time = now - timedelta(seconds=self.config.STALENESS_THRESHOLD)

        # Find potentially stuck tasks
        potential_stuck_tasks = (
            db.query(Task)
            .filter(
                Task.status.in_(["pending", "in_progress"]),
                Task.updated_at < stale_time,
            )
            .all()
        )

        # Filter based on duration. A transcription's Task row is created when it is
        # DISPATCHED, so its age says nothing about whether it ever ran (#1020): it is stuck
        # only when it is provably dead, or has been running past its budget.
        runs = probe_runs(
            t.id for t in potential_stuck_tasks if t.task_type == TRANSCRIPTION_TASK_TYPE
        )
        # A task that is heartbeating, or waiting to be replayed after its worker died, is
        # the lost-task sweep's to judge (issue #1067), not a duration budget's.
        replay_owned = replay_tracked_ids(
            t.id for t in potential_stuck_tasks if t.task_type != TRANSCRIPTION_TASK_TYPE
        )
        dead_runs = dead_unqueued_run_ids(runs)
        stuck_tasks = []
        for task in potential_stuck_tasks:
            run = runs.get(str(task.id))
            if run is None:
                if str(task.id) in replay_owned:
                    continue
                if self._is_task_duration_exceeded(task, now):
                    stuck_tasks.append(task)
            elif str(task.id) in dead_runs or (
                run.state == RunState.RUNNING
                and run.started_at is not None
                and self._duration_exceeded_since(task, run.started_at, now)
            ):
                stuck_tasks.append(task)

        logger.info(f"Identified {len(stuck_tasks)} stuck tasks")
        return stuck_tasks

    def identify_stuck_files_without_active_celery_tasks(self, db: Session) -> list[MediaFile]:
        """
        Identify files that are stuck in PROCESSING state without any active Celery tasks.

        This method identifies files that are marked as PROCESSING but have:
        1. No tasks in "pending" or "in_progress" state
        2. Been in this state for longer than a threshold time
        3. No recent task updates (indicating Celery worker may have died)

        Args:
            db: Database session

        Returns:
            List of stuck files that need recovery
        """
        now = datetime.now(UTC)
        stuck_threshold = now - timedelta(minutes=5)  # Files stuck for 5+ minutes

        # Get PROCESSING files with stale task_last_update — filter in SQL
        processing_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.PROCESSING,
                MediaFile.file_size > 0,
                MediaFile.task_last_update < stuck_threshold,
            )
            .all()
        )

        if not processing_files:
            logger.info("Identified 0 stuck files without active Celery tasks")
            return []

        # Batch-fetch active tasks for all candidate files in one query
        file_ids = [mf.id for mf in processing_files]
        active_tasks_by_file: dict[int, list[Task]] = {}
        active_tasks_all = (
            db.query(Task)
            .filter(
                Task.media_file_id.in_(file_ids),
                Task.status.in_(["pending", "in_progress"]),
            )
            .all()
        )
        for task in active_tasks_all:
            active_tasks_by_file.setdefault(task.media_file_id, []).append(task)  # type: ignore[arg-type]
        dead_ids = self._dead_task_ids(active_tasks_all)

        stuck_files = []
        for media_file in processing_files:
            # task_last_update is non-None here: filtered on `< stuck_threshold` above
            assert media_file.task_last_update is not None
            active_tasks = active_tasks_by_file.get(media_file.id, [])  # type: ignore[call-overload]

            if not active_tasks:
                # Before marking the file as stuck, try to reconcile its status from task history.
                # This prevents false positives where tasks completed but file status wasn't updated.
                refreshed_file = update_media_file_from_task_status(db, int(media_file.id))
                if refreshed_file and refreshed_file.status in [
                    FileStatus.COMPLETED,
                    FileStatus.ERROR,
                ]:
                    logger.info(
                        f"File {media_file.id} ({media_file.filename}) was marked as processing "
                        f"but all tasks have finished with status {refreshed_file.status.value}; "
                        f"skipping recovery."
                    )
                    continue

                # File is still marked as processing and has no active tasks - treat as stuck.
                stuck_files.append(media_file)
                logger.info(
                    f"Found stuck file {media_file.id} ({media_file.filename}) - "
                    f"processing for {(now - media_file.task_last_update).total_seconds() / 60:.1f} minutes "
                    f"with no active tasks"
                )
            else:
                # Stuck only when every active task is provably dead. A transcription
                # still waiting in the broker is not (#1020).
                all_tasks_stale = all(str(task.id) in dead_ids for task in active_tasks)

                if all_tasks_stale:
                    # Before marking as stuck, check if file has completed tasks
                    # (e.g., transcription completed but file status wasn't updated)
                    completed_tasks = (
                        db.query(Task)
                        .filter(
                            Task.media_file_id == media_file.id,
                            Task.status == "completed",
                        )
                        .order_by(Task.completed_at.desc())
                        .first()
                    )

                    if completed_tasks:
                        # File has completed tasks but status wasn't updated
                        # Attempt to reconcile status and skip recovery
                        refreshed_file = update_media_file_from_task_status(db, int(media_file.id))
                        if refreshed_file and refreshed_file.status in [
                            FileStatus.COMPLETED,
                            FileStatus.ERROR,
                        ]:
                            logger.info(
                                f"File {media_file.id} ({media_file.filename}) has "
                                f"completed tasks but status was still PROCESSING. "
                                f"Reconciled to {refreshed_file.status.value}; "
                                f"skipping recovery."
                            )
                            continue

                    # File is still marked as processing with stale tasks
                    stuck_files.append(media_file)
                    logger.info(
                        f"Found stuck file {media_file.id} ({media_file.filename}) - "
                        f"has {len(active_tasks)} stale tasks"
                    )

        logger.info(f"Identified {len(stuck_files)} stuck files without active Celery tasks")
        return stuck_files

    def identify_inconsistent_media_files(self, db: Session) -> list[MediaFile]:
        """
        Identify media files with inconsistent states.

        A media file is considered inconsistent if:
        1. It's in PROCESSING state but has no active tasks
        2. It's in PENDING state but has been there for too long
        3. It has completed tasks but is still marked as PROCESSING

        Args:
            db: Database session

        Returns:
            List of media files with inconsistent states
        """
        inconsistent_files = []

        # Check processing files without active tasks
        processing_files = self._find_processing_files_without_tasks(db)
        inconsistent_files.extend(processing_files)

        # Check stale pending files
        stale_pending_files = self._find_stale_pending_files(db)
        inconsistent_files.extend(stale_pending_files)

        logger.info(f"Identified {len(inconsistent_files)} inconsistent files")
        return inconsistent_files

    def identify_orphaned_tasks(self, db: Session) -> list[Task]:
        """
        Identify tasks that were orphaned during system shutdown.

        Args:
            db: Database session

        Returns:
            List of orphaned tasks
        """
        cutoff_time = datetime.now(UTC) - timedelta(hours=self.config.ORPHANED_TASK_THRESHOLD)

        candidates = (
            db.query(Task)
            .filter(
                Task.status.in_(["pending", "in_progress"]),
                Task.updated_at < cutoff_time,
            )
            .all()
        )

        # A transcription quiet for an hour is usually just waiting for a GPU worker; only
        # one that is provably dead is orphaned (#1020).
        runs = probe_runs(t.id for t in candidates if t.task_type == TRANSCRIPTION_TASK_TYPE)
        dead_runs = dead_unqueued_run_ids(runs)
        orphaned_tasks = [t for t in candidates if str(t.id) not in runs or str(t.id) in dead_runs]

        logger.info(f"Identified {len(orphaned_tasks)} orphaned tasks")
        return orphaned_tasks

    def identify_abandoned_files(self, db: Session) -> tuple[list[MediaFile], list[str]]:
        """
        Identify files that were abandoned during processing.

        Includes:
        - Downloaded files (file_size > 0) stuck in PROCESSING
        - YouTube download files (file_size = 0 but have source_url) stuck in PROCESSING

        Excludes:
        - QUEUED files (playlist placeholders waiting for download)
        - DOWNLOADING files (actively being downloaded)
        - PENDING files (already queued for processing)
        - Playlist placeholder files (file_size = 0, no source_url)

        Args:
            db: Database session

        Returns:
            Tuple of (abandoned_files, stale_task_ids_to_mark_failed).
            Detection only — caller is responsible for mutating stale tasks.
        """
        from sqlalchemy import or_

        # Only look at PROCESSING files - explicitly exclude QUEUED/DOWNLOADING.
        # CRITICAL: also exclude files whose task_last_update is recent (within
        # the stuck threshold). Without this filter, fresh uploads were getting
        # flagged as "abandoned" because the periodic health-check forks new
        # ForkPoolWorker subprocesses on demand — those forks have a
        # _MODULE_LOAD_TIME (boot_time) that is *later* than a freshly-created
        # task's updated_at, which makes the boot-time check below incorrectly
        # decide the task pre-dates the worker. The 5-minute threshold gives
        # in-flight transcription pipelines breathing room before recovery
        # logic considers them abandoned.
        now = datetime.now(UTC)
        stuck_threshold = now - timedelta(minutes=5)
        abandoned_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.PROCESSING,
                or_(
                    MediaFile.file_size > 0,  # Downloaded files
                    MediaFile.source_url.isnot(None),  # YouTube download-in-progress files
                ),
                or_(
                    MediaFile.task_last_update.is_(None),  # never had a task update
                    MediaFile.task_last_update < stuck_threshold,  # haven't been touched in 5+ min
                ),
            )
            .all()
        )

        # Filter to only include files with no truly active tasks.
        truly_abandoned = []
        stale_task_ids: list[str] = []
        skipped_with_live_tasks = 0

        # Batch-fetch all active tasks for candidate files (avoids N+1 queries)
        file_ids = [mf.id for mf in abandoned_files]
        if file_ids:
            all_active_tasks = (
                db.query(Task)
                .filter(
                    Task.media_file_id.in_(file_ids),
                    Task.status.in_(["pending", "in_progress"]),
                )
                .all()
            )
            tasks_by_file: dict[int, list[Task]] = {}
            for task in all_active_tasks:
                tasks_by_file.setdefault(task.media_file_id, []).append(task)  # type: ignore[arg-type]
            dead_ids = self._dead_task_ids(all_active_tasks)
        else:
            tasks_by_file = {}
            dead_ids = set()

        for media_file in abandoned_files:
            active_db_tasks = tasks_by_file.get(int(media_file.id), [])

            if not active_db_tasks:
                truly_abandoned.append(media_file)
                continue

            # A task still queued or running keeps the file (#1020): only a file whose
            # every active task is provably dead is abandoned.
            if any(str(task.id) not in dead_ids for task in active_db_tasks):
                skipped_with_live_tasks += 1
            else:
                logger.info(
                    f"File {media_file.id} ({media_file.filename}) has "
                    f"{len(active_db_tasks)} dead DB tasks - "
                    f"marking as abandoned"
                )
                # Collect stale task IDs for caller to mark as failed
                stale_task_ids.extend(str(task.id) for task in active_db_tasks)
                truly_abandoned.append(media_file)

        logger.info(
            f"Identified {len(truly_abandoned)} abandoned files "
            f"(skipped {skipped_with_live_tasks} with live Celery tasks)"
        )
        return truly_abandoned, stale_task_ids

    def _is_task_duration_exceeded(self, task: Task, now: datetime) -> bool:
        """Check if a task has exceeded its maximum allowed duration.

        Measured from ``created_at``, which is when the task started for every type that
        creates its own Task row when it runs. A transcription does not (its row is created
        at dispatch); ``identify_stuck_tasks`` measures it from its heartbeat instead.
        """
        if not task.created_at:
            return False
        return self._duration_exceeded_since(task, task.created_at, now)

    def _duration_exceeded_since(self, task: Task, started_at: datetime, now: datetime) -> bool:
        """Whether ``task`` has run past its per-type budget since ``started_at``."""
        task_durations = self.config.MAX_TASK_DURATIONS
        if task_durations is None:
            return False
        max_duration = task_durations.get(str(task.task_type), task_durations["default"])
        return bool((now - started_at).total_seconds() > max_duration)

    @staticmethod
    def _dead_task_ids(tasks: list[Task]) -> set[str]:
        """The ids of the active tasks that are provably no longer queued or running.

        A transcription is judged by its liveness markers (#1020) and is never dead when
        they cannot be read. Other task types create their Task row when they start and
        carry no markers, so they keep the older test: last touched before this process
        started.
        """
        runs = probe_runs(t.id for t in tasks if t.task_type == TRANSCRIPTION_TASK_TYPE)
        boot_time = _get_container_boot_time()
        dead_runs = dead_unqueued_run_ids(runs)
        dead: set[str] = set()
        for task in tasks:
            run = runs.get(str(task.id))
            if run is not None:
                if str(task.id) in dead_runs:
                    dead.add(str(task.id))
            elif not (task.updated_at and task.updated_at > boot_time):
                dead.add(str(task.id))
        return dead

    def _find_processing_files_without_tasks(self, db: Session) -> list[MediaFile]:
        """Find files in PROCESSING state with no live tasks.

        Checks DB task records and compares against container boot time to
        catch files whose workers died (e.g., after container restart).

        Includes the same 5-minute task_last_update guard as
        identify_abandoned_files / identify_stuck_files_without_active_celery_tasks
        so that fresh uploads are never considered "inconsistent" while their
        transcription pipeline is still spinning up.
        """
        from sqlalchemy import or_

        now = datetime.now(UTC)
        stuck_threshold = now - timedelta(minutes=5)

        processing_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.PROCESSING,
                or_(
                    MediaFile.file_size > 0,
                    MediaFile.source_url.isnot(None),
                ),
                or_(
                    MediaFile.task_last_update.is_(None),
                    MediaFile.task_last_update < stuck_threshold,
                ),
            )
            .all()
        )

        # Batch-fetch all active tasks for processing files (avoids N+1)
        proc_file_ids = [mf.id for mf in processing_files]
        active_proc_tasks = (
            db.query(Task)
            .filter(
                Task.media_file_id.in_(proc_file_ids),
                Task.status.in_(["pending", "in_progress"]),
            )
            .all()
            if proc_file_ids
            else []
        )
        tasks_by_file: dict[int, list] = {}
        for task in active_proc_tasks:
            tasks_by_file.setdefault(task.media_file_id, []).append(task)  # type: ignore[arg-type]
        dead_ids = self._dead_task_ids(active_proc_tasks)

        files_without_tasks = []
        for media_file in processing_files:
            file_tasks = tasks_by_file.get(media_file.id, [])
            if all(str(task.id) in dead_ids for task in file_tasks):
                files_without_tasks.append(media_file)

        return files_without_tasks

    def _find_stale_pending_files(self, db: Session) -> list[MediaFile]:
        """Find files that have been in PENDING state for too long."""
        stale_time = datetime.now(UTC) - timedelta(hours=1)

        return (  # type: ignore[no-any-return]
            db.query(MediaFile)
            .filter(
                MediaFile.status == FileStatus.PENDING,
                MediaFile.upload_time < stale_time,
            )
            .all()
        )
