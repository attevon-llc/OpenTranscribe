"""Celery task for AI tag and collection suggestions.

Automatically generates AI-powered tag and collection suggestions from
transcripts after transcription completes. Only runs if an LLM provider
is configured for the user.

**Phased, and the split is load-bearing.** Every DB session in this module is
short and Postgres-only; the provider round trip runs with **none** open. This
was the seventh LLM-in-transaction instance and the same shape as the two that
were actively wedging the live database — one ``session_scope`` wrapped the whole
task body, so Postgres sat ``idle in transaction`` for the length of the LLM call
with a full ``transcript_segment`` SELECT as its last statement. Such a
transaction holds ACCESS SHARE on ``transcript_segment``, so it queues every
``ALTER TABLE`` (i.e. any Alembic upgrade, which dev runs on backend startup),
pins the vacuum horizon on the largest table in the product, and consumes a pool
connection for its whole life. Measured on ``ai.generate_summary``: 1 h 26 m.

It could not be fixed from this module alone: ``TopicExtractionService`` was
constructed with ``db=`` and both read the transcript and wrote the
``TopicSuggestion`` row through it, so the read/LLM/write split lives in
``services/topic_extraction_service.py`` and this module only feeds it plain data.

See ``app/tasks/CLAUDE.md`` ("The session-lifetime rule") and
``summarization.py`` for the worked example this follows.
"""

import logging
from typing import Any

from celery.exceptions import Retry

from app.core.celery import celery_app
from app.core.constants import NLPPriority
from app.db.session_utils import session_scope
from app.services.redaction.llm_guard import RedactionNotReadyError
from app.services.redaction.llm_guard import defer_for_redaction
from app.services.redaction.llm_guard import resolve_llm_masking
from app.services.topic_extraction_service import TopicExtractionService

logger = logging.getLogger(__name__)


def send_topic_extraction_notification(
    user_id: int,
    file_id: int,
    status: str,
    message: str,
    suggestion_id: str | None = None,
) -> bool:
    """Send AI suggestion extraction status notification via WebSocket."""
    from app.services.notification_service import send_task_notification

    extra: dict[str, Any] = {}
    if status == "completed" and suggestion_id:
        extra["suggestion_id"] = suggestion_id

    return send_task_notification(
        user_id,
        "topic_extraction_status",
        status=status,
        message=message,
        file_id=file_id,
        extra=extra,
    )


def _load_file_context(file_uuid: str) -> dict[str, Any]:
    """Phase 1a — identity read (short session, Postgres only).

    Returns **plain data only**; no ORM instance escapes. An escaping instance
    would lazy-load during the provider call and silently reopen a transaction.
    """
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        if not media_file:
            raise ValueError(f"Media file with UUID {file_uuid} not found")
        return {
            "file_id": int(media_file.id),
            "user_id": int(media_file.user_id),
            "upload_batch_id": media_file.upload_batch_id,
        }


def _resolve_masking(file_uuid: str):
    """Phase 1b — the pre-LLM masking policy (short session, Postgres only).

    Topic extraction posts the transcript to a third-party provider, so it honours
    ``redact_before_llm`` the same way summarization and speaker ID do. The config
    is a plain dataclass, so it travels safely out of this scope.

    Raises:
        RedactionNotReadyError: Detection has not cached spans yet. It propagates
            out of the scope, so the caller defers with **no session open** —
            ``defer_for_redaction`` dispatches a Celery task and must not run
            inside a transaction.
    """
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        if not media_file:
            raise ValueError(f"Media file with UUID {file_uuid} not found")
        return resolve_llm_masking(db, media_file)


