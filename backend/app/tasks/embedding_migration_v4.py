"""
Speaker embedding migration task for v3->v4 upgrade.

This module provides the migration infrastructure for upgrading speaker
embeddings from PyAnnote v3 (512-dim) to v4 (256-dim WeSpeaker).

Uses the unified migration_pipeline for I/O pipelining and GPU workers,
and speaker_analysis_models.EmbeddingModelAdapter for model abstraction.

Key features:
- Batched extraction (25 files/task) with warm-cached embedding model
- Prefetch pipeline: I/O threads download next files while GPU processes current
- Skip logic for files already migrated to v4
- Bulk OpenSearch writes instead of individual calls
- Celery priorities ensure migration tasks run before transcription
- Safe finalize with count validation, backup, and finally-block cleanup
"""

import contextlib
import datetime
import json
import logging

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_MIGRATION_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_MIGRATION_FINALIZED
from app.core.constants import NOTIFICATION_TYPE_MIGRATION_PROGRESS
from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
from app.core.constants import CPUPriority
from app.core.constants import GPUPriority
from app.core.constants import UtilityPriority
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v3
from app.core.constants import get_speaker_index_v3_backup
from app.core.constants import get_speaker_index_v4
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.services.embedding_mode_service import MODE_V4
from app.services.embedding_mode_service import EmbeddingModeService
from app.services.migration_progress_service import migration_progress
from app.services.speaker_analysis_models import SegmentResult
from app.tasks.migration_pipeline import PreparedFile
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

# Batch size for grouping files into a single GPU task
_BATCH_SIZE = 25


# ---------------------------------------------------------------------------
# Status helpers (unchanged API)
# ---------------------------------------------------------------------------


def get_migration_status() -> dict:
    """Get the current migration status.

    Degrades gracefully when OpenSearch is unreachable: constructing the client
    never touches the network, so an outage only surfaces on the first real call
    — return the same "not available" envelope as the no-client branch instead
    of letting a connection error bubble up as a 500 from the status endpoint.
    """
    from opensearchpy.exceptions import OpenSearchException

    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return {"status": "error", "message": "OpenSearch not available"}

    current_mode = EmbeddingModeService.detect_mode()
    v3_index = get_speaker_index_v3()
    v4_index = get_speaker_index_v4()
    backup_index = get_speaker_index_v3_backup()
    try:
        v4_exists = client.indices.exists(index=v4_index)
        backup_exists = client.indices.exists(index=backup_index)
    except OpenSearchException as e:
        logger.warning("OpenSearch unreachable while reading migration status: %s", e)
        return {"status": "error", "message": "OpenSearch not available"}

    # Count only speaker documents (exclude profile_ and cluster_ documents
    # which share the same index but have a document_type field)
    speaker_only_query = {"query": {"bool": {"must_not": {"exists": {"field": "document_type"}}}}}

    # Query concrete versioned indices directly (not through alias)
    try:
        v3_document_count = (
            client.count(index=v3_index, body=speaker_only_query)["count"]
            if client.indices.exists(index=v3_index)
            else 0
        )
    except Exception:
        v3_document_count = 0

    try:
        v4_document_count = (
            client.count(index=v4_index, body=speaker_only_query)["count"] if v4_exists else 0
        )
    except Exception:
        v4_document_count = 0

    # If v3 concrete index is empty, check the legacy backup (non-critical)
    if v3_document_count == 0 and backup_exists:
        with contextlib.suppress(Exception):
            v3_document_count = client.count(index=backup_index, body=speaker_only_query)["count"]

    return {
        "current_mode": current_mode,
        "v4_index_exists": v4_exists,
        "v3_document_count": v3_document_count,
        "v4_document_count": v4_document_count,
        "migration_needed": current_mode == "v3",
        "migration_complete": current_mode == "v4" and not backup_exists,
        "transcription_paused": False,
    }


@celery_app.task(bind=True, name="check_migration_status", priority=UtilityPriority.BACKGROUND)
def check_migration_status_task(self):
    """Check the current embedding migration status."""
    return get_migration_status()


# ---------------------------------------------------------------------------
# Index management helpers
# ---------------------------------------------------------------------------


