"""Celery task for re-indexing transcripts with chunk-level embeddings."""

import contextlib
import logging
from typing import Any

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import NOTIFICATION_TYPE_REINDEX_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_REINDEX_PROGRESS
from app.core.constants import CPUPriority
from app.core.redis import get_redis
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def _ensure_neural_pipeline_ready() -> bool:
    """Ensure neural search pipeline is ready for indexing.

    Returns:
        True if neural search is available and pipeline is ready.
    """
    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        return False

    try:
        from app.services.search.indexing_service import ensure_neural_ingest_pipeline
        from app.services.search.indexing_service import reset_neural_pipeline_state

        # Reset state to force re-check
        reset_neural_pipeline_state()

        if ensure_neural_ingest_pipeline():
            logger.info("Neural ingest pipeline ready for reindexing")
            return True
        else:
            logger.warning("Neural ingest pipeline not available")
            return False
    except Exception as e:
        logger.warning(f"Could not setup neural pipeline: {e}")
        return False


def _handle_model_switch(model_id: str) -> dict[str, Any] | None:
    """Switch embedding model if valid. Returns error dict on failure, None on success.

    Only handles OpenSearch neural models.
    """
    from app.core.constants import OPENSEARCH_EMBEDDING_MODELS
    from app.services.search.indexing_service import recreate_index_for_dimension

    if model_id in OPENSEARCH_EMBEDDING_MODELS:
        model_info = OPENSEARCH_EMBEDDING_MODELS[model_id]
        new_dimension: int = model_info["dimension"]  # type: ignore[assignment]

        # Recreate index with new dimension if needed
        if not recreate_index_for_dimension(new_dimension):
            logger.error(f"Failed to recreate index for dimension {new_dimension}")
            return {"error": "Failed to recreate index for new model dimension"}

        logger.info(f"Worker using neural model {model_id} ({new_dimension}d)")
        return None

    logger.warning(f"Unknown model_id: {model_id}, using current model")
    return None


def _extract_file_metadata(db: Any, media_file: Any) -> dict[str, Any] | None:
    """Extract segments and metadata from a media file for indexing.

    Returns:
        Dict with segments, title, speakers, tags, and other metadata,
        or None if the file has no segments.
    """
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment
    from app.services.permission_service import PermissionService

    file_id = int(media_file.id)
    file_uuid = str(media_file.uuid)

    # Load transcript segments
    segments = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
        .all()
    )

    if not segments:
        logger.info(f"No segments for file {file_uuid}, skipping")
        return None

    # Batch-fetch all speakers for this file (avoid N+1 queries)
    speaker_ids = {seg.speaker_id for seg in segments if seg.speaker_id}
    speakers_map: dict[int, str] = {}
    if speaker_ids:
        spk_rows = (
            db.query(Speaker.id, Speaker.name, Speaker.display_name)
            .filter(Speaker.id.in_(speaker_ids))
            .all()
        )
        speakers_map = {r.id: r.display_name or r.name or "Unknown" for r in spk_rows}

    # Convert ORM objects to dicts
    segment_dicts = []
    for seg in segments:
        speaker_name = speakers_map.get(seg.speaker_id, "Unknown") if seg.speaker_id else "Unknown"
        seg_dict = {
            "start": float(seg.start_time),
            "end": float(seg.end_time),
            "text": seg.text or "",
            "speaker": speaker_name,
        }
        if seg.words:
            seg_dict["words"] = seg.words  # Already a list of dicts from JSONB
        segment_dicts.append(seg_dict)

    # Get unique speaker names
    speaker_names: list[str] = list(
        set(str(s["speaker"]) for s in segment_dicts if s["speaker"] != "Unknown")
    )

    # Get tags via FileTag -> Tag relationship
    tag_names = []
    if hasattr(media_file, "file_tags") and media_file.file_tags:
        tag_names = [ft.tag.name for ft in media_file.file_tags if ft.tag]

    # Get collection IDs via CollectionMember relationship
    collection_id_list = []
    if hasattr(media_file, "collection_memberships") and media_file.collection_memberships:
        collection_id_list = [int(cm.collection_id) for cm in media_file.collection_memberships]

    # Compute full access list (owner + shared users/groups) so reindex
    # preserves share grants instead of resetting to owner-only
    accessible_user_ids = PermissionService.get_users_with_file_access(db, file_id)

    return {
        "file_id": file_id,
        "file_uuid": file_uuid,
        "segments": segment_dicts,
        "title": media_file.title or media_file.filename or f"File {file_id}",
        "upload_time": (media_file.upload_time.isoformat() if media_file.upload_time else None),
        "language": media_file.language or "en",
        "speakers": speaker_names,
        "tags": tag_names,
        "content_type": media_file.content_type or "",
        "duration": media_file.duration,
        "file_size": media_file.file_size,
        "collection_ids": collection_id_list,
        "accessible_user_ids": accessible_user_ids,
        "organization_id": media_file.organization_id,
    }


