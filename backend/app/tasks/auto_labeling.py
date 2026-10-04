"""
Celery tasks for auto-labeling: batch grouping and retroactive apply.
"""

import contextlib
import logging
import time

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_AUTO_LABEL_STATUS
from app.core.constants import NLPPriority
from app.db.session_utils import session_scope
from app.services.notification_service import send_file_cache_invalidation

logger = logging.getLogger(__name__)


def send_auto_label_notification(
    user_id: int,
    status: str,
    message: str,
    data: dict | None = None,
    file_id: str = "auto_label_batch",
) -> bool:
    """Send auto-label status notification via WebSocket."""
    from app.services.notification_service import send_task_notification

    return send_task_notification(
        user_id,
        NOTIFICATION_TYPE_AUTO_LABEL_STATUS,
        status=status,
        message=message,
        extra={"file_id": file_id, **(data or {})},
    )


@celery_app.task(name="ai.group_batch_files", priority=NLPPriority.BACKGROUND)
def group_batch_files_task(batch_id: int, user_id: int):
    """Group batch files by shared topics into collections.

    Triggered after all files in a batch complete topic extraction.
    """
    with session_scope() as db:
        try:
            from app.services.auto_label_service import AutoLabelService

            service = AutoLabelService(db)

            # Check user settings
            user_settings = service.get_user_auto_label_settings(user_id)
            if not user_settings.get("enabled") or not user_settings.get("bulk_grouping_enabled"):
                logger.info(f"Batch grouping disabled for user {user_id}, skipping")
                return {"status": "skipped", "reason": "disabled"}

            send_auto_label_notification(
                user_id=user_id,
                status="processing",
                message="Grouping files by shared topics...",
                file_id="batch_grouping",
            )

            result = service.group_batch_by_topics(batch_id, user_id)

            send_auto_label_notification(
                user_id=user_id,
                status="completed",
                message=f"Created {result['collections_created']} collections from shared topics",
                data=result,
                file_id="batch_grouping",
            )

            return {"status": "completed", **result}

        except Exception as e:
            logger.error(f"Error in batch grouping task: {e}", exc_info=True)
            send_auto_label_notification(
                user_id=user_id,
                status="failed",
                message="Batch file grouping failed",
                file_id="batch_grouping",
            )
            return {"status": "failed", "error": str(e)}


AUTO_LABEL_BATCH_SIZE = 10
_PROGRESS_KEY = "auto_label_progress:{user_id}"
_LOCK_KEY = "auto_label_lock:{user_id}"


def pending_suggestion_ids(
    db,
    user_id: int,
    file_uuids: list[str] | None = None,
    *,
    tenant_scoped: bool = False,
    organization_id: int | None = None,
) -> list[int]:
    """IDs of ``user_id``'s suggestions not yet auto-applied.

    With ``tenant_scoped`` only suggestions on files of tenant ``organization_id``
    (None = org-less files) are returned, so a run started in one workspace never
    labels files that belong to another.
    """
    from app.models.media import MediaFile
    from app.models.topic import TopicSuggestion
    from app.utils.db_helpers import org_stamp_is

    query = db.query(TopicSuggestion.id).filter(
        TopicSuggestion.user_id == user_id,
        TopicSuggestion.auto_apply_completed_at.is_(None),
    )
    if tenant_scoped:
        query = query.join(MediaFile, MediaFile.id == TopicSuggestion.media_file_id).filter(
            org_stamp_is(MediaFile.organization_id, organization_id)
        )
    if file_uuids:
        file_ids = [r[0] for r in db.query(MediaFile.id).filter(MediaFile.uuid.in_(file_uuids))]
        query = query.filter(TopicSuggestion.media_file_id.in_(file_ids))
    return [r[0] for r in query.all()]


