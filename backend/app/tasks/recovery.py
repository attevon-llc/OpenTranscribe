"""
Celery tasks for system recovery operations.

This module contains Celery tasks that handle various recovery scenarios,
separated from other utility tasks for better organization.
"""

import logging
from datetime import UTC
from datetime import datetime

from app.core.celery import celery_app
from app.core.constants import UtilityPriority
from app.core.task_config import task_recovery_config
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import Task
from app.services.task_detection_service import task_detection_service
from app.services.task_recovery_service import task_recovery_service
from app.utils.task_lock import with_task_lock

logger = logging.getLogger(__name__)


@celery_app.task(
    name="system.startup_recovery",
    bind=True,
    priority=UtilityPriority.EMERGENCY,
    acks_late=True,
    reject_on_worker_lost=True,
)
@with_task_lock("system_startup_recovery", timeout=900)
def startup_recovery_task(self):
    """
    Recovery task to run on system startup to handle files/tasks interrupted by
    system crashes, power outages, or Docker shutdowns.

    This task specifically handles:
    1. Files stuck in PROCESSING state with no active tasks
    2. Tasks that were in progress when system went down
    3. Files that should be retried after system recovery

    Guarded by ``with_task_lock`` (issue #284 A1.15): every API replica dispatches this
    on startup, so on a rollout N replicas each re-dispatched recovery for the SAME
    stuck files — re-queueing the same GPU transcription several times over. The lock
    makes it run once per rollout regardless of replica count.

    Returns:
        Dictionary with summary of recovery actions
    """
    summary = {
        "abandoned_files_found": 0,
        "abandoned_files_reset": 0,
        "abandoned_files_completed": 0,
        "orphaned_tasks_found": 0,
        "orphaned_tasks_failed": 0,
        "files_retried": 0,
    }

    try:
        with session_scope() as db:
            # Step 1: Handle abandoned files
            abandoned_files, stale_task_ids = task_detection_service.identify_abandoned_files(db)
            summary["abandoned_files_found"] = len(abandoned_files)

            # Mark stale tasks as failed (mutations separated from detection)
            for task_id in stale_task_ids:
                task = db.query(Task).get(task_id)
                if task:
                    task.status = "failed"  # type: ignore[assignment]
                    task.error_message = "Celery task lost after system restart"  # type: ignore[assignment]
                    task.completed_at = datetime.now(UTC)  # type: ignore[assignment]
            if stale_task_ids:
                db.flush()

            reset_stats = task_recovery_service.reset_abandoned_files(db, abandoned_files)
            summary["abandoned_files_reset"] = reset_stats["files_reset"]
            summary["abandoned_files_completed"] = reset_stats["files_completed"]

            # Step 2: Retry abandoned files that were reset (not those that were completed)
            retry_count = 0
            files_to_retry = [
                f
                for f in abandoned_files
                if f.status == FileStatus.PENDING  # Only retry files that were reset to PENDING
            ]
            for media_file in files_to_retry:
                if task_recovery_service.schedule_file_retry(int(media_file.id)):
                    retry_count += 1
            summary["files_retried"] = retry_count

            # Step 3: Handle orphaned tasks
            orphaned_tasks = task_detection_service.identify_orphaned_tasks(db)
            summary["orphaned_tasks_found"] = len(orphaned_tasks)

            failed_count = task_recovery_service.recover_orphaned_tasks(db, orphaned_tasks)
            summary["orphaned_tasks_failed"] = failed_count

            logger.info(
                f"Startup recovery completed: "
                f"Found {summary['abandoned_files_found']} abandoned files, "
                f"reset {summary['abandoned_files_reset']} incomplete files, "
                f"completed {summary['abandoned_files_completed']} finished files, "
                f"failed {summary['orphaned_tasks_failed']} orphaned tasks, "
                f"retried {summary['files_retried']} transcriptions"
            )

    except Exception as e:
        logger.error(f"Error in startup recovery: {str(e)}")
        summary["error"] = str(e)  # type: ignore[assignment]

    return summary