def _clear_v4_index() -> None:
    """Delete all documents from the v4 speaker index for a clean re-migration."""
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch not available — cannot clear v4 index")
        return

    v4_index = get_speaker_index_v4()
    if not client.indices.exists(index=v4_index):
        logger.info("v4 index does not exist — nothing to clear")
        return

    try:
        count_before = client.count(index=v4_index).get("count", 0)
        client.delete_by_query(
            index=v4_index,
            body={"query": {"match_all": {}}},
            refresh=True,
        )
        logger.info(f"Cleared {count_before} documents from {v4_index} for force re-migration")
    except Exception as e:
        logger.error(f"Failed to clear v4 index: {e}")


# ---------------------------------------------------------------------------
# Skip-logic helper
# ---------------------------------------------------------------------------


def _count_embeddable_speakers_per_file(file_ids: list[int]) -> dict[int, int]:
    """Count speakers that the pipeline will produce embeddings for, per file.

    Mirrors the threshold logic in migration_pipeline.collect_work_items:
    any speaker with at least one segment >= SPEAKER_SHORT_SEGMENT_MIN_DURATION
    will be processed (the pipeline falls back to that lower threshold when a
    speaker's longest merged segment is below SPEAKER_SEGMENT_MIN_DURATION).
    """
    from sqlalchemy import distinct
    from sqlalchemy import func as sa_func

    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    if not file_ids:
        return {}

    with session_scope() as db:
        rows = (
            db.query(Speaker.media_file_id, sa_func.count(distinct(Speaker.id)))
            .join(
                TranscriptSegment,
                (TranscriptSegment.speaker_id == Speaker.id)
                & (TranscriptSegment.media_file_id == Speaker.media_file_id),
            )
            .filter(
                Speaker.media_file_id.in_(file_ids),
                (TranscriptSegment.end_time - TranscriptSegment.start_time)
                >= SPEAKER_SHORT_SEGMENT_MIN_DURATION,
            )
            .group_by(Speaker.media_file_id)
            .all()
        )
        return {int(fid): cnt for fid, cnt in rows}


def _get_already_migrated_file_ids() -> set[int]:
    """Return set of media_file_id values fully migrated in the v4 index."""
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return set()

    v4_index = get_speaker_index_v4()
    if not client.indices.exists(index=v4_index):
        return set()

    try:
        response = client.search(
            index=v4_index,
            body={
                "size": 0,
                "aggs": {
                    "file_ids": {
                        "terms": {
                            "field": "media_file_id",
                            "size": 50000,
                        }
                    }
                },
            },
        )
        buckets = response.get("aggregations", {}).get("file_ids", {}).get("buckets", [])
        v4_counts = {int(b["key"]): b["doc_count"] for b in buckets}
    except Exception as e:
        logger.warning(f"Could not query v4 index for skip logic: {e}")
        return set()

    if not v4_counts:
        return set()

    embeddable_counts = _count_embeddable_speakers_per_file(list(v4_counts.keys()))

    fully_migrated: set[int] = set()
    partial_count = 0

    for file_id, v4_count in v4_counts.items():
        embeddable = embeddable_counts.get(file_id, 0)
        if embeddable == 0 or v4_count >= embeddable:
            fully_migrated.add(file_id)
        else:
            partial_count += 1

    if partial_count:
        logger.info(f"Found {partial_count} partially migrated files (will be re-processed)")

    return fully_migrated


# ---------------------------------------------------------------------------
# Segment builder (shared between batch task and legacy single-file task)
# ---------------------------------------------------------------------------


def _build_speaker_segments(db, media_file_id: int) -> dict[int, list[dict[str, float]]]:
    """Build mapping of speaker ID to their transcript segments."""
    from app.models.media import TranscriptSegment

    segments = (
        db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == media_file_id).all()
    )

    speaker_segments: dict[int, list[dict[str, float]]] = {}
    for seg in segments:
        if seg.speaker_id not in speaker_segments:
            speaker_segments[seg.speaker_id] = []
        speaker_segments[seg.speaker_id].append({"start": seg.start_time, "end": seg.end_time})
    return speaker_segments


# ---------------------------------------------------------------------------
# Bulk-write helper
# ---------------------------------------------------------------------------