@celery_app.task(name="ai.retroactive_auto_label", priority=NLPPriority.BACKGROUND)
def retroactive_auto_label_task(
    user_id: int,
    file_uuids: list[str] | None = None,
    tenant_scoped: bool = False,
    organization_id: int | None = None,
):
    """Coordinator: dispatch parallel batch workers for retroactive auto-labeling.

    Partitions pending suggestions into batches and dispatches
    auto_label_batch_task workers to process them in parallel on
    the NLP queue.

    Args:
        user_id: User ID
        file_uuids: Optional list of specific file UUIDs to process
        tenant_scoped: Restrict to files of tenant ``organization_id``
        organization_id: Active tenant of the request (None = personal)
    """
    from app.core.redis import get_redis

    redis = get_redis()
    lock_key = _LOCK_KEY.format(user_id=user_id)

    # Guard against concurrent runs
    if not redis.set(lock_key, "1", ex=3600, nx=True):
        logger.info(f"Auto-label already running for user {user_id}")
        return {"status": "already_running"}

    try:
        with session_scope() as db:
            from app.services.auto_label_service import AutoLabelService

            service = AutoLabelService(db)
            user_settings = service.get_user_auto_label_settings(user_id)
            threshold = user_settings.get("confidence_threshold", 0.75)

            suggestion_ids = pending_suggestion_ids(
                db,
                user_id,
                file_uuids,
                tenant_scoped=tenant_scoped,
                organization_id=organization_id,
            )

        total = len(suggestion_ids)
        if total == 0:
            redis.delete(lock_key)
            send_auto_label_notification(
                user_id=user_id,
                status="completed",
                message="No pending suggestions to apply",
                data={"files_processed": 0, "tags_applied": 0, "collections_applied": 0},
                file_id="retroactive_apply",
            )
            return {"status": "completed", "total": 0}

        # Initialize Redis progress hash
        progress_key = _PROGRESS_KEY.format(user_id=user_id)
        redis.delete(progress_key)
        redis.hset(
            progress_key,
            mapping={
                "total": total,
                "processed": 0,
                "tags_applied": 0,
                "collections_applied": 0,
                "errors": 0,
                "start_time": time.time(),
            },
        )
        redis.expire(progress_key, 7200)

        send_auto_label_notification(
            user_id=user_id,
            status="processing",
            message=f"Starting auto-labeling of {total} files...",
            data={"total": total, "processed": 0, "progress": 0},
            file_id="retroactive_apply",
        )

        # Partition into batches and dispatch
        batches = [
            suggestion_ids[i : i + AUTO_LABEL_BATCH_SIZE]
            for i in range(0, total, AUTO_LABEL_BATCH_SIZE)
        ]
        for batch in batches:
            auto_label_batch_task.apply_async(
                args=[batch, user_id, threshold],
                priority=NLPPriority.BACKGROUND,
            )

        logger.info(
            f"Auto-label coordinator dispatched {len(batches)} batches "
            f"({total} suggestions) for user {user_id}"
        )
        return {"status": "dispatched", "total": total, "batches": len(batches)}

    except Exception as e:
        logger.error(f"Error in auto-label coordinator: {e}", exc_info=True)
        with contextlib.suppress(Exception):
            redis.delete(lock_key)
        send_auto_label_notification(
            user_id=user_id,
            status="failed",
            message="Auto-labeling failed to start",
            file_id="retroactive_apply",
        )
        return {"status": "failed", "error": str(e)}