@celery_app.task(
    name="system.recover_user_files",
    bind=True,
    priority=UtilityPriority.OPERATIONAL,
    acks_late=True,
    reject_on_worker_lost=True,
)
def recover_user_files_task(self, user_id: int | None = None):
    """
    Task to recover files for a specific user or all users.
    Useful when a user reports missing/stuck files.

    Args:
        user_id: If provided, only recover files for this user. Otherwise recover all.

    Returns:
        Dictionary with summary of recovery actions
    """
    summary = {
        "users_processed": 1 if user_id else 0,
        "files_checked": 0,
        "files_recovered": 0,
        "tasks_retried": 0,
    }

    try:
        with session_scope() as db:
            if not user_id:
                # Count unique users for all files
                from app.models.media import MediaFile

                user_count = db.query(MediaFile.user_id).distinct().count()
                summary["users_processed"] = user_count

            # Find problem files
            problem_files = task_detection_service.find_user_problem_files(db, user_id)
            summary["files_checked"] = len(problem_files)

            # Recover the files
            recovery_stats = task_recovery_service.recover_user_files(db, problem_files)
            summary.update(recovery_stats)

            logger.info(
                f"User file recovery completed: "
                f"Processed {summary['users_processed']} users, "
                f"checked {summary['files_checked']} files, "
                f"recovered {summary['files_recovered']} files, "
                f"retried {summary['tasks_retried']} tasks"
            )

    except Exception as e:
        logger.error(f"Error in user file recovery: {str(e)}")
        summary["error"] = str(e)  # type: ignore[assignment]

    return summary


def _check_opensearch_health(summary: dict) -> None:
    """Check and repair OpenSearch indices with corrupted HNSW vector segments.

    Runs outside the DB session since it only touches OpenSearch.
    Updates the summary dict in place with repair results.
    """
    try:
        from app.services.opensearch_service import check_and_repair_indices
        from app.services.search.index_health import check_and_repair_chunks_index

        # Two calls, one per layer: the speaker/legacy indices belong to
        # opensearch_service, the chunk plane to services/search (issue #540 —
        # merging them would form a package cycle; see index_health's docstring).
        repaired_indices = check_and_repair_indices() + check_and_repair_chunks_index()
        if repaired_indices:
            summary["opensearch_indices_repaired"] = repaired_indices
            logger.info(
                f"OpenSearch index repair: repaired {len(repaired_indices)} indices: "
                f"{', '.join(repaired_indices)}"
            )
    except Exception as os_err:
        logger.warning(f"OpenSearch health check failed (non-fatal): {os_err}")