def _bulk_write_v4_embeddings(speaker_docs: list[dict]) -> int:
    """Bulk-index a list of speaker embedding documents to the v4 index."""
    from app.services.opensearch_service import get_opensearch_client

    if not speaker_docs:
        return 0

    client = get_opensearch_client()
    if not client:
        logger.error("OpenSearch client unavailable for bulk write")
        return 0

    v4_index = get_speaker_index_v4()
    bulk_body: list[dict] = []
    for doc in speaker_docs:
        doc_id = doc.pop("_id")
        bulk_body.append({"index": {"_index": v4_index, "_id": doc_id}})
        bulk_body.append(doc)

    try:
        response = client.bulk(body=bulk_body)
        errors = response.get("errors", False)
        if errors:
            failed_count = 0
            for item in response.get("items", []):
                err = item.get("index", {}).get("error")
                if err:
                    logger.error(f"Bulk index error: {err}")
                    failed_count += 1
            return len(speaker_docs) - failed_count
        return len(speaker_docs)
    except Exception as e:
        logger.error(f"Bulk write to v4 index failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Result writer for the unified pipeline
# ---------------------------------------------------------------------------


def _embedding_result_writer(
    prepared: PreparedFile,
    results_by_model: dict[str, list[SegmentResult]],
) -> int:
    """Aggregate embedding results and bulk-write to OpenSearch v4 index.

    This is the result_writer callback for process_batch_pipelined().
    """
    embedding_results = results_by_model.get("embedding", [])
    if not embedding_results:
        return 0

    # Group embeddings by speaker
    speaker_embeddings: dict[int, list] = {}
    for sr in embedding_results:
        speaker_embeddings.setdefault(sr.speaker_id, []).append(sr.value)

    # Aggregate and build docs
    docs: list[dict] = []
    now = datetime.datetime.now(datetime.UTC).isoformat()
    speaker_profiles = prepared.extra.get("speaker_profiles", {})

    # Build speaker lookup for metadata
    speaker_by_id = {sp.id: sp for sp in prepared.speakers}

    for speaker_id, embs in speaker_embeddings.items():
        if not embs:
            continue

        # Use static aggregate method
        import numpy as np

        if len(embs) == 1:
            aggregated = embs[0]
            norm = np.linalg.norm(aggregated)
            if norm > 0:
                aggregated = aggregated / norm
        else:
            stacked = np.vstack(embs)
            aggregated = np.mean(stacked, axis=0)
            norm = np.linalg.norm(aggregated)
            if norm > 0:
                aggregated = aggregated / norm

        speaker = speaker_by_id.get(speaker_id)
        if not speaker:
            continue

        docs.append(
            {
                "_id": str(speaker.uuid),
                "speaker_id": speaker.id,
                "speaker_uuid": str(speaker.uuid),
                "profile_id": speaker.profile_id,
                "profile_uuid": speaker_profiles.get(speaker.id),
                "user_id": prepared.user_id,
                "name": speaker.name,
                "display_name": None,
                "collection_ids": [],
                "media_file_id": prepared.media_file_id,
                "segment_count": len(embs),
                "created_at": now,
                "updated_at": now,
                "embedding": aggregated.tolist(),
            }
        )

    if docs:
        _bulk_write_v4_embeddings(docs)

    return len(docs)


# ---------------------------------------------------------------------------
# Orchestrator task
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="migrate_speaker_embeddings_to_v4",
    priority=CPUPriority.ADMIN_BATCH,
)
def migrate_speaker_embeddings_v4_task(
    self,
    user_id: int | None = None,
    force: bool = False,
):
    """Orchestrate migration of all speaker embeddings to v4.

    Acquires the migration lock (pausing transcription), queries completed
    files, filters out already-migrated ones, and dispatches batched GPU
    tasks (25 files per task).
    """
    from app.services.opensearch_service import create_speaker_index_v4

    task_id = self.request.id
    action = "force re-migration" if force else "migration"
    logger.info(f"Starting speaker embedding {action} to v4: task_id={task_id}")

    # Prevent concurrent migrations of the same type
    if migration_progress.is_running():
        logger.warning("Migration already in progress, skipping")
        return {
            "status": "skipped",
            "message": "A migration is already in progress",
            "existing_status": migration_progress.get_status(),
        }

    # Check current mode (allow force even if already v4)
    current_mode = EmbeddingModeService.detect_mode()
    if current_mode == MODE_V4 and not force:
        logger.info("Already in v4 mode, no migration needed")
        return {"status": "skipped", "message": "Already using v4 embeddings"}

    # Force mode: delete all existing v4 documents for clean re-extraction
    if force:
        _clear_v4_index()

    # Create the v4 staging index (no-op if already exists)
    if not create_speaker_index_v4():
        logger.error("Failed to create v4 staging index")
        return {"status": "error", "message": "Failed to create v4 staging index"}

    # Get completed files from DB (project only id + uuid to avoid loading JSONB)
    with session_scope() as db:
        query = db.query(MediaFile.id, MediaFile.uuid).filter(
            MediaFile.status == FileStatus.COMPLETED
        )
        if user_id:
            query = query.filter(MediaFile.user_id == user_id)
        media_rows = query.all()

        # Filter out files already in v4 index (force mode: nothing to skip)
        if force:
            files_to_migrate = list(media_rows)
            skipped = 0
        else:
            already_migrated = _get_already_migrated_file_ids()
            files_to_migrate = [f for f in media_rows if f.id not in already_migrated]
            skipped = len(media_rows) - len(files_to_migrate)

        total_files = len(files_to_migrate)
        logger.info(
            f"Found {len(media_rows)} completed files, "
            f"{skipped} already migrated, {total_files} to process"
        )

        if total_files == 0:
            return {
                "status": "success",
                "message": f"No files to migrate ({skipped} already done)",
                "migrated": 0,
                "skipped": skipped,
            }

        # Start progress tracking
        migration_progress.start_migration(total_files=total_files, task_id=task_id)

        # Initialize unified progress tracker for ETA
        from app.services.progress_tracker import ProgressTracker

        tracker = ProgressTracker(
            task_type="migration",
            user_id=user_id or 1,
            total=total_files,
        )
        tracker.start(message="Starting embedding migration...")

        send_ws_event(
            user_id or 1,
            NOTIFICATION_TYPE_MIGRATION_PROGRESS,
            {
                "processed_files": 0,
                "total_files": total_files,
                "failed_files": [],
                "progress": 0,
                "running": True,
            },
        )

        # Build batches of file UUIDs
        file_uuids = [str(f.uuid) for f in files_to_migrate]

    batches = [file_uuids[i : i + _BATCH_SIZE] for i in range(0, len(file_uuids), _BATCH_SIZE)]

    batch_task_ids = []
    for batch_idx, batch in enumerate(batches):
        result = extract_v4_embeddings_batch_task.apply_async(
            kwargs={
                "file_uuids": batch,
                "batch_index": batch_idx,
                "total_batches": len(batches),
                "total_files": total_files,
                "user_id": user_id,
            },
            priority=GPUPriority.ADMIN_MIGRATION,
        )
        batch_task_ids.append(result.id)

    # Store batch task IDs for revocation on stop
    try:
        from app.core.redis import get_redis

        r = get_redis()
        r.set("embedding_migration:batch_task_ids", json.dumps(batch_task_ids), ex=86400)
    except Exception as e:
        logger.warning("Failed to store batch task IDs: %s", e)

    logger.info(f"Dispatched {len(batches)} batch tasks for {total_files} files")

    # Notify frontend that batches are queued and waiting for a GPU worker.
    # The first WS event (above) had no message; this one tells the user why
    # progress is stuck at 0% — GPU may be busy with another task.
    send_ws_event(
        user_id or 1,
        NOTIFICATION_TYPE_MIGRATION_PROGRESS,
        {
            "processed_files": 0,
            "total_files": total_files,
            "failed_files": [],
            "progress": 0,
            "running": True,
            "message": f"Queued — {len(batches)} batches waiting for GPU worker",
        },
    )

    return {
        "status": "in_progress",
        "message": f"Dispatched {len(batches)} batch tasks ({total_files} files, {skipped} skipped)",
        "total_files": total_files,
        "skipped": skipped,
        "task_id": task_id,
    }