def _refresh_index_and_clear_cache() -> None:
    """Refresh the search index and clear the search cache."""
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
            logger.info("Refreshed search index after reindex completion")
    except Exception as e:
        logger.warning(f"Index refresh after reindex failed: {e}")

    try:
        from app.services.search.hybrid_search_service import clear_search_cache

        clear_search_cache()
    except Exception as e:
        logger.warning(f"Failed to clear search cache: {e}")


def _set_bulk_indexing_mode() -> None:
    """Temporarily disable refresh_interval for faster bulk ingestion.

    During large reindex operations, the default 1-second refresh creates
    unnecessary Lucene segments. Explicit _periodic_refresh() calls still
    work independently of this setting.
    """
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.put_settings(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={"index": {"refresh_interval": "-1"}},
            )
            logger.info("Set refresh_interval=-1 for bulk reindex")
    except Exception as e:
        logger.warning(f"Could not set bulk indexing mode: {e}")


def _restore_normal_mode() -> None:
    """Restore normal refresh_interval after bulk ingestion."""
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.put_settings(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={"index": {"refresh_interval": "1s"}},
            )
            logger.info("Restored refresh_interval=1s after reindex")
    except Exception as e:
        logger.warning(f"Could not restore normal mode: {e}")


def _force_merge_after_reindex() -> None:
    """Force merge to consolidate Lucene segments after a full reindex.

    For HNSW kNN, each segment has its own graph — fewer segments means
    better recall. This is only called after a full reindex, not per-file.
    """
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.forcemerge(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                max_num_segments=1,
                params={"wait_for_completion": "false"},
            )
            logger.info("Force merge initiated (running in background)")
    except Exception as e:
        logger.warning(f"Force merge after reindex failed (non-fatal): {e}")


def _periodic_refresh(files_since_refresh: int) -> int:
    """Trigger a Lucene segment flush if enough files have been indexed.

    Called periodically during large reindex operations to keep search
    results fresh rather than waiting until the very end.

    Args:
        files_since_refresh: Number of files indexed since the last refresh.

    Returns:
        0 if refresh was triggered (counter reset), otherwise the unchanged count.
    """
    interval = settings.SEARCH_REINDEX_REFRESH_INTERVAL
    if files_since_refresh < interval:
        return files_since_refresh

    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
            logger.info(f"Periodic index refresh after {files_since_refresh} files")
    except Exception as e:
        logger.warning(f"Periodic index refresh failed: {e}")

    return 0


