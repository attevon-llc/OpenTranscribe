"""Celery tasks for re-indexing transcripts with chunk-level embeddings."""

import contextlib
import logging
from typing import Any
from uuid import uuid4

from app.core.celery import celery_app
from app.core.config import settings
from app.core.queues import CPUPriority
from app.core.redis import get_redis
from app.db.session_utils import session_scope
from app.services.search.reindex_cancel import consume_pending_cancel
from app.services.search.reindex_lock import REINDEX_LOCK_KEY
from app.tasks.reindex_completion import _handle_reindex_completion
from app.tasks.reindex_index_ops import _check_and_recreate_stale_index
from app.tasks.reindex_index_ops import _ensure_neural_pipeline_ready
from app.tasks.reindex_index_ops import _handle_model_switch
from app.tasks.reindex_index_ops import _index_structure_lock
from app.tasks.reindex_index_ops import _restore_normal_mode
from app.tasks.reindex_index_ops import _set_bulk_indexing_mode
from app.tasks.reindex_run_state import _REINDEX_STATE_TTL_SECONDS
from app.tasks.reindex_run_state import _clear_stale_progress
from app.tasks.reindex_run_state import _is_cancellation_requested
from app.tasks.reindex_run_state import _reindex_keys
from app.tasks.reindex_run_state import _release_reindex_lock
from app.tasks.reindex_run_state import _send_reindex_progress

logger = logging.getLogger(__name__)


def _resolve_reindex_speaker_name(row: Any, *, voiceprint_names: bool) -> str:
    """Canonical label for one batch-fetched speaker row.

    ``row`` is one of the ``(id, name, display_name, profile_id,
    suggested_name, confidence)`` tuples :func:`_extract_file_metadata`
    batch-fetches. Delegates to
    :func:`~app.utils.speaker_labels.canonical_speaker_label_for_row` — the SINGLE
    home for this resolution — instead of this writer's old ``display_name or
    name or "Unknown"`` chain, which emitted a bare ``"Unknown"`` next to
    ``file_facts``'s ``"Unknown Speaker"`` and never considered a confident
    LLM/embedding suggestion.

    A voiceprint-derived name is left out while the file's voiceprints are off (#1280), so a
    reindex run after a switch is turned off drops the stale label from the index.
    ``voiceprint_names`` is required: the caller resolves the file's decision once.

    Args:
        row: A row exposing ``.name``, ``.display_name``, ``.suggested_name``,
            ``.confidence``, ``.suggestion_source``, ``.verified`` and ``.media_file_id``.
        voiceprint_names: The file's voiceprint decision, resolved once by the caller.

    Returns:
        The canonical display label.
    """
    from app.utils.speaker_labels import canonical_speaker_label_for_row

    return canonical_speaker_label_for_row(row, voiceprint_names=voiceprint_names)