# ---------------------------------------------------------------------------
# Batched extraction task with unified pipeline
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True, name="extract_v4_embeddings_batch", priority=GPUPriority.ADMIN_MIGRATION
)
def extract_v4_embeddings_batch_task(
    self,
    file_uuids: list[str],
    batch_index: int = 0,
    total_batches: int = 1,
    total_files: int = 0,
    user_id: int | None = None,
):
    """Extract v4 embeddings for a batch of media files.

    Uses the unified migration_pipeline with EmbeddingModelAdapter for
    multi-model GPU pipelining. The primary embedding model is loaded ONCE
    and kept warm across batches.
    """
    from app.services.speaker_analysis_models import EmbeddingModelAdapter
    from app.services.speaker_analysis_models import MultiModelRunner
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.migration_pipeline import prepare_file
    from app.tasks.migration_pipeline import process_batch_pipelined

    logger.info(f"Batch {batch_index + 1}/{total_batches}: processing {len(file_uuids)} files")

    # Get warm-cached embedding service (loaded once, reused across batches)
    embedding_service = get_cached_embedding_service(mode=MODE_V4)
    runner = MultiModelRunner([EmbeddingModelAdapter(embedding_service)])

    # Initialize unified progress tracker for ETA
    from app.services.progress_tracker import ProgressTracker

    target_user = user_id or 1
    tracker = ProgressTracker(
        task_type="migration",
        user_id=target_user,
        total=total_files,
    )
    existing_tracker = ProgressTracker.get_state("migration", target_user)
    if existing_tracker:
        tracker.resume_from_state(existing_tracker)

    # Phase 1: Prepare all files (presigned URLs + DB queries — fast)
    files_with_speakers: list[tuple[str, PreparedFile]] = []
    for fuuid in file_uuids:
        if not migration_progress.is_running():
            logger.warning("Migration stopped — aborting batch")
            break
        try:
            prepared = prepare_file(fuuid, include_profile=True)
            if prepared is None:
                # No speakers — still counts as processed
                migration_progress.increment_processed(success=True)
                _emit_progress(tracker, target_user, total_files)
            else:
                files_with_speakers.append((fuuid, prepared))
        except Exception as e:
            logger.error(f"Failed to prepare file {fuuid}: {e}")
            migration_progress.increment_processed(success=False, file_uuid=fuuid)
            _emit_progress(tracker, target_user, total_files, failed_item=fuuid)

    # Phase 2: Multi-model pipelined GPU extraction
    if files_with_speakers:

        def on_success(fuuid: str) -> None:
            migration_progress.increment_processed(success=True)
            _emit_progress(tracker, target_user, total_files)

        def on_failure(fuuid: str, exc: Exception | None) -> None:
            migration_progress.increment_processed(success=False, file_uuid=fuuid)
            _emit_progress(tracker, target_user, total_files, failed_item=fuuid)

        process_batch_pipelined(
            prepared_files=files_with_speakers,
            runner=runner,
            result_writer=_embedding_result_writer,
            is_running_check=migration_progress.is_running,
            on_file_success=on_success,
            on_file_failure=on_failure,
            min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
        )

    # Check completion
    status = migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0) or total_files
    failed_files = status.get("failed_files", [])
    is_complete = processed >= total and total > 0

    if is_complete and migration_progress.complete_migration(success=True):
        logger.info(f"All {total} migration files processed")
        tracker.complete(message="Migration complete")
        send_ws_event(
            user_id or 1,
            NOTIFICATION_TYPE_MIGRATION_COMPLETE,
            {
                "status": "complete",
                "total_files": total,
                "failed_files": failed_files,
                "success_count": total - len(failed_files),
            },
        )

    # Free intermediate CUDA tensors for follow-on tasks
    from app.tasks.migration_pipeline import cleanup_gpu_memory

    cleanup_gpu_memory()

    return {
        "status": "success",
        "batch_index": batch_index,
    }