def _check_and_recreate_stale_index() -> None:
    """Check if the search index has a stale schema and recreate if needed.

    When _INDEX_VERSION bumps (new analyzers, fields, etc.), the existing
    index mapping is incompatible. A full reindex is the right time to
    recreate the index with the latest mapping.

    Recreating means **deleting** the chunks index, and the reindex that follows
    only repopulates the calling user's files — so every other user's chunks are
    gone until they each run their own reindex. Three refusals bound that:

    - **No ``_meta`` block at all** → the index's provenance is unknown, so its
      version cannot be compared and it is left alone. A missing ``_meta`` used
      to read as version 0, i.e. unconditionally destroy.
    - **Unreadable embedding dimension** → abort. Defaulting to 384 gave a 768d
      deployment a wrong-dimension index in which *every* subsequent write fails.
    - **Index body built before the delete**, and a create failure is followed by
      an immediate rebuild attempt from the mapping just read, because the
      alternative state is no index at all.
    """
    try:
        from app.services.opensearch_service import opensearch_client
        from app.services.search.indexing_service import _INDEX_VERSION
        from app.services.search.indexing_service import _get_index_body_with_dimension

        if not opensearch_client:
            return

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        if not opensearch_client.indices.exists(index=index_name):
            return

        # Read stored version from index _meta
        mapping = opensearch_client.indices.get_mapping(index=index_name)
        index_mappings = mapping.get(index_name, {}).get("mappings", {})
        meta = index_mappings.get("_meta", {})
        stored_version = meta.get("version")

        if stored_version is None:
            logger.warning(
                f"Index '{index_name}' carries no _meta version. Refusing to recreate it: "
                "an index of unknown provenance may hold every user's chunks, and only "
                "the calling user's files get reindexed afterwards. Fix the mapping's "
                "_meta by hand if this index really is stale."
            )
            return

        if stored_version >= _INDEX_VERSION:
            logger.debug(
                f"Index '{index_name}' is at version {stored_version}, no recreation needed"
            )
            return

        logger.warning(
            f"Index '{index_name}' is version {stored_version}, "
            f"latest is {_INDEX_VERSION}. Recreating index with updated mapping."
        )

        # Get current dimension before deleting. No default: a guessed dimension
        # produces an index that rejects every write it is asked to accept.
        current_dim = index_mappings.get("properties", {}).get("embedding", {}).get("dimension")
        if not isinstance(current_dim, int) or current_dim <= 0:
            logger.error(
                f"Refusing to recreate '{index_name}': its embedding dimension could not "
                f"be read from the mapping (got {current_dim!r}). Recreating with a "
                "guessed dimension makes every subsequent write fail."
            )
            return

        # Build the new body BEFORE anything is destroyed, so a body-construction
        # failure cannot strand the deployment with no index.
        index_body = _get_index_body_with_dimension(current_dim)

        # Remove alias if it exists
        alias_name = "transcript_search"
        try:
            if opensearch_client.indices.exists_alias(name=alias_name):
                opensearch_client.indices.delete_alias(index=index_name, name=alias_name)
        except Exception as e:
            logger.debug(f"Could not remove alias '{alias_name}': {e}")

        opensearch_client.indices.delete(index=index_name)

        # The create gets its own handler: sharing one `except` with the delete
        # meant a create failure left the deployment with NO index and a single
        # warning line. Fall back to re-creating the index we just read, so the
        # write path keeps working even if the new mapping is rejected.
        try:
            opensearch_client.indices.create(index=index_name, body=index_body)
        except Exception as create_err:
            logger.error(
                f"Creating '{index_name}' with version {_INDEX_VERSION} failed after the "
                f"old index was deleted: {create_err}. Restoring the previous mapping."
            )
            opensearch_client.indices.create(index=index_name, body={"mappings": index_mappings})
            logger.warning(
                f"Restored '{index_name}' with its previous mapping (version "
                f"{stored_version}); the schema upgrade did NOT happen."
            )
            return

        # Recreate alias
        opensearch_client.indices.put_alias(index=index_name, name=alias_name)

        logger.info(f"Recreated index '{index_name}' with version {_INDEX_VERSION}")

    except Exception as e:
        logger.error(f"Failed to check/recreate stale index: {e}")


def _is_cancellation_requested(user_id: int) -> bool:
    """Check if a reindex cancellation has been requested via Redis.

    Args:
        user_id: The user whose reindex to check.

    Returns:
        True if cancellation was requested.
    """
    try:
        return bool(get_redis().get(f"reindex_cancel:{user_id}"))
    except Exception as e:
        logger.warning(f"Could not check cancellation flag: {e}")
        return False


def _clear_cancellation_flag(user_id: int) -> None:
    """Clear the reindex cancellation flag in Redis.

    Args:
        user_id: The user whose cancellation flag to clear.
    """
    try:
        get_redis().delete(f"reindex_cancel:{user_id}")
    except Exception as e:
        logger.warning(f"Could not clear cancellation flag: {e}")


