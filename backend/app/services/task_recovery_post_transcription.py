"""Post-transcription recovery (mixin of ``TaskRecoveryService``).

Split out of ``task_recovery_service``.
"""

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.task_config import TaskRecoveryConfig
from app.models.media import Task
from app.services.task_recovery_stuck import _recovery_attempts
from app.services.task_row_supersede import reopen_if_slot_free

logger = logging.getLogger(__name__)


class TaskRecoveryPostTranscriptionMixin:
    """Recovery of COMPLETED files missing post-transcription results, and of LLM tasks."""

    config: TaskRecoveryConfig

    def recover_incomplete_post_transcription_files(
        self,
        db: Session,
        incomplete_files: list,
    ) -> dict[str, int]:
        """
        Dispatch missing post-transcription tasks for COMPLETED files.

        For each file, dispatches whichever tasks are missing (analytics,
        speaker ID, summary, topics). Each dispatch is individually
        try/excepted so one failure doesn't block others.

        Args:
            db: Database session
            incomplete_files: List of IncompletePostTranscriptionFile from detection

        Returns:
            Dict with dispatch statistics
        """
        stats = {
            "files_processed": 0,
            "summaries_dispatched": 0,
            "topics_dispatched": 0,
            "speaker_id_dispatched": 0,
            "analytics_dispatched": 0,
            "search_indexing_dispatched": 0,
            "total_tasks_dispatched": 0,
            "dispatch_errors": 0,
        }

        for incomplete in incomplete_files:
            stats["files_processed"] += 1
            file_uuid = incomplete.file_uuid

            # 1. Analytics (no LLM needed)
            if incomplete.missing_analytics:
                try:
                    from app.tasks.analytics import analyze_transcript_task

                    result = analyze_transcript_task.delay(file_uuid=file_uuid)
                    stats["analytics_dispatched"] += 1
                    stats["total_tasks_dispatched"] += 1
                    logger.info(
                        f"[Post-transcription recovery] Dispatched analytics task "
                        f"{result.id} for file {incomplete.media_file_id}"
                    )
                except Exception as e:
                    stats["dispatch_errors"] += 1
                    logger.error(
                        f"[Post-transcription recovery] Failed to dispatch analytics "
                        f"for file {incomplete.media_file_id}: {e}"
                    )

            # 2. Speaker identification (before summary so names are available)
            if incomplete.missing_speaker_id:
                try:
                    from app.tasks.speaker_tasks import identify_speakers_llm_task

                    result = identify_speakers_llm_task.delay(file_uuid=file_uuid)
                    stats["speaker_id_dispatched"] += 1
                    stats["total_tasks_dispatched"] += 1
                    logger.info(
                        f"[Post-transcription recovery] Dispatched speaker ID task "
                        f"{result.id} for file {incomplete.media_file_id}"
                    )
                except Exception as e:
                    stats["dispatch_errors"] += 1
                    logger.error(
                        f"[Post-transcription recovery] Failed to dispatch speaker ID "
                        f"for file {incomplete.media_file_id}: {e}"
                    )

            # 3. Summarization
            if incomplete.missing_summary:
                try:
                    from app.tasks.summarization import summarize_transcript_task

                    result = summarize_transcript_task.delay(file_uuid=file_uuid)
                    stats["summaries_dispatched"] += 1
                    stats["total_tasks_dispatched"] += 1
                    logger.info(
                        f"[Post-transcription recovery] Dispatched summary task "
                        f"{result.id} for file {incomplete.media_file_id}"
                    )
                except Exception as e:
                    stats["dispatch_errors"] += 1
                    logger.error(
                        f"[Post-transcription recovery] Failed to dispatch summary "
                        f"for file {incomplete.media_file_id}: {e}"
                    )

            # 4. Topic extraction
            if incomplete.missing_topics:
                try:
                    from app.tasks.topic_extraction import extract_topics_task

                    result = extract_topics_task.delay(file_uuid=file_uuid, force_regenerate=False)
                    stats["topics_dispatched"] += 1
                    stats["total_tasks_dispatched"] += 1
                    logger.info(
                        f"[Post-transcription recovery] Dispatched topic extraction task "
                        f"{result.id} for file {incomplete.media_file_id}"
                    )
                except Exception as e:
                    stats["dispatch_errors"] += 1
                    logger.error(
                        f"[Post-transcription recovery] Failed to dispatch topics "
                        f"for file {incomplete.media_file_id}: {e}"
                    )

            # 5. Search indexing (chunk-level embeddings for neural search)
            if incomplete.missing_search_indexing:
                try:
                    from app.tasks.search_indexing_task import index_transcript_search_task

                    result = index_transcript_search_task.delay(
                        file_id=incomplete.media_file_id,
                        file_uuid=file_uuid,
                        user_id=incomplete.user_id,
                    )
                    stats["search_indexing_dispatched"] += 1
                    stats["total_tasks_dispatched"] += 1
                    logger.info(
                        f"[Post-transcription recovery] Dispatched search indexing task "
                        f"{result.id} for file {incomplete.media_file_id}"
                    )
                except Exception as e:
                    stats["dispatch_errors"] += 1
                    logger.error(
                        f"[Post-transcription recovery] Failed to dispatch search indexing "
                        f"for file {incomplete.media_file_id}: {e}"
                    )

        logger.info(
            f"[Post-transcription recovery] Processed {stats['files_processed']} files, "
            f"dispatched {stats['total_tasks_dispatched']} tasks "
            f"(analytics={stats['analytics_dispatched']}, "
            f"speaker_id={stats['speaker_id_dispatched']}, "
            f"summaries={stats['summaries_dispatched']}, "
            f"topics={stats['topics_dispatched']}, "
            f"search_indexing={stats['search_indexing_dispatched']}), "
            f"errors={stats['dispatch_errors']}"
        )

        return stats

    def recover_stuck_llm_tasks(self, db: Session, stuck_tasks: list[Task]) -> dict[str, int]:
        """
        Mark LLM tasks stuck in_progress for > 6 hours as failed.

        These tasks are abandoned (worker crash, network timeout) and need
        to be marked failed so recovery can retry them.

        Args:
            db: Database session
            stuck_tasks: List of stuck LLM tasks

        Returns:
            Dict with recovery statistics: tasks_marked_failed
        """
        stats = {"tasks_marked_failed": 0}

        for task in stuck_tasks:
            try:
                # `task.created_at` is nullable — the server_default only fires for an
                # INSERT that omits the column — and this is only a log line, so an
                # unknown creation time must not abort the sweep that marks the task failed.
                stuck_for = (
                    str(datetime.now(UTC) - task.created_at)
                    if task.created_at is not None
                    else "an unknown duration (created_at is NULL)"
                )
                logger.warning(
                    f"Marking stuck LLM task {task.id} ({task.task_type}) as failed - "
                    f"stuck in progress for {stuck_for}"
                )

                task.status = "failed"  # type: ignore[assignment]
                task.error_message = "Task timeout - stuck in progress for > 6 hours"  # type: ignore[assignment]
                task.completed_at = datetime.now(UTC)  # type: ignore[assignment]
                task.updated_at = datetime.now(UTC)  # type: ignore[assignment]
                stats["tasks_marked_failed"] += 1

            except Exception as e:
                logger.error(f"Error marking stuck task {task.id} as failed: {e}")
                db.rollback()

        try:
            db.commit()
            if stats["tasks_marked_failed"] > 0:
                logger.info(f"Marked {stats['tasks_marked_failed']} stuck LLM tasks as failed")
        except Exception as e:
            logger.error(f"Error committing stuck task recovery: {e}")
            db.rollback()
            return {"tasks_marked_failed": 0}

        return stats

    def recover_false_positive_failed_tasks(
        self, db: Session, false_positive_tasks: list[Task]
    ) -> dict[str, int]:
        """
        Reset tasks falsely marked failed by overly aggressive recovery.

        These tasks have error "Task recovered after being stuck in processing"
        but likely completed successfully. Reset to pending for automatic retry.

        Args:
            db: Database session
            false_positive_tasks: List of falsely failed tasks

        Returns:
            Dict with recovery statistics: tasks_reset
        """
        stats = {"tasks_reset": 0}

        for task in false_positive_tasks:
            try:
                logger.info(
                    f"Resetting false-positive failed task {task.id} ({task.task_type}) to pending"
                )

                # Track recovery attempts to prevent infinite loop. The counter survives the
                # next stuck-task failure because recover_stuck_task carries it forward.
                current_attempts = _recovery_attempts(task.error_message)

                if current_attempts >= 2:
                    logger.warning(
                        f"Task {task.id} has been reset {current_attempts} times, "
                        f"marking as permanently failed"
                    )
                    task.error_message = (
                        f"Permanently failed after {current_attempts} recovery attempts"  # type: ignore[assignment]
                    )
                    continue

                # A newer run of this step may hold the file's one active slot (v448, issue
                # #1185) or may have completed already; then this old row is simply not
                # reopened (the supersede sweep would only fail it again).
                if not reopen_if_slot_free(
                    db,
                    task,
                    {
                        "status": "pending",
                        "error_message": f"recovery_attempt_{current_attempts + 1}_reset",
                        "completed_at": None,
                        "updated_at": datetime.now(UTC),
                    },
                ):
                    logger.info(
                        f"Not resetting task {task.id} ({task.task_type}): a newer run of the "
                        f"same step is active or has completed for file {task.media_file_id}"
                    )
                    continue
                stats["tasks_reset"] += 1

            except Exception as e:
                logger.error(f"Error resetting false-positive task {task.id}: {e}")
                db.rollback()

        try:
            db.commit()
            if stats["tasks_reset"] > 0:
                logger.info(f"Reset {stats['tasks_reset']} false-positive failed tasks")
        except Exception as e:
            logger.error(f"Error committing false-positive task recovery: {e}")
            db.rollback()
            return {"tasks_reset": 0}

        return stats