@celery_app.task(name="ai.auto_label_batch", priority=NLPPriority.BACKGROUND)
def auto_label_batch_task(
    suggestion_ids: list[int],
    user_id: int,
    confidence_threshold: float,
):
    """Process a batch of auto-label suggestions.

    Each batch worker creates its own DB session for thread safety.
    Progress is tracked atomically via Redis HINCRBY.
    """
    from app.core.redis import get_redis

    redis = get_redis()
    progress_key = _PROGRESS_KEY.format(user_id=user_id)
    lock_key = _LOCK_KEY.format(user_id=user_id)

    batch_result = {"processed": 0, "tags_applied": 0, "collections_applied": 0, "errors": 0}

    with session_scope() as db:
        from app.models.media import MediaFile
        from app.models.topic import TopicSuggestion
        from app.services.auto_label_service import AutoLabelService

        service = AutoLabelService(db)
        user_settings = service.get_user_auto_label_settings(user_id)
        apply_tags = user_settings.get("tags_enabled", True)
        apply_collections = user_settings.get("collections_enabled", True)

        # Batch-fetch all suggestions and their media files (avoids N+1)
        suggestions_batch = (
            db.query(TopicSuggestion).filter(TopicSuggestion.id.in_(suggestion_ids)).all()
        )
        suggestion_by_id = {s.id: s for s in suggestions_batch}
        media_file_ids_needed = {s.media_file_id for s in suggestions_batch if s.media_file_id}
        media_files_batch = (
            db.query(MediaFile).filter(MediaFile.id.in_(media_file_ids_needed)).all()
            if media_file_ids_needed
            else []
        )
        media_file_by_id = {mf.id: mf for mf in media_files_batch}

        for suggestion_id in suggestion_ids:
            try:
                suggestion = suggestion_by_id.get(suggestion_id)
                if not suggestion:
                    continue

                media_file = media_file_by_id.get(suggestion.media_file_id)
                if not media_file:
                    continue

                apply_result = service.auto_apply_suggestions(
                    media_file=media_file,
                    suggestion=suggestion,
                    user_id=user_id,
                    confidence_threshold=confidence_threshold,
                    apply_tags=apply_tags,
                    apply_collections=apply_collections,
                )

                batch_result["processed"] += 1
                batch_result["tags_applied"] += len(apply_result["auto_applied_tags"])
                batch_result["collections_applied"] += len(apply_result["auto_applied_collections"])

                # Notify frontend to refresh this specific file
                send_file_cache_invalidation(user_id, str(media_file.uuid))

            except Exception as e:
                logger.error(f"Error processing suggestion {suggestion_id}: {e}")
                batch_result["errors"] += 1
                with contextlib.suppress(Exception):
                    db.rollback()

    # Atomically update global progress
    processed = redis.hincrby(progress_key, "processed", batch_result["processed"])
    redis.hincrby(progress_key, "tags_applied", batch_result["tags_applied"])
    redis.hincrby(progress_key, "collections_applied", batch_result["collections_applied"])
    redis.hincrby(progress_key, "errors", batch_result["errors"])
    total = int(redis.hget(progress_key, "total") or 0)

    # Compute ETA
    eta_seconds = None
    start_time_raw = redis.hget(progress_key, "start_time")
    if start_time_raw and processed > 0 and total > 0:
        elapsed = time.time() - float(start_time_raw)
        remaining = total - processed
        rate = processed / elapsed if elapsed > 0 else 0
        eta_seconds = round(remaining / rate) if rate > 0 else None

    # Send progress notification
    progress_pct = int((processed / total) * 100) if total > 0 else 0
    send_auto_label_notification(
        user_id=user_id,
        status="processing",
        message=f"Processing {processed}/{total}",
        data={
            "processed": processed,
            "total": total,
            "progress": progress_pct,
            "eta_seconds": eta_seconds,
        },
        file_id="retroactive_apply",
    )

    # Check if this is the last batch
    if processed >= total:
        tags = int(redis.hget(progress_key, "tags_applied") or 0)
        collections = int(redis.hget(progress_key, "collections_applied") or 0)
        errors = int(redis.hget(progress_key, "errors") or 0)

        send_auto_label_notification(
            user_id=user_id,
            status="completed",
            message=(
                f"Auto-labeled {processed} files: {tags} tags, {collections} collections applied"
            ),
            data={
                "files_processed": processed,
                "tags_applied": tags,
                "collections_applied": collections,
                "errors": errors,
            },
            file_id="retroactive_apply",
        )

        # Cleanup Redis keys
        redis.delete(progress_key, lock_key)

        logger.info(
            f"Auto-label complete for user {user_id}: "
            f"{processed} files, {tags} tags, {collections} collections"
        )

    return batch_result