def _extract_file_metadata(db: Any, media_file: Any) -> dict[str, Any] | None:
    """Extract segments and metadata from a media file for indexing.

    Returns:
        Dict with segments, title, speakers, tags, and other metadata,
        or None if the file has no segments.
    """
    from app.models.media import TranscriptSegment
    from app.models.speaker import Speaker
    from app.services.permission_service import PermissionService
    from app.services.privacy.gates import voiceprints_allowed
    from app.utils.speaker_identity_source import visible_profile_id
    from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABEL
    from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS

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
    profile_map: dict[int, int | None] = {}
    if speaker_ids:
        spk_rows = (
            db.query(
                Speaker.id,
                Speaker.name,
                Speaker.display_name,
                Speaker.profile_id,
                Speaker.suggested_name,
                Speaker.confidence,
                Speaker.suggestion_source,
                Speaker.verified,
                Speaker.identity_source,
                Speaker.media_file_id,
            )
            .filter(Speaker.id.in_(speaker_ids))
            .all()
        )
        # One decision for the file (unreadable = hidden), not one per speaker.
        voiceprint_names = voiceprints_allowed(file_id, db)
        speakers_map = {
            r.id: _resolve_reindex_speaker_name(r, voiceprint_names=voiceprint_names)
            for r in spk_rows
        }
        profile_map = {
            r.id: visible_profile_id(r.profile_id, r.identity_source, voiceprint_names)
            for r in spk_rows
        }

    # Convert ORM objects to dicts
    segment_dicts = []
    for seg in segments:
        speaker_name = (
            speakers_map.get(seg.speaker_id, UNKNOWN_SPEAKER_LABEL)
            if seg.speaker_id
            else UNKNOWN_SPEAKER_LABEL
        )
        seg_dict = {
            "start": float(seg.start_time),
            "end": float(seg.end_time),
            "text": seg.text or "",
            "speaker": speaker_name,
        }
        # Same only-when-known convention as `chunking_service._make_chunk`: a
        # segment with no resolved Speaker row carries neither key at all.
        if seg.speaker_id:
            seg_dict["speaker_id"] = seg.speaker_id
            profile_id = profile_map.get(seg.speaker_id)
            if profile_id is not None:
                seg_dict["profile_id"] = profile_id
        if seg.words:
            seg_dict["words"] = seg.words  # Already a list of dicts from JSONB
        segment_dicts.append(seg_dict)

    # Get unique speaker names
    speaker_names: list[str] = list(
        set(str(s["speaker"]) for s in segment_dicts if s["speaker"] not in UNKNOWN_SPEAKER_LABELS)
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

    # The run id. `self.request.id` is absent when the task is invoked outside a
    # Celery request (eager mode, a direct call), and a `None` run id silently
    # selects the legacy shared key shape this whole mechanism exists to avoid —
    # so synthesise one rather than aliasing onto another run.
    task_id = self.request.id or f"local-{uuid4()}"
    logger.info(f"Re-index coordinator {task_id} started for user {user_id}")

    # Prevent concurrent reindex runs for the same user. Kept USER-scoped: this
    # is the mutual exclusion, and run-scoping it would grant the concurrency.
    redis_lock = get_redis()
    lock_key = REINDEX_LOCK_KEY.format(user_id=user_id)
    if not redis_lock.set(lock_key, task_id, nx=True, ex=3600):
        logger.warning(f"Reindex already running for user {user_id}, skipping")
        return {"status": "skipped", "message": "Reindex already in progress"}

    # A stop that landed while this coordinator sat in the broker queue names
    # THIS run (#691), and honouring it is what makes cancelling a fan-out work
    # on a deployment with fewer workers than owners: the caller's coordinator
    # runs while owners B..N wait, so the flags `stop` wrote for them are read
    # here, not by a batch worker. The flag is cleared either way — one left over
    # from an EARLIER run must not abort this one after its first file — and
    # naming the run is exactly what tells those two cases apart.
    if consume_pending_cancel(user_id, task_id):
        logger.info(f"Reindex run {task_id} for user {user_id} was cancelled before it started")
        _release_reindex_lock(redis_lock, user_id, task_id)
        return {"status": "cancelled", "message": "Reindex cancelled before it started"}

    # Clear the stale progress tracker from any previous run (failed or completed).
    # This no longer touches the coordination state: see `_clear_stale_progress`.
    _clear_stale_progress(user_id)

    state_key, uuids_key = _reindex_keys(user_id, task_id)

    if model_id:
        error = _handle_model_switch(model_id)
        if error:
            # Same leak as the zero-files return below: every exit between
            # acquiring the user lock and dispatching the workers must release it,
            # or the next reindex within the hour is silently skipped.
            _release_reindex_lock(redis_lock, user_id, task_id)
            return error

    # Structural reconciliation of the SHARED chunks index. `reindex_lock` is
    # per-user, so N owners' coordinators reach these two calls concurrently —
    # and both of them can DELETE and recreate the index. The scope is
    # deliberately just this pair, not the whole coordinator: both are
    # check-then-return no-ops in the common case (matching version, matching
    # dimension), so the lock is held for milliseconds, and the expensive bulk
    # re-embed below stays parallel across owners the way the fan-out intends.
    with _index_structure_lock() as acquired:
        if acquired:
            # Recreate stale index if schema version has changed
            _check_and_recreate_stale_index()

            # Reconcile index dimension with settings (handles model switch via API
            # where model_id is not passed to this task but is already saved to DB)
            try:
                from app.services.search.index_admin import recreate_index_for_dimension
                from app.services.search.settings_service import get_search_embedding_dimension

                target_dim = get_search_embedding_dimension()
                recreate_index_for_dimension(target_dim)
            except Exception as e:
                logger.warning(f"Dimension reconciliation failed: {e}")
        else:
            # Another coordinator (or a corruption repair) is reconciling the
            # same index right now. Indexing our own files is still correct and
            # additive; the next run re-checks the structure.
            logger.warning(
                "Index structure lock busy; skipping stale-index/dimension reconciliation "
                "for this run"
            )

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
            # ⚠️ Release the user lock. This return used to leak it for its full
            # 3600 s TTL, and the failure it produced was measured, not imagined:
            # a model switch dispatched a reindex before any files existed (0
            # files, done in 0.09 s, lock kept), the corpus was ingested, a SECOND
            # switch dispatched its re-embed 59 minutes later — and the coordinator
            # answered "Reindex already running, skipping" 67 seconds before the
            # dead run's lock would have expired. The switch reported success, the
            # pipeline pointed at the new model, and every stored vector silently
            # stayed in the OLD model's space: the exact mixed-vector-space failure
            # #437 exists to prevent, triggered by the documented happy path.
            _release_reindex_lock(get_redis(), user_id, task_id)
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
        redis.expire(state_key, _REINDEX_STATE_TTL_SECONDS)
        # The uuid set needs the same TTL — it is the input to the orphan sweep,
        # and a set that outlives its run is a set that describes a different one.
        # It CANNOT be set here: the set does not exist yet (Redis `EXPIRE` on a
        # missing key is a no-op that returns 0), so this call has never done
        # anything. The batch worker sets it on first `sadd` instead.

        # Partition file IDs into worker_count groups
        partitions: list[list[int]] = [[] for _ in range(worker_count)]
        for i, file_id in enumerate(all_file_ids):
            partitions[i % worker_count].append(file_id)

        dispatched = 0
        for partition in partitions:
            if partition:
                reindex_batch_task.apply_async(
                    args=[partition, user_id, task_id],
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
            # This run's OWN keys only — never the user-scoped pair, which may
            # belong to a run that is still working.
            get_redis().delete(state_key, uuids_key)
        with contextlib.suppress(Exception):
            _release_reindex_lock(get_redis(), user_id, task_id)
        return {"error": str(e)}


def _load_reindex_page(file_ids: list[int]) -> list[tuple[str, dict[str, Any] | None]]:
    """Load one page of files as ``(file_uuid, metadata | None)``, then close the session.

    Returns plain dicts only — ``_extract_file_metadata`` already flattens
    segments, speakers, tags and collections — so the indexing phase can run
    with no session (and therefore no transaction) open. ``None`` metadata means
    the file has no segments and should be counted as skipped.
    """
    from sqlalchemy.orm import defer

    from app.models.media import MediaFile

    page: list[tuple[str, dict[str, Any] | None]] = []
    with session_scope() as db:
        page_files = (
            db.query(MediaFile)
            .options(
                defer(MediaFile.summary_data),
                defer(MediaFile.metadata_raw),
                defer(MediaFile.waveform_data),
            )
            .filter(MediaFile.id.in_(file_ids))
            .order_by(MediaFile.id)
            .all()
        )
        for media_file in page_files:
            page.append((str(media_file.uuid), _extract_file_metadata(db, media_file)))
    return page


@celery_app.task(name="reindex_batch", priority=CPUPriority.MAINTENANCE)
def reindex_batch_task(
    file_ids: list[int],
    user_id: int,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Process a partition of files for re-indexing.

    Each worker indexes its assigned files and updates its own run's Redis state
    atomically. The last worker of that run to finish handles cleanup.

    Args:
        file_ids: The partition of media file ids this worker owns.
        user_id: Owner of the reindex.
        run_id: The dispatching coordinator's task id, which scopes the
            coordination keys to one run. **Defaults to None deliberately**: a
            Celery signature is a wire contract, and batch messages already on
            the queue when the workers are upgraded arrive with the old
            two-argument form. A required third parameter fails those at
            dispatch and abandons their files. ``None`` selects the legacy
            user-only keys — which is not merely tolerated but correct, because
            the pre-upgrade coordinator that queued the message wrote exactly
            that pair.
    """
    from app.services.progress_tracker import ProgressTracker
    from app.services.search.indexing_service import TranscriptIndexingService

    indexing_service = TranscriptIndexingService()
    redis = get_redis()
    state_key, uuids_key = _reindex_keys(user_id, run_id)

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
    uuids_ttl_set = False

    for batch_start in range(0, len(file_ids), page_size):
        if cancelled:
            break
        batch_ids = file_ids[batch_start : batch_start + page_size]

        # Phase 1 — read the whole page out as plain dicts, then CLOSE the
        # session. The old shape held one transaction open across up to 50
        # OpenSearch round trips (a full transcript re-chunk + embed each), so a
        # reindex of a large library parked a Postgres backend "idle in
        # transaction" for minutes at a time with `transcript_segment` under
        # ACCESS SHARE — blocking DDL and pinning the vacuum horizon.
        try:
            page_metadata = _load_reindex_page(batch_ids)
        except Exception as e:
            logger.error(f"Error loading reindex page starting at {batch_start}: {e}")
            local_stats["failed"] += len(batch_ids)
            redis.hincrby(state_key, "failed", len(batch_ids))
            continue

        # Phase 2 — index. OpenSearch only; NO DB session is held here.
        for file_uuid, metadata in page_metadata:
            # Check cancellation between files
            if _is_cancellation_requested(user_id, run_id):
                logger.info(f"Reindex batch cancelled for user {user_id}")
                cancelled = True
                break

            if metadata is None:
                local_stats["skipped"] += 1
                redis.hincrby(state_key, "indexed", 1)
                redis.hincrby(state_key, "skipped", 1)
                _send_reindex_progress(redis, user_id, state_key, tracker)
                continue

            try:
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
                if not uuids_ttl_set:
                    # The set only exists once something has been added to it,
                    # so this is the earliest point the TTL can actually attach.
                    redis.expire(uuids_key, _REINDEX_STATE_TTL_SECONDS)
                    uuids_ttl_set = True

                # Update this run's progress atomically
                redis.hincrby(state_key, "indexed", 1)
                redis.hincrby(state_key, "chunks", chunk_count)

                # Send progress notification with EWMA ETA
                _send_reindex_progress(redis, user_id, state_key, tracker)

            except Exception as e:
                logger.error(f"Error re-indexing file {file_uuid}: {e}")
                local_stats["failed"] += 1
                redis.hincrby(state_key, "failed", 1)

    # Mark this worker as done
    workers_done = redis.hincrby(state_key, "workers_done", 1)
    worker_count = int(redis.hget(state_key, "worker_count") or 1)

    # Last worker OF THIS RUN handles cleanup
    if workers_done >= worker_count:
        _handle_reindex_completion(redis, user_id, state_key, uuids_key, run_id)

    return local_stats