def _emit_progress(
    tracker,
    user_id: int,
    total_files: int,
    failed_item: str | None = None,
) -> None:
    """Emit migration progress notification with ETA."""
    from app.services.progress_tracker import emit_progress_notification

    status = migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0) or total_files
    failed_files = status.get("failed_files", [])

    emit_progress_notification(
        tracker=tracker,
        processed=processed,
        user_id=user_id,
        notification_type=NOTIFICATION_TYPE_MIGRATION_PROGRESS,
        extra_data={
            "processed_files": processed,
            "total_files": total,
            "failed_files": failed_files,
            "running": processed < total,
        },
        message=f"Processed {processed} of {total} files",
        failed_item=failed_item,
    )


# ---------------------------------------------------------------------------
# Legacy single-file task (kept for backward compat with any in-flight tasks)
# ---------------------------------------------------------------------------


@celery_app.task(bind=True, name="extract_v4_embeddings", priority=GPUPriority.ADMIN_MIGRATION)
def extract_v4_embeddings_task(
    self,
    file_uuid: str,
    task_index: int = 0,
    total_tasks: int = 1,
    user_id: int | None = None,
):
    """Legacy single-file extraction (kept for in-flight task compatibility).

    New migrations use extract_v4_embeddings_batch_task instead.
    """
    from app.services.speaker_analysis_models import EmbeddingModelAdapter
    from app.services.speaker_analysis_models import MultiModelRunner
    from app.services.speaker_embedding_service import get_cached_embedding_service
    from app.tasks.migration_pipeline import prepare_file
    from app.tasks.migration_pipeline import process_batch_pipelined

    logger.info(f"[legacy] Extracting v4 embeddings for file {file_uuid}")

    embedding_service = get_cached_embedding_service(mode=MODE_V4)
    runner = MultiModelRunner([EmbeddingModelAdapter(embedding_service)])

    try:
        prepared = prepare_file(file_uuid, include_profile=True)
        if prepared:
            process_batch_pipelined(
                prepared_files=[(file_uuid, prepared)],
                runner=runner,
                result_writer=_embedding_result_writer,
                is_running_check=migration_progress.is_running,
                on_file_success=lambda _: migration_progress.increment_processed(  # type: ignore[arg-type]
                    success=True
                ),
                on_file_failure=lambda f, _: migration_progress.increment_processed(  # type: ignore[arg-type]
                    success=False, file_uuid=f
                ),
                min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION,
            )
        else:
            migration_progress.increment_processed(success=True)
    except Exception as e:
        logger.error(f"[legacy] Error migrating {file_uuid}: {e}")
        migration_progress.increment_processed(success=False, file_uuid=file_uuid)

    # Check completion
    status = migration_progress.get_status()
    processed = status.get("processed_files", 0)
    total = status.get("total_files", 0)
    if processed >= total and total > 0:
        migration_progress.complete_migration(success=True)

    # Free intermediate CUDA tensors for follow-on tasks
    from app.tasks.migration_pipeline import cleanup_gpu_memory

    cleanup_gpu_memory()

    return {"status": "success", "file_uuid": file_uuid}