_REINDEX_STATE_KEY = "reindex_state:{user_id}"
_REINDEX_UUIDS_KEY = "reindex_uuids:{user_id}"


def _clear_stale_progress(user_id: int) -> None:
    """Clear stale reindex progress and coordination state from Redis.

    Called at the start of a new reindex to ensure no leftover state from
    a previous run (whether it completed, failed, or was interrupted)
    contaminates the new run's progress tracking.
    """
    try:
        redis = get_redis()
        state_key = _REINDEX_STATE_KEY.format(user_id=user_id)
        uuids_key = _REINDEX_UUIDS_KEY.format(user_id=user_id)
        progress_key = f"task_progress:reindex:{user_id}"
        redis.delete(state_key, uuids_key, progress_key)
    except Exception as e:
        logger.warning(f"Could not clear stale reindex state: {e}")


def _send_reindex_progress(redis_client: Any, user_id: int, tracker: Any | None = None) -> None:
    """Read global progress from Redis and send a WebSocket notification.

    Uses ProgressTracker for EWMA-smoothed ETA when available.
    """
    state = redis_client.hgetall(_REINDEX_STATE_KEY.format(user_id=user_id))
    if not state:
        return
    indexed = int(state.get(b"indexed", state.get("indexed", 0)))
    total = int(state.get(b"total", state.get("total", 0)))
    failed = int(state.get(b"failed", state.get("failed", 0)))

    # Use ProgressTracker for EWMA ETA if available
    eta_seconds = None
    message = f"Indexed {indexed} of {total} files"
    if tracker is not None:
        from app.services.progress_tracker import emit_progress_notification

        emit_progress_notification(
            tracker,
            processed=indexed,
            user_id=user_id,
            notification_type=NOTIFICATION_TYPE_REINDEX_PROGRESS,
            message=message,
            extra_data={
                "indexed_files": indexed,
                "total_files": total,
                "failed_files": failed,
            },
        )
        return

    # Fallback: send without ETA
    progress_frac = round(indexed / total, 4) if total > 0 else 0
    send_ws_event(
        user_id,
        NOTIFICATION_TYPE_REINDEX_PROGRESS,
        {
            "indexed_files": indexed,
            "total_files": total,
            "failed_files": failed,
            "progress": progress_frac,
            "eta_seconds": eta_seconds,
            "message": message,
        },
    )


