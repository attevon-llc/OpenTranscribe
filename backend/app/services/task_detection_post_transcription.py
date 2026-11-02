"""Post-transcription completeness detection (mixin of ``TaskDetectionService``).

Split out of ``task_detection_service``.
"""

import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.task_config import RECOVERED_AFTER_STUCK
from app.core.task_config import TaskRecoveryConfig
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)


@dataclass
class IncompletePostTranscriptionFile:
    """A COMPLETED file that is missing one or more post-transcription results."""

    media_file_id: int
    file_uuid: str
    user_id: int
    missing_summary: bool = False
    missing_topics: bool = False
    missing_speaker_id: bool = False
    missing_analytics: bool = False
    missing_search_indexing: bool = False
    llm_configured: bool = False


class TaskDetectionPostTranscriptionMixin:
    """Detection of COMPLETED files missing post-transcription results, and stuck LLM tasks."""

    config: TaskRecoveryConfig

    def identify_incomplete_post_transcription_files(
        self, db: Session, batch_size: int = 10
    ) -> list[IncompletePostTranscriptionFile]:
        """
        Identify COMPLETED files missing post-transcription results.

        Checks for missing summaries, topics, speaker identifications, and
        analytics on files that completed transcription at least 30 minutes ago.
        Uses batch queries to avoid N+1 problems.

        Args:
            db: Database session
            batch_size: Maximum files to return per cycle

        Returns:
            List of IncompletePostTranscriptionFile with missing flags set
        """
        from app.models.media import Analytics
        from app.models.speaker import Speaker
        from app.models.topic import TopicSuggestion

        now = datetime.now(UTC)
        maturity_cutoff = now - timedelta(minutes=30)

        # Query candidates: COMPLETED files that finished >30 min ago
        candidates = (
            db.query(
                MediaFile.id,
                MediaFile.uuid,
                MediaFile.user_id,
                MediaFile.summary_status,
            )
            .filter(
                MediaFile.status == FileStatus.COMPLETED,
                MediaFile.completed_at < maturity_cutoff,
            )
            .order_by(MediaFile.completed_at.asc())
            .limit(batch_size * 3)
            .all()
        )

        if not candidates:
            return []

        file_ids = [c.id for c in candidates]

        # Cache LLM config per unique user_id. This must be the same answer the LLM
        # tasks reach when they build their client: a looser check (e.g. "is
        # LLM_PROVIDER set?") flags every file on every sweep while each dispatched
        # task finds no usable provider and skips.
        unique_user_ids = {c.user_id for c in candidates}
        llm_configured_by_user: dict[int, bool] = {}
        for uid in unique_user_ids:
            llm_configured_by_user[uid] = LLMService.is_configured_for_user(db, uid)

        # A user who switched an automatic AI step off (issue #1148) must not have it
        # re-queued by this sweep: it would silently override the choice, and because the
        # step never produces a row it would be re-detected as "missing" every 10 minutes.
        from app.services.auto_llm_settings import resolve_auto_llm_flags_for_users

        auto_llm_by_user = resolve_auto_llm_flags_for_users(db, sorted(unique_user_ids))

        # Batch-fetch existence: files with topics
        files_with_topics = set(
            row[0]
            for row in db.query(TopicSuggestion.media_file_id)
            .filter(TopicSuggestion.media_file_id.in_(file_ids))
            .all()
        )

        # Batch-fetch existence: files with analytics
        files_with_analytics = set(
            row[0]
            for row in db.query(Analytics.media_file_id)
            .filter(Analytics.media_file_id.in_(file_ids))
            .all()
        )

        # Batch-fetch existence: files with LLM speaker identifications.
        # ``suggestion_source`` is a LEGACY / best-effort existence proxy for "has LLM
        # speaker ID already run on this file" -- it survives accept/reject (accept/reject
        # in `api/endpoints/speakers.py`, `_apply_verification_on_display_name` /
        # `_reject_speaker_suggestion`, deliberately leave this column set for exactly this
        # reader; only `suggested_name`/`confidence` are cleared), but it does NOT survive
        # every write path: a later voice-match or clustering "skip" write can silently
        # overwrite it with a different provenance string, and until issue #620 there was no
        # other record. That produced a false `missing_speaker_id` and a wasted LLM
        # re-dispatch for a file the LLM had already actually processed.
        #
        # The durable signal is the Task table: a completed `speaker_identification` Task
        # row is never overwritten by anything else, unlike this column. Union the two
        # rather than replace the legacy leg outright -- this is strictly MORE permissive
        # than before (a file can only newly count as "has speaker ID", never newly count
        # as missing), so it cannot cause a mass re-dispatch, and it keeps working for
        # historical files that predate the Task-table leg ever being written.
        files_with_speaker_id_legacy = set(
            row[0]
            for row in db.query(Speaker.media_file_id)
            .filter(
                Speaker.media_file_id.in_(file_ids),
                Speaker.suggestion_source == "llm_analysis",
            )
            .all()
        )
        files_with_speaker_id_task = set(
            row[0]
            for row in db.query(Task.media_file_id)
            .filter(
                Task.media_file_id.in_(file_ids),
                Task.task_type == "speaker_identification",
                Task.status == "completed",
            )
            .all()
        )
        files_with_speaker_id = files_with_speaker_id_legacy | files_with_speaker_id_task

        # Batch-fetch files with successful search indexing (completed status)
        files_with_search_indexing = set(
            row[0]
            for row in db.query(Task.media_file_id)
            .filter(
                Task.media_file_id.in_(file_ids),
                Task.task_type == "search_indexing",
                Task.status == "completed",
            )
            .all()
        )

        # Batch-fetch recently-attempted task types for these files
        # Tasks created in the last 30 min that are pending/in_progress or recently completed
        recent_task_cutoff = now - timedelta(minutes=30)
        post_transcription_task_types = [
            "analytics",
            "speaker_identification",
            "summarization",
            "topic_extraction",
            "search_indexing",  # Track search indexing failures
        ]
        recently_attempted_rows = (
            db.query(Task.media_file_id, Task.task_type)
            .filter(
                Task.media_file_id.in_(file_ids),
                Task.task_type.in_(post_transcription_task_types),
                Task.status.in_(["pending", "in_progress"]),
            )
            .all()
        )
        # Also include tasks created recently (even if completed/failed) to avoid
        # re-dispatching tasks that just ran
        recently_created_rows = (
            db.query(Task.media_file_id, Task.task_type)
            .filter(
                Task.media_file_id.in_(file_ids),
                Task.task_type.in_(post_transcription_task_types),
                Task.created_at > recent_task_cutoff,
            )
            .all()
        )

        recently_attempted: set[tuple[int, str]] = set()
        for media_file_id, task_type in (*recently_attempted_rows, *recently_created_rows):
            if media_file_id is not None:
                recently_attempted.add((media_file_id, task_type))

        # Evaluate each candidate
        results: list[IncompletePostTranscriptionFile] = []
        for c in candidates:
            if len(results) >= batch_size:
                break

            llm_ok = llm_configured_by_user.get(c.user_id, False)

            missing_summary = (
                llm_ok
                and c.summary_status in (None, "pending", "failed")
                and (c.id, "summarization") not in recently_attempted
            )
            auto_llm = auto_llm_by_user[c.user_id]
            missing_topics = (
                llm_ok
                and auto_llm.topic_extraction
                and c.id not in files_with_topics
                and (c.id, "topic_extraction") not in recently_attempted
            )
            missing_speaker_id = (
                llm_ok
                and auto_llm.speaker_identification
                and c.id not in files_with_speaker_id
                and (c.id, "speaker_identification") not in recently_attempted
            )
            missing_analytics = (
                c.id not in files_with_analytics and (c.id, "analytics") not in recently_attempted
            )
            missing_search_indexing = (
                c.id not in files_with_search_indexing
                and (c.id, "search_indexing") not in recently_attempted
            )

            if (
                missing_summary
                or missing_topics
                or missing_speaker_id
                or missing_analytics
                or missing_search_indexing
            ):
                results.append(
                    IncompletePostTranscriptionFile(
                        media_file_id=c.id,
                        file_uuid=str(c.uuid),
                        user_id=c.user_id,
                        missing_summary=missing_summary,
                        missing_topics=missing_topics,
                        missing_speaker_id=missing_speaker_id,
                        missing_analytics=missing_analytics,
                        missing_search_indexing=missing_search_indexing,
                        llm_configured=llm_ok,
                    )
                )

        if results:
            logger.info(
                f"Identified {len(results)} COMPLETED files with incomplete "
                f"post-transcription processing"
            )

        return results

    def identify_stuck_llm_tasks(self, db: Session) -> list[Task]:
        """
        Identify LLM tasks stuck in_progress for an unreasonably long time.

        Tasks stuck for > 6 hours are considered abandoned (worker crash, network timeout).
        Normal LLM tasks complete in minutes to 1-2 hours for very long transcripts.

        Args:
            db: Database session

        Returns:
            List of stuck LLM tasks
        """
        stuck_threshold = datetime.now(UTC) - timedelta(hours=6)

        stuck_tasks: list[Task] = (
            db.query(Task)
            .filter(
                Task.status == "in_progress",
                Task.task_type.in_(["speaker_identification", "summarization", "topic_extraction"]),
                Task.created_at < stuck_threshold,
            )
            .all()
        )

        if stuck_tasks:
            logger.info(f"Identified {len(stuck_tasks)} LLM tasks stuck in progress > 6 hours")

        return stuck_tasks

    def identify_false_positive_failed_tasks(self, db: Session) -> list[Task]:
        """
        Identify tasks marked failed by overly aggressive recovery.

        These tasks have error message "Task recovered after being stuck in processing"
        but likely completed successfully. Only consider recent tasks (< 3 days old)
        to avoid retrying very old failures.

        Transcriptions are excluded (#1020). Resetting a transcription's Task row to
        pending dispatches nothing -- transcription recovery re-dispatches through
        ``schedule_file_retry`` -- and a pending row is exactly what let a superseded
        message, still waiting in the broker, pass its ownership check and run.

        The match is a prefix: ``recover_stuck_task`` appends the attempt counter a
        previous reset recorded, so the counter survives the next failure and
        ``recover_false_positive_failed_tasks`` can stop after its last attempt. It used
        to be overwritten, which reset the counter to zero and repeated forever.

        Args:
            db: Database session

        Returns:
            List of falsely failed tasks eligible for retry
        """
        recent_cutoff = datetime.now(UTC) - timedelta(days=3)

        false_positive_tasks: list[Task] = (
            db.query(Task)
            .filter(
                Task.status == "failed",
                Task.error_message.startswith(RECOVERED_AFTER_STUCK, autoescape=True),
                Task.task_type.in_(["speaker_identification", "summarization", "topic_extraction"]),
                Task.created_at > recent_cutoff,
            )
            .all()
        )

        if false_positive_tasks:
            logger.info(
                f"Identified {len(false_positive_tasks)} tasks with false-positive failure status"
            )

        return false_positive_tasks