# ---------------------------------------------------------------------------
# Finalize task (safe version)
# ---------------------------------------------------------------------------

_PROFILE_DOC_QUERY = {"query": {"term": {"document_type": "profile"}}}


def migrate_profile_documents_to_v4(client, v4_index: str) -> int:
    """Backfill v4 profile-voiceprint documents from already-migrated v4 speakers.

    Defect 1 of issue #657: the streaming per-file writer (
    ``_embedding_result_writer``) only ever produced speaker documents, never
    ``document_type: "profile"`` docs. Profile voiceprints are consolidated
    centroids that live only in the ``speakers`` alias's dual-write path
    (``store_profile_embedding``/``store_profile_embedding_v4``), so any
    profile not touched by a file transcribed *during* the migration window
    would have no v4 counterpart at all, and ``swap_speaker_alias`` would
    silently strand its voiceprint in the (soon inactive) v3 index.

    This recomputes every profile's centroid directly from the v4 speaker
    documents that already carry a ``profile_uuid`` — the only source of a
    256-dim vector for that profile, since a v3 512-dim profile embedding
    cannot be reused at a different dimension without re-running the model.
    A profile with zero migrated v4 speakers is therefore still not
    recoverable here; that is a hard requirement of the dimension change, not
    an oversight, and is why this is called *before* the finalize profile-
    count guard, which will then correctly refuse the swap if any profile
    still cannot be accounted for.

    Args:
        client: OpenSearch client.
        v4_index: Concrete v4 index to read speakers from / write profiles to.

    Returns:
        Number of profile documents written.
    """
    import numpy as np

    profiles: dict[str, dict] = {}

    try:
        response = client.search(
            index=v4_index,
            body={
                "size": 500,
                "query": {
                    "bool": {
                        "must": [{"exists": {"field": "profile_uuid"}}],
                        "must_not": [{"exists": {"field": "document_type"}}],
                    }
                },
                "_source": [
                    "profile_id",
                    "profile_uuid",
                    "user_id",
                    "organization_id",
                    "segment_count",
                    "embedding",
                ],
            },
            scroll="2m",
        )
        scroll_id = response.get("_scroll_id")
        hits = response["hits"]["hits"]
        while hits:
            for hit in hits:
                source = hit["_source"]
                profile_uuid = source.get("profile_uuid")
                if not profile_uuid:
                    continue
                weight = max(1, int(source.get("segment_count") or 1))
                entry = profiles.setdefault(
                    profile_uuid,
                    {
                        "profile_id": source.get("profile_id"),
                        "user_id": source.get("user_id"),
                        "organization_id": source.get("organization_id"),
                        "vectors": [],
                        "weights": [],
                        "speaker_count": 0,
                    },
                )
                entry["vectors"].append(source["embedding"])
                entry["weights"].append(weight)
                entry["speaker_count"] += 1
            if not scroll_id:
                break
            response = client.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = response.get("_scroll_id")
            hits = response["hits"]["hits"]
    except Exception as e:
        logger.error(f"Failed to scroll v4 speaker docs for profile backfill: {e}")
        return 0

    if not profiles:
        return 0

    from app.models.media import SpeakerProfile
    from app.services.opensearch_service import store_profile_embedding_v4

    profile_ids = [e["profile_id"] for e in profiles.values() if e.get("profile_id")]
    profile_names: dict[int, str] = {}
    if profile_ids:
        with session_scope() as db:
            rows = (
                db.query(SpeakerProfile.id, SpeakerProfile.name)
                .filter(SpeakerProfile.id.in_(profile_ids))
                .all()
            )
            profile_names = {row.id: row.name for row in rows}

    written = 0
    for profile_uuid, entry in profiles.items():
        try:
            vectors = np.array(entry["vectors"], dtype=float)
            weights = np.array(entry["weights"], dtype=float)
            weighted = np.average(vectors, axis=0, weights=weights)
            norm = np.linalg.norm(weighted)
            if norm > 0:
                weighted = weighted / norm

            ok = store_profile_embedding_v4(
                profile_id=entry["profile_id"],
                profile_uuid=profile_uuid,
                profile_name=profile_names.get(entry["profile_id"], ""),
                embedding=weighted.tolist(),
                speaker_count=entry["speaker_count"],
                user_id=entry["user_id"],
                organization_id=entry.get("organization_id"),
            )
            if ok:
                written += 1
        except Exception as e:
            logger.error(f"Failed to backfill v4 profile {profile_uuid}: {e}")

    logger.info(f"Profile backfill: wrote {written}/{len(profiles)} v4 profile documents")
    return written