def _trigger_batch_grouping(user_id: int, upload_batch_id: int | None) -> None:
    """Dispatch batch grouping once every file in the batch has suggestions.

    Best-effort, in its own short session: the caller holds none by design.
    """
    if not upload_batch_id:
        return
    try:
        with session_scope() as db:
            from app.models.media import MediaFile
            from app.models.topic import TopicSuggestion
            from app.models.upload_batch import UploadBatch

            batch = db.query(UploadBatch).filter(UploadBatch.id == upload_batch_id).first()
            if not batch or batch.grouping_status != "pending":
                return

            batch_file_id_rows = (
                db.query(MediaFile.id).filter(MediaFile.upload_batch_id == batch.id).all()
            )
            batch_file_ids = [r[0] for r in batch_file_id_rows]
            completed_count = (
                db.query(TopicSuggestion)
                .filter(TopicSuggestion.media_file_id.in_(batch_file_ids))
                .count()
            )
            if completed_count < len(batch_file_ids) or len(batch_file_ids) < 2:
                return

            # Atomic compare-and-swap to prevent duplicate dispatches
            rows_updated = (
                db.query(UploadBatch)
                .filter(
                    UploadBatch.id == batch.id,
                    UploadBatch.grouping_status == "pending",
                )
                .update({"grouping_status": "processing"})
            )
            db.commit()
            if rows_updated > 0:
                from app.tasks.auto_labeling import group_batch_files_task

                group_batch_files_task.delay(batch.id, user_id)
                logger.info(f"Triggered batch grouping for batch {batch.id}")
    except Exception as e:
        logger.warning(f"Batch grouping check failed: {e}")


def _handle_task_error(e: Exception, file_uuid: str) -> dict[str, Any]:
    """Report a task-level failure, opening its **own** short session.

    The caller is mid-provider-call and holds no session by design.
    """
    from app.utils.uuid_helpers import get_file_by_uuid

    error_msg = f"Error extracting topics: {str(e)}"
    logger.error(f"{error_msg} for file {file_uuid}")

    try:
        with session_scope() as db:
            media_file = get_file_by_uuid(db, file_uuid)
            notify = (
                (int(media_file.user_id), int(media_file.id)) if media_file is not None else None
            )
    except Exception as notify_err:
        # Log but don't raise - notification failures shouldn't mask the original error
        logger.debug(f"Failed to resolve file for topic extraction failure notice: {notify_err}")
        notify = None

    if notify is not None:
        try:
            send_topic_extraction_notification(
                user_id=notify[0],
                file_id=notify[1],
                status="failed",
                message=error_msg,
            )
        except Exception as notify_err:
            logger.debug(f"Failed to send topic extraction failure notification: {notify_err}")

    return {
        "status": "failed",
        "error": error_msg,
    }


@celery_app.task(bind=True, name="ai.extract_topics", priority=NLPPriority.AUTO_PIPELINE)
def extract_topics_task(self, file_uuid: str, force_regenerate: bool = False):
    """
    Extract AI tag and collection suggestions from a completed transcript.

    This task runs AFTER transcription has been completed. It's typically
    triggered automatically by the transcription workflow, but can also be
    manually triggered via the API.

    Args:
        file_uuid: UUID of the MediaFile to extract suggestions from
        force_regenerate: If True, re-extract even if suggestions exist

    Returns:
        dict: Contains status, suggestion_id, tag_count, and collection_count
    """
    try:
        # Phase 1a — read (DB session open, Postgres only).
        context = _load_file_context(file_uuid)
        file_id = context["file_id"]
        user_id = context["user_id"]

        logger.info(f"Starting topic extraction for file {file_id} (user {user_id})")

        # Resolve the LLM BEFORE announcing any work. Having no provider
        # configured is a deployment choice, not a task outcome: notifying
        # about it puts a warning on every file for something the user
        # already knows and cannot fix from a notification. Announcing
        # "Preparing AI analysis..." first made it worse — that notification
        # is progressive, so it sat unresolved until the second one replaced
        # it, giving two entries per file for work that never started.
        #
        # A genuine failure — a provider that IS configured but errors or
        # returns nothing — still notifies, which is the case worth flagging.
        #
        # No session is passed: the probe resolves the user's LLM config through
        # a short session of its own, and the extraction phases open theirs.
        extraction_service = TopicExtractionService.create_from_settings(user_id=user_id)

        if not extraction_service:
            logger.info(f"LLM not configured for user {user_id}, skipping topic extraction")
            return {
                "status": "skipped",
                "reason": "LLM not configured",
            }

        # Phase 1b — read (DB session open, Postgres only).
        try:
            redaction_cfg = _resolve_masking(file_uuid)
        except RedactionNotReadyError as not_ready:
            defer_for_redaction(self, not_ready)
            raise  # unreachable — defer_for_redaction always raises

        # Send initial processing notification
        send_topic_extraction_notification(
            user_id=user_id,
            file_id=file_id,
            status="processing",
            message="Preparing AI analysis...",
        )

        # Send notification before LLM processing
        send_topic_extraction_notification(
            user_id=user_id,
            file_id=file_id,
            status="processing",
            message="Analyzing transcript with AI...",
        )

        # Create a notification callback for the service to use
        def notify_progress(message: str):
            send_topic_extraction_notification(
                user_id=user_id,
                file_id=file_id,
                status="processing",
                message=message,
            )

        # Phase 2 — the slow phase. NO DB session is held here: the service opens
        # a short read scope for the transcript, calls the provider with nothing
        # held, then reopens a short write scope for the TopicSuggestion row.
        result = extraction_service.extract_topics(
            media_file_id=file_id,
            force_regenerate=force_regenerate,
            progress_callback=notify_progress,
            redaction_cfg=redaction_cfg,
        )

        if not result:
            error_msg = "Failed to extract topics from transcript"
            logger.error(f"{error_msg} for file {file_id}")

            # Send failure notification
            send_topic_extraction_notification(
                user_id=user_id,
                file_id=file_id,
                status="failed",
                message=error_msg,
            )

            return {
                "status": "failed",
                "error": error_msg,
            }

        tag_count = result.tag_count
        collection_count = result.collection_count

        # Send completion notification
        send_topic_extraction_notification(
            user_id=user_id,
            file_id=file_id,
            status="completed",
            message=f"Found {tag_count} tags and {collection_count} collections",
            suggestion_id=result.suggestion_uuid,
        )

        # Check if this file is part of a batch and trigger grouping.
        _trigger_batch_grouping(user_id, context["upload_batch_id"])

        logger.info(
            f"Successfully extracted {tag_count} tags and {collection_count} collections "
            f"for file {file_id}"
        )

        return {
            "status": "completed",
            "suggestion_id": result.suggestion_uuid,
            "tag_count": tag_count,
            "collection_count": collection_count,
        }

    except Retry:
        # Celery signals deferral with an exception that subclasses Exception,
        # so the handler below would otherwise report it as a failure.
        raise
    except Exception as e:
        return _handle_task_error(e, file_uuid)