@celery_app.task(bind=True, name="reindex_transcripts", priority=CPUPriority.MAINTENANCE)
def reindex_transcripts_task(
    self,
    user_id: int,
    file_uuids: list[str] | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Coordinator: dispatch parallel batch workers for re-indexing.

    Handles model switch, index recreation, bulk mode setup, then
    partitions file IDs into worker_count groups and dispatches
    reindex_batch_task for each. The last finishing worker handles
    cleanup, orphan removal, and completion notification.

    Args:
        user_id: ID of the user triggering the re-index.
        file_uuids: Optional list of specific file UUIDs to re-index.
        model_id: Optional embedding model ID to switch to.

    Returns:
        Dict with dispatch status.
    """
    from app.db.session_utils import session_scope
    from app.models.media import FileStatus
    from app.models.media import MediaFile

    task_id = self.request.id
    logger.info(f"Re-index coordinator {task_id} started for user {user_id}")

    # Prevent concurrent reindex runs for the same user
    redis_lock = get_redis()
    lock_key = f"reindex_lock:{user_id}"
    if not redis_lock.set(lock_key, task_id, nx=True, ex=3600):
        logger.warning(f"Reindex already running for user {user_id}, skipping")
        return {"status": "skipped", "message": "Reindex already in progress"}

    _clear_cancellation_flag(user_id)

    # Clear stale progress state from any previous run (failed or completed)
    _clear_stale_progress(user_id)

    if model_id:
        error = _handle_model_switch(model_id)
        if error:
            return error

    # Recreate stale index if schema version has changed
    _check_and_recreate_stale_index()

    # Reconcile index dimension with settings (handles model switch via API
    # where model_id is not passed to this task but is already saved to DB)
    try:
        from app.services.search.indexing_service import recreate_index_for_dimension
        from app.services.search.settings_service import get_search_embedding_dimension

        target_dim = get_search_embedding_dimension()
        recreate_index_for_dimension(target_dim)
    except Exception as e:
        logger.warning(f"Dimension reconciliation failed: {e}")

    # Ensure neural pipeline is ready AFTER index/dimension are reconciled,
    # so concurrent embedding queue tasks also use the correct model
    use_neural = _ensure_neural_pipeline_ready()
    if use_neural:
        logger.info("Reindex will use OpenSearch neural embedding (CPU path)")
    else:
        logger.warning("No embedding available - reindex will be text-only (BM25)")

    try:
        # Snapshot file IDs
        with session_scope() as db:
            id_query = db.query(MediaFile.id).filter(
                MediaFile.user_id == user_id,
                MediaFile.status == FileStatus.COMPLETED,
            )
            if file_uuids:
                id_query = id_query.filter(MediaFile.uuid.in_(file_uuids))
            all_file_ids = [int(row[0]) for row in id_query.order_by(MediaFile.id).all()]

        total_files = len(all_file_ids)
        if total_files == 0:
            logger.info(f"No files to re-index for user {user_id}")
            return {"total_files": 0}

        # Initialize progress tracker
        from app.services.progress_tracker import ProgressTracker

        tracker = ProgressTracker(task_type="reindex", user_id=user_id, total=total_files)
        tracker.start(message="Starting re-index...")

        # Safety: restore normal mode, then set bulk mode
        _restore_normal_mode()
        _set_bulk_indexing_mode()

        # Set up Redis state for worker coordination
        redis = get_redis()
        worker_count = settings.REINDEX_PARALLEL_WORKERS
        state_key = _REINDEX_STATE_KEY.format(user_id=user_id)
        uuids_key = _REINDEX_UUIDS_KEY.format(user_id=user_id)

        redis.delete(state_key, uuids_key)
        redis.hset(
            state_key,
            mapping={
                "total": total_files,
                "indexed": 0,
                "failed": 0,
                "skipped": 0,
                "chunks": 0,
                "workers_done": 0,
                "worker_count": worker_count,
                "mode": "cpu",
                "partial": "1" if file_uuids else "0",
            },
        )
        redis.expire(state_key, 86400)  # 24h TTL

        # Partition file IDs into worker_count groups
        partitions: list[list[int]] = [[] for _ in range(worker_count)]
        for i, file_id in enumerate(all_file_ids):
            partitions[i % worker_count].append(file_id)

        dispatched = 0
        for partition in partitions:
            if partition:
                reindex_batch_task.apply_async(
                    args=[partition, user_id],
                    priority=CPUPriority.MAINTENANCE,
                )
                dispatched += 1

        # Update worker_count to actual dispatched count
        if dispatched != worker_count:
            redis.hset(state_key, "worker_count", dispatched)

        logger.info(
            f"Re-index coordinator dispatched {dispatched} cpu workers "
            f"for {total_files} files (user {user_id})"
        )
        return {
            "status": "dispatched",
            "total_files": total_files,
            "workers": dispatched,
            "mode": "cpu",
        }

    except Exception as e:
        logger.error(f"Re-index coordinator failed: {e}")
        with contextlib.suppress(Exception):
            _restore_normal_mode()
        with contextlib.suppress(Exception):
            _clear_stale_progress(user_id)
        with contextlib.suppress(Exception):
            get_redis().delete(f"reindex_lock:{user_id}")
        return {"error": str(e)}


@celery_app.task(name="reindex_batch", priority=CPUPriority.MAINTENANCE)
def reindex_batch_task(
    file_ids: list[int],
    user_id: int,
) -> dict[str, Any]:
    """Process a partition of files for re-indexing.

    Each worker indexes its assigned files and updates Redis state
    atomically. The last worker to finish handles cleanup.
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.services.progress_tracker import ProgressTracker
    from app.services.search.indexing_service import TranscriptIndexingService

    indexing_service = TranscriptIndexingService()
    redis = get_redis()
    state_key = _REINDEX_STATE_KEY.format(user_id=user_id)
    uuids_key = _REINDEX_UUIDS_KEY.format(user_id=user_id)

    # Resume ProgressTracker from coordinator state for EWMA ETA
    total = int(redis.hget(state_key, "total") or len(file_ids))
    tracker = ProgressTracker(task_type="reindex", user_id=user_id, total=total)
    existing_state = ProgressTracker.get_state("reindex", user_id)
    if existing_state:
        tracker.resume_from_state(existing_state)
    else:
        tracker.start(message="Re-indexing...")

    local_stats = {"indexed": 0, "failed": 0, "skipped": 0, "chunks": 0}
    cancelled = False
    page_size = 50

    for batch_start in range(0, len(file_ids), page_size):
        if cancelled:
            break
        batch_ids = file_ids[batch_start : batch_start + page_size]

        with session_scope() as db:
            from sqlalchemy.orm import defer

            page_files = (
                db.query(MediaFile)
                .options(
                    defer(MediaFile.summary_data),
                    defer(MediaFile.metadata_raw),
                    defer(MediaFile.waveform_data),
                )
                .filter(MediaFile.id.in_(batch_ids))
                .order_by(MediaFile.id)
                .all()
            )

            for media_file in page_files:
                # Check cancellation between files
                if _is_cancellation_requested(user_id):
                    logger.info(f"Reindex batch cancelled for user {user_id}")
                    cancelled = True
                    break

                file_uuid = str(media_file.uuid)
                try:
                    metadata = _extract_file_metadata(db, media_file)
                    if metadata is None:
                        local_stats["skipped"] += 1
                        redis.hincrby(state_key, "indexed", 1)
                        redis.hincrby(state_key, "skipped", 1)
                        _send_reindex_progress(redis, user_id, tracker)
                        continue

                    chunk_count = indexing_service.reindex_transcript(
                        file_id=metadata["file_id"],
                        file_uuid=metadata["file_uuid"],
                        user_id=user_id,
                        segments=metadata["segments"],
                        title=metadata["title"],
                        speakers=metadata["speakers"],
                        tags=metadata["tags"],
                        upload_time=metadata["upload_time"],
                        language=metadata["language"],
                        content_type=metadata["content_type"],
                        duration=metadata["duration"],
                        file_size=metadata["file_size"],
                        collection_ids=metadata["collection_ids"],
                        accessible_user_ids=metadata.get("accessible_user_ids"),
                        organization_id=metadata.get("organization_id"),
                    )

                    local_stats["indexed"] += 1
                    local_stats["chunks"] += chunk_count

                    # Track indexed UUID for orphan cleanup
                    redis.sadd(uuids_key, file_uuid)

                    # Update global progress atomically
                    redis.hincrby(state_key, "indexed", 1)
                    redis.hincrby(state_key, "chunks", chunk_count)

                    # Send progress notification with EWMA ETA
                    _send_reindex_progress(redis, user_id, tracker)

                except Exception as e:
                    logger.error(f"Error re-indexing file {file_uuid}: {e}")
                    local_stats["failed"] += 1
                    redis.hincrby(state_key, "failed", 1)

    # Mark this worker as done
    workers_done = redis.hincrby(state_key, "workers_done", 1)
    worker_count = int(redis.hget(state_key, "worker_count") or 1)

    # Last worker handles cleanup
    if workers_done >= worker_count:
        _handle_reindex_completion(redis, user_id, state_key, uuids_key)

    return local_stats


def _handle_reindex_completion(
    redis_client: Any, user_id: int, state_key: str, uuids_key: str
) -> None:
    """Final cleanup after all reindex workers complete."""
    _restore_normal_mode()
    _refresh_index_and_clear_cache()
    _force_merge_after_reindex()

    # Orphan cleanup — only safe for FULL reindex (all files).
    # For partial reindex (specific file_uuids), the indexed set is incomplete
    # and orphan detection would incorrectly delete existing chunks.
    state = redis_client.hgetall(state_key)
    is_partial = state.get(b"partial", state.get("partial", b"0")) in (b"1", "1")

    if not is_partial:
        indexed_uuids = redis_client.smembers(uuids_key)
        if indexed_uuids:
            uuid_set = {u.decode() if isinstance(u, bytes) else u for u in indexed_uuids}
            orphan_cleaned = _cleanup_orphaned_chunks(user_id, uuid_set)
            if orphan_cleaned > 0:
                logger.info(f"Cleaned {orphan_cleaned} orphaned chunks for user {user_id}")
    else:
        logger.info(f"Skipping orphan cleanup for partial reindex (user {user_id})")

    # Read final stats
    state = redis_client.hgetall(state_key)

    def _state_int(key: str) -> int:
        return int(state.get(key.encode(), state.get(key, 0)))

    indexed = _state_int("indexed")
    total = _state_int("total")
    failed = _state_int("failed")
    chunks = _state_int("chunks")
    mode = state.get(b"mode", state.get("mode", b"cpu"))
    if isinstance(mode, bytes):
        mode = mode.decode()

    # Send a final 100% progress notification so the UI doesn't jump from e.g. 79% to complete
    effective_total = max(total, indexed, 1)
    send_ws_event(
        user_id,
        NOTIFICATION_TYPE_REINDEX_PROGRESS,
        {
            "indexed_files": effective_total,
            "total_files": effective_total,
            "failed_files": failed,
            "progress": 1.0,
            "eta_seconds": None,
            "message": "Re-indexing complete",
        },
    )

    # Complete progress tracker (use max to guard against stale/empty hash)
    with contextlib.suppress(Exception):
        from app.services.progress_tracker import ProgressTracker

        tracker = ProgressTracker(task_type="reindex", user_id=user_id, total=effective_total)
        tracker.complete(message="Re-indexing complete")

    # Send completion notification
    stats: dict[str, Any] = {
        "total_files": total,
        "indexed_files": indexed,
        "failed_files": failed,
        "total_chunks": chunks,
        "mode": mode,
    }
    send_ws_event(user_id, NOTIFICATION_TYPE_REINDEX_COMPLETE, {"stats": stats})

    # Cleanup Redis keys (including reindex lock)
    redis_client.delete(state_key, uuids_key, f"reindex_lock:{user_id}")
    _clear_cancellation_flag(user_id)

    logger.info(
        f"Re-index complete for user {user_id} ({mode}): "
        f"{indexed}/{total} files, {chunks} chunks, {failed} failures"
    )


def _cleanup_orphaned_chunks(user_id: int, indexed_file_uuids: set[str]) -> int:
    """Remove chunk documents for files that were deleted during reindex.

    Compares the set of file UUIDs in the chunks index (for this user) against
    the set we just indexed. Any file_uuid in the index but NOT in the indexed
    set is orphaned and gets cleaned up.

    Returns count of orphaned documents deleted.
    """
    from app.services.opensearch_service import opensearch_client as os_client

    if not os_client:
        return 0

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if not os_client.indices.exists(index=index_name):
            return 0

        # Get all file_uuids in the chunks index for this user
        agg_resp = os_client.search(
            index=index_name,
            body={
                "size": 0,
                "query": {"term": {"user_id": user_id}},
                "aggs": {
                    "file_uuids": {
                        "terms": {"field": "file_uuid", "size": 50000},
                    }
                },
            },
        )
        buckets = agg_resp.get("aggregations", {}).get("file_uuids", {}).get("buckets", [])
        indexed_in_os = {b["key"] for b in buckets}

        orphan_uuids = indexed_in_os - indexed_file_uuids
        if not orphan_uuids:
            return 0

        # Delete orphaned chunks
        del_resp = os_client.delete_by_query(
            index=index_name,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"user_id": user_id}},
                            {"terms": {"file_uuid": list(orphan_uuids)}},
                        ]
                    }
                }
            },
            refresh=True,
        )
        return int(del_resp.get("deleted", 0))

    except Exception as e:
        logger.warning(f"Post-reindex orphan cleanup failed: {e}")
        return 0