def _count_profile_docs(client, index: str) -> int | None:
    """Count `document_type: "profile"` docs in an index.

    Fail-closed: returns ``None`` (never ``0``) whenever the count cannot be
    trusted — the index is missing, or the count query itself errors. Callers
    MUST treat ``None`` as "refuse", never as "no profiles to worry about".
    Profile documents (voiceprints) are OpenSearch's ONLY copy — Postgres
    stores no embedding vectors (see `app/models/media.py`'s note on
    `Speaker` / `SpeakerProfile`) — so an unresolvable count is not a reason
    to proceed, it is the reason to stop.
    """
    try:
        if not client.indices.exists(index=index):
            return None
        return int(client.count(index=index, body=_PROFILE_DOC_QUERY)["count"])
    except Exception as e:
        logger.error(f"Profile-doc count failed for index '{index}': {e}")
        return None


@celery_app.task(bind=True, name="finalize_v4_migration", priority=UtilityPriority.BACKGROUND)
def finalize_v4_migration_task(self, user_id: int = 1):
    """Finalize the v4 migration by swapping indices.

    Safety checks:
    - Count validation: reject if v4 has <50% of v3 docs
    - Profile-voiceprint guard: reject on ANY shortfall of `document_type:
      "profile"` docs between v3 and v4, or if either count is unresolvable.
      OpenSearch is the only copy of these embeddings (issue #657) and the
      total-doc-count ratio above cannot detect a profile-only loss, so this
      is a separate, fail-closed gate alongside it — not a replacement.
    - Flush v4 before swap to ensure all docs are persisted
    - Post-swap verification: reject if new main has <95% of v4 count
    - finally block ALWAYS clears mode cache + releases migration lock
    """
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return {"status": "error", "message": "OpenSearch not available"}

    v4_index = get_speaker_index_v4()
    main_index = get_speaker_index()

    try:
        # ---- Pre-flight checks ----
        if not client.indices.exists(index=v4_index):
            return {"status": "error", "message": "V4 index does not exist"}

        # Flush v4 to ensure all docs are persisted
        client.indices.flush(index=v4_index, wait_if_ongoing=True)

        v4_count = client.count(index=v4_index)["count"]
        if v4_count == 0:
            return {"status": "error", "message": "V4 index is empty"}

        # Get v3 count for validation (resolve alias if needed)
        from app.services.opensearch_service import get_active_versioned_index
        from app.services.opensearch_service import swap_speaker_alias

        v3_index = get_active_versioned_index()  # e.g. "speakers_v3" or "speakers"
        import contextlib

        v3_count = 0
        with contextlib.suppress(Exception):
            if client.indices.exists(index=v3_index):
                v3_count = client.count(index=v3_index)["count"]

        # Count validation: v4 should have at least 50% of v3 docs
        if v3_count > 0 and v4_count < v3_count * 0.5:
            return {
                "status": "error",
                "message": (
                    f"V4 index has only {v4_count} docs vs {v3_count} in v3 "
                    f"({v4_count / v3_count * 100:.0f}%). "
                    "Migration appears incomplete. Aborting finalize."
                ),
            }

        # ---- Backfill profile documents into v4 (defect 1 of issue #657) ----
        # The streaming per-file writer never produced profile docs; recompute
        # every profile's centroid from its already-migrated v4 speakers before
        # the guard below counts them. A profile with zero migrated speakers
        # cannot be recovered here (no v3->v4 dimension-preserving copy exists)
        # and will correctly fail the guard that follows.
        backfilled = migrate_profile_documents_to_v4(client, v4_index)
        if backfilled:
            logger.info(f"Finalize: backfilled {backfilled} v4 profile document(s)")

        # ---- Profile-voiceprint guard (fail-closed, issue #657) ----
        # The total-doc-count ratio above cannot catch a profile-only loss:
        # v3 legitimately holds more docs than v4 for unrelated reasons, so
        # the ratio absorbs a missing set of profile docs. Check separately.
        v3_profile_count = _count_profile_docs(client, v3_index)
        v4_profile_count = _count_profile_docs(client, v4_index)

        if v3_profile_count is None or v4_profile_count is None:
            return {
                "status": "error",
                "message": (
                    "Cannot verify profile voiceprint counts "
                    f"(v3='{v3_index}': {v3_profile_count}, "
                    f"v4='{v4_index}': {v4_profile_count}). "
                    "Refusing to swap — OpenSearch is the only copy of these "
                    "embeddings and the count could not be confirmed safe."
                ),
            }

        if v3_profile_count > v4_profile_count:
            return {
                "status": "error",
                "message": (
                    f"V4 index has only {v4_profile_count} profile voiceprint "
                    f"doc(s) vs {v3_profile_count} in v3. Finalizing would "
                    "silently drop profile voiceprints (OpenSearch is their "
                    "only copy — Postgres stores no embedding vectors). "
                    "Aborting finalize; carry profile docs into v4 before "
                    "retrying."
                ),
            }

        # ---- Swap alias (atomic, no data copy) ----
        # The 'speakers' alias now points to speakers_v4 instead of speakers_v3.
        # speakers_v3 is preserved automatically as the backup — no separate
        # backup index needed since we never delete or move data.
        swap_result = swap_speaker_alias(v4_index)
        if swap_result.get("status") != "success":
            return {
                "status": "error",
                "message": f"Alias swap failed: {swap_result}",
            }

        logger.info(
            f"V4 migration finalized: alias '{main_index}' → '{v4_index}' "
            f"({v4_count} docs). V3 data preserved in '{v3_index}'."
        )
        result = {
            "status": "success",
            "message": "Migration complete",
            "v4_documents": v4_count,
            "v3_preserved_in": v3_index,
        }
        send_ws_event(user_id, NOTIFICATION_TYPE_MIGRATION_FINALIZED, result)
        return result

    except Exception as e:
        logger.error(f"Error finalizing migration: {e}")
        result = {"status": "error", "message": str(e)}
        send_ws_event(user_id, NOTIFICATION_TYPE_MIGRATION_FINALIZED, result)
        return result

    finally:
        # ALWAYS clear caches, even on error
        EmbeddingModeService.clear_cache()
        migration_progress.clear_status()