@celery_app.task(bind=True, name="ai.extract_topics_batch", priority=NLPPriority.ADMIN_BATCH)
def batch_extract_topics_task(self, file_uuids: list[str], force_regenerate: bool = False):
    """
    Extract AI suggestions for multiple files in batch.

    Args:
        file_uuids: List of file UUIDs to process
        force_regenerate: If True, re-extract even if suggestions exist

    Returns:
        dict: Contains total, completed, failed, skipped counts and details for each file
    """
    results: dict[str, int | list[dict[str, str | None]]] = {
        "total": len(file_uuids),
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "details": [],
    }

    # Dispatch each file as a separate Celery task so multiple workers
    # can process them in parallel instead of running sequentially in
    # this single worker.
    async_results = []
    for file_uuid in file_uuids:
        async_result = extract_topics_task.delay(file_uuid, force_regenerate)
        async_results.append((file_uuid, async_result))

    for file_uuid, async_result in async_results:
        try:
            result = async_result.get(timeout=600)  # 10 min per file

            if result["status"] == "completed":
                results["completed"] = int(results["completed"]) + 1  # type: ignore[arg-type]
            elif result["status"] == "failed":
                results["failed"] = int(results["failed"]) + 1  # type: ignore[arg-type]
            elif result["status"] == "skipped":
                results["skipped"] = int(results["skipped"]) + 1  # type: ignore[arg-type]

            details_list = results["details"]
            assert isinstance(details_list, list)
            details_list.append(
                {
                    "file_uuid": file_uuid,
                    "status": result["status"],
                    "suggestion_id": result.get("suggestion_id"),
                    "error": result.get("error"),
                }
            )

        except Exception as e:
            logger.error(f"Error in batch processing file {file_uuid}: {e}")
            results["failed"] = int(results["failed"]) + 1  # type: ignore[arg-type]
            details_list = results["details"]
            assert isinstance(details_list, list)
            details_list.append(
                {
                    "file_uuid": file_uuid,
                    "status": "failed",
                    "error": str(e),
                }
            )

    logger.info(
        f"Batch topic extraction completed: {results['completed']} succeeded, "
        f"{results['failed']} failed, {results['skipped']} skipped"
    )

    return results