@celery_app.task(
    name="system.health_check",
    bind=True,
    priority=UtilityPriority.OPERATIONAL,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=task_recovery_config.HEALTH_CHECK_MAX_RUNTIME - 30,
    time_limit=task_recovery_config.HEALTH_CHECK_MAX_RUNTIME,
)
@with_task_lock("system.health_check", timeout=task_recovery_config.HEALTH_CHECK_MAX_RUNTIME)
def periodic_health_check_task(self):
    """
    Periodic task to check for stuck tasks and inconsistent media files.

    This task runs on a schedule to identify and recover:
    1. Tasks that are stuck in processing or pending state
    2. Media files with inconsistent states
    3. Files stuck in processing without active Celery tasks
    4. Files that failed with GPU Out-of-Memory errors (OOM)

    Returns:
        Dictionary with summary of actions taken
    """
    summary = {
        "stuck_tasks_found": 0,
        "stuck_tasks_recovered": 0,
        "inconsistent_files_found": 0,
        "inconsistent_files_fixed": 0,
        "stuck_files_without_celery_found": 0,
        "stuck_files_without_celery_recovered": 0,
        "stuck_downloading_found": 0,
        "stuck_downloading_recovered": 0,
        "oom_files_found": 0,
        "oom_files_retried": 0,
        "oom_files_exhausted": 0,
        "retriable_errors_found": 0,
        "retriable_errors_retried": 0,
        "stuck_pending_downloads_found": 0,
        "stuck_pending_downloads_marked_error": 0,
        "stuck_llm_tasks_found": 0,
        "stuck_llm_tasks_marked_failed": 0,
        "false_positive_tasks_found": 0,
        "false_positive_tasks_reset": 0,
        "incomplete_post_transcription_found": 0,
        "post_transcription_tasks_dispatched": 0,
        "opensearch_indices_repaired": [],
    }

    try:
        with session_scope() as db:
            # Step 1: Identify and recover stuck tasks
            stuck_tasks = task_detection_service.identify_stuck_tasks(db)
            summary["stuck_tasks_found"] = len(stuck_tasks)

            recovered_count = 0
            for task in stuck_tasks:
                if task_recovery_service.recover_stuck_task(db, task):
                    recovered_count += 1

            summary["stuck_tasks_recovered"] = recovered_count

            # Step 2: Identify and fix inconsistent media files
            inconsistent_files = task_detection_service.identify_inconsistent_media_files(db)
            summary["inconsistent_files_found"] = len(inconsistent_files)

            fixed_count = 0
            for media_file in inconsistent_files:
                if task_recovery_service.fix_inconsistent_media_file(db, media_file):
                    fixed_count += 1

            summary["inconsistent_files_fixed"] = fixed_count

            # Step 3: Identify and recover files stuck without active Celery tasks
            stuck_files_without_celery = (
                task_detection_service.identify_stuck_files_without_active_celery_tasks(db)
            )
            summary["stuck_files_without_celery_found"] = len(stuck_files_without_celery)

            if stuck_files_without_celery:
                recovery_stats = task_recovery_service.recover_stuck_files_without_celery_tasks(
                    db, stuck_files_without_celery
                )
                summary["stuck_files_without_celery_recovered"] = recovery_stats["files_recovered"]
                logger.info(
                    f"Recovered {recovery_stats['files_recovered']} stuck files without active Celery tasks"
                )

            # Step 3.5: Identify and recover files stuck in DOWNLOADING status
            stuck_downloading_files = task_detection_service.identify_stuck_downloading_files(db)
            summary["stuck_downloading_found"] = len(stuck_downloading_files)

            if stuck_downloading_files:
                download_recovery_stats = task_recovery_service.recover_stuck_downloading_files(
                    db, stuck_downloading_files
                )
                summary["stuck_downloading_recovered"] = download_recovery_stats["files_recovered"]
                logger.info(
                    f"Download Recovery: Found {len(stuck_downloading_files)} stuck files, "
                    f"recovered {download_recovery_stats['files_recovered']}, "
                    f"retried {download_recovery_stats['tasks_retried']}"
                )

            # Step 4: Identify and recover files with OOM errors
            if task_recovery_config.OOM_RETRY_ENABLED:
                oom_files = task_detection_service.identify_oom_error_files(db)
                summary["oom_files_found"] = len(oom_files)

                if oom_files:
                    oom_stats = task_recovery_service.recover_oom_error_files(db, oom_files)
                    summary["oom_files_retried"] = oom_stats["files_retried"]
                    summary["oom_files_exhausted"] = oom_stats["files_exhausted"]
                    logger.info(
                        f"OOM Recovery: Found {len(oom_files)} files, "
                        f"retried {oom_stats['files_retried']}, "
                        f"exhausted {oom_stats['files_exhausted']}"
                    )

            # Step 5: Identify and recover retriable ERROR files (staggered batches)
            # YouTube downloads use a strict per-cycle cap to stay within
            # rate limits and avoid bans.  Transcription retries are less
            # sensitive so they get a larger batch.
            from app.core.config import settings as _settings

            yt_batch = _settings.YOUTUBE_RECOVERY_BATCH_SIZE  # default 3
            transcription_batch = 20

            yt_retries, tx_retries = task_detection_service.identify_retriable_error_files_split(
                db,
                youtube_batch_size=yt_batch,
                transcription_batch_size=transcription_batch,
            )
            all_retriable = yt_retries + tx_retries
            summary["retriable_errors_found"] = len(all_retriable)
            summary["retriable_youtube_found"] = len(yt_retries)
            summary["retriable_errors_retried"] = 0

            if all_retriable:
                retry_stats = task_recovery_service.recover_retriable_error_files(db, all_retriable)
                summary["retriable_errors_retried"] = retry_stats["files_retried"]
                logger.info(
                    f"Error Retry: Found {len(yt_retries)} YouTube + "
                    f"{len(tx_retries)} transcription retriable files, "
                    f"retried {retry_stats['files_retried']}, "
                    f"failed {retry_stats['files_failed']}"
                )

            # Step 5.5: Mark PENDING files with unrecoverable download errors as ERROR
            stuck_pending_downloads = task_detection_service.identify_stuck_pending_download_files(
                db
            )
            summary["stuck_pending_downloads_found"] = len(stuck_pending_downloads)

            if stuck_pending_downloads:
                pending_stats = task_recovery_service.recover_stuck_pending_download_files(
                    db, stuck_pending_downloads
                )
                summary["stuck_pending_downloads_marked_error"] = pending_stats[
                    "files_marked_error"
                ]
                logger.info(
                    f"Stuck PENDING Recovery: Found {len(stuck_pending_downloads)} files with "
                    f"unrecoverable download errors, marked {pending_stats['files_marked_error']} as ERROR"
                )

            # Step 5.6: Mark stuck LLM tasks (in_progress > 6 hours) as failed
            stuck_llm_tasks = task_detection_service.identify_stuck_llm_tasks(db)
            summary["stuck_llm_tasks_found"] = len(stuck_llm_tasks)

            if stuck_llm_tasks:
                llm_stats = task_recovery_service.recover_stuck_llm_tasks(db, stuck_llm_tasks)
                summary["stuck_llm_tasks_marked_failed"] = llm_stats["tasks_marked_failed"]
                logger.info(
                    f"Stuck LLM Task Recovery: Found {len(stuck_llm_tasks)} tasks stuck > 6 hours, "
                    f"marked {llm_stats['tasks_marked_failed']} as failed"
                )

            # Step 5.7: Reset false-positive failed tasks for retry
            false_positive_tasks = task_detection_service.identify_false_positive_failed_tasks(db)
            summary["false_positive_tasks_found"] = len(false_positive_tasks)

            if false_positive_tasks:
                fp_stats = task_recovery_service.recover_false_positive_failed_tasks(
                    db, false_positive_tasks
                )
                summary["false_positive_tasks_reset"] = fp_stats["tasks_reset"]
                logger.info(
                    f"False-Positive Task Recovery: Found {len(false_positive_tasks)} tasks "
                    f"falsely marked failed, reset {fp_stats['tasks_reset']} for retry"
                )

            # Step 6: Recover COMPLETED files with incomplete post-transcription processing
            incomplete_files = task_detection_service.identify_incomplete_post_transcription_files(
                db, batch_size=10
            )
            summary["incomplete_post_transcription_found"] = len(incomplete_files)

            if incomplete_files:
                post_stats = task_recovery_service.recover_incomplete_post_transcription_files(
                    db, incomplete_files
                )
                summary["post_transcription_tasks_dispatched"] = post_stats[
                    "total_tasks_dispatched"
                ]
                logger.info(
                    f"Post-transcription recovery: Found {len(incomplete_files)} incomplete files, "
                    f"dispatched {post_stats['total_tasks_dispatched']} tasks"
                )

            # Log summary
            logger.info(
                f"Periodic health check completed: "
                f"Found {summary['stuck_tasks_found']} stuck tasks, recovered {summary['stuck_tasks_recovered']}; "
                f"Found {summary['inconsistent_files_found']} inconsistent files, fixed {summary['inconsistent_files_fixed']}; "
                f"Found {summary['stuck_files_without_celery_found']} stuck files without Celery tasks, recovered {summary['stuck_files_without_celery_recovered']}; "
                f"Found {summary['stuck_downloading_found']} stuck downloads, recovered {summary['stuck_downloading_recovered']}; "
                f"Found {summary['oom_files_found']} OOM error files, retried {summary['oom_files_retried']}, exhausted {summary['oom_files_exhausted']}; "
                f"Found {summary['retriable_errors_found']} retriable errors, retried {summary['retriable_errors_retried']}; "
                f"Found {summary['stuck_pending_downloads_found']} stuck PENDING downloads, marked {summary['stuck_pending_downloads_marked_error']} as ERROR; "
                f"Found {summary['stuck_llm_tasks_found']} stuck LLM tasks, marked {summary['stuck_llm_tasks_marked_failed']} as failed; "
                f"Found {summary['false_positive_tasks_found']} false-positive tasks, reset {summary['false_positive_tasks_reset']}; "
                f"Found {summary['incomplete_post_transcription_found']} incomplete post-transcription files, dispatched {summary['post_transcription_tasks_dispatched']} tasks"
                f"; OpenSearch indices repaired: {summary['opensearch_indices_repaired']}"
            )

    except Exception as e:
        logger.error(f"Error in periodic health check: {str(e)}")
        summary["error"] = str(e)  # type: ignore[assignment]

    # Step 7: Check and repair OpenSearch indices (corrupted HNSW vector segments)
    _check_opensearch_health(summary)

    return summary
