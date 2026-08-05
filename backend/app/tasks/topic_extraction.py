"""
Celery Task for AI Tag and Collection Suggestions

Automatically generates AI-powered tag and collection suggestions from
transcripts after transcription completes. Only runs if LLM provider
is configured for the user.
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
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        try:
            # Get media file from database
            media_file = get_file_by_uuid(db, file_uuid)
            if not media_file:
                raise ValueError(f"Media file with UUID {file_uuid} not found")

            file_id = int(media_file.id)
            user_id = int(media_file.user_id)

            logger.info(f"Starting topic extraction for file {file_id} (user {user_id})")

            # Send initial processing notification
            send_topic_extraction_notification(
                user_id=user_id,
                file_id=file_id,
                status="processing",
                message="Preparing AI analysis...",
            )

            # Create topic extraction service
            extraction_service = TopicExtractionService.create_from_settings(user_id=user_id, db=db)

            if not extraction_service:
                logger.info(f"LLM not configured for user {user_id}, skipping topic extraction")
                # Send notification that LLM is not configured
                send_topic_extraction_notification(
                    user_id=user_id,
                    file_id=file_id,
                    status="not_configured",
                    message="Topic extraction not available - no LLM provider configured in settings",
                )
                return {
                    "status": "skipped",
                    "reason": "LLM not configured",
                }

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

            # Topic extraction posts the transcript to a third-party provider, so it
            # honours redact_before_llm the same way summarization and speaker ID do.
            try:
                redaction_cfg = resolve_llm_masking(db, media_file)
            except RedactionNotReadyError as not_ready:
                defer_for_redaction(self, not_ready)
                raise  # unreachable — defer_for_redaction always raises

            # Extract topics with progress callback
            suggestion = extraction_service.extract_topics(
                media_file_id=file_id,
                force_regenerate=force_regenerate,
                progress_callback=notify_progress,
                redaction_cfg=redaction_cfg,
            )

            if not suggestion:
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

            # Send notification that AI processing is complete, now storing results
            send_topic_extraction_notification(
                user_id=user_id,
                file_id=file_id,
                status="processing",
                message="Saving AI suggestions...",
            )

            # Count suggestions
            tag_count = len(suggestion.suggested_tags or [])
            collection_count = len(suggestion.suggested_collections or [])

            # Send completion notification
            send_topic_extraction_notification(
                user_id=user_id,
                file_id=file_id,
                status="completed",
                message=f"Found {tag_count} tags and {collection_count} collections",
                suggestion_id=str(suggestion.uuid),
            )

            # Check if this file is part of a batch and trigger grouping
            try:
                if media_file.upload_batch_id:
                    from app.models.upload_batch import UploadBatch

                    batch = (
                        db.query(UploadBatch)
                        .filter(UploadBatch.id == media_file.upload_batch_id)
                        .first()
                    )
                    if batch and batch.grouping_status == "pending":
                        # Check if all batch files have completed topic extraction
                        from app.models.media import MediaFile
                        from app.models.topic import TopicSuggestion

                        batch_file_id_rows = (
                            db.query(MediaFile.id)
                            .filter(MediaFile.upload_batch_id == batch.id)
                            .all()
                        )
                        batch_file_ids = [r[0] for r in batch_file_id_rows]
                        completed_count = (
                            db.query(TopicSuggestion)
                            .filter(TopicSuggestion.media_file_id.in_(batch_file_ids))
                            .count()
                        )
                        if completed_count >= len(batch_file_ids) and len(batch_file_ids) >= 2:
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

            logger.info(
                f"Successfully extracted {tag_count} tags and {collection_count} collections for file {file_id}"
            )

            return {
                "status": "completed",
                "suggestion_id": str(suggestion.uuid),
                "tag_count": tag_count,
                "collection_count": collection_count,
            }

        except Retry:
            # Celery signals deferral with an exception that subclasses Exception,
            # so the handler below would otherwise report it as a failure.
            raise
        except Exception as e:
            error_msg = f"Error extracting topics: {str(e)}"
            logger.error(f"{error_msg} for file {file_uuid}")

            # Send failure notification
            try:
                media_file = get_file_by_uuid(db, file_uuid)
                if media_file:
                    send_topic_extraction_notification(
                        user_id=int(media_file.user_id),
                        file_id=int(media_file.id),
                        status="failed",
                        message=error_msg,
                    )
            except Exception as notify_err:
                # Log but don't raise - notification failures shouldn't mask the original error
                logger.debug(f"Failed to send topic extraction failure notification: {notify_err}")

            return {
                "status": "failed",
                "error": error_msg,
            }


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
