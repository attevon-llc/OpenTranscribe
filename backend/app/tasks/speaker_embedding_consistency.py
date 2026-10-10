"""Speaker embedding consistency check and self-healing task.

Detects speakers in PostgreSQL that are missing from the OpenSearch speakers
index (v3 and/or v4) and re-extracts their embeddings from the original audio.

Architecture:
- Orchestrator (CPU queue): lightweight detection via set comparison, dispatches
  GPU batch tasks only when gaps are found.
- GPU batch worker: reuses migration_pipeline for I/O-pipelined extraction.
- Periodic beat schedule: runs every 10 minutes for fast gap detection.

Redis keys:
- embedding_consistency_running      — lock (1hr TTL)
- embedding_consistency_last_run     — last results JSON (7-day TTL)
- embedding_consistency_progress     — current progress data
"""

import contextlib
import json
import logging
import time
from typing import Any

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_PROGRESS
from app.core.constants import CPUPriority
from app.core.constants import GPUPriority
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v3
from app.core.constants import get_speaker_index_v4
from app.core.redis import get_redis
from app.db.session_utils import session_scope
from app.tasks.opensearch_integrity_task import _exceeds_delete_ratio
from app.tasks.opensearch_integrity_task import _speaker_uuids_present
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

_REDIS_LOCK_KEY = "embedding_consistency_running"
_REDIS_LAST_RUN_KEY = "embedding_consistency_last_run"
_REDIS_PROGRESS_KEY = "embedding_consistency_progress"
_REDIS_BATCH_IDS_KEY = "embedding_consistency:batch_task_ids"
_LOCK_TTL = 7200  # 2 hours — must outlast the longest possible repair run
_BATCH_SIZE = 25


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _get_pg_speaker_uuids_with_segments() -> dict[str, int]:
    """Return {speaker_uuid: media_file_id} for speakers that have segments."""
    from sqlalchemy import exists
    from sqlalchemy import select

    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    with session_scope() as db:
        has_segments = exists(
            select(TranscriptSegment.id).where(TranscriptSegment.speaker_id == Speaker.id)
        )
        rows = db.query(Speaker.uuid, Speaker.media_file_id).filter(has_segments).all()
        return {str(uuid): int(fid) for uuid, fid in rows}


def _get_opensearch_speaker_uuids(index_name: str) -> set[str] | None:
    """Scroll all speaker_uuid values from an OpenSearch index.

    Returns:
        Set of speaker UUID strings, or None if OpenSearch is unreachable
        or the query failed (callers must handle None to avoid treating
        a query failure as "nothing is indexed").
    """
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return None

    if not client.indices.exists(index=index_name):
        return set()

    try:
        resp = client.search(
            index=index_name,
            body={
                "size": 0,
                "aggs": {
                    "speaker_uuids": {
                        "terms": {"field": "speaker_uuid", "size": 100000},
                    }
                },
            },
        )
        buckets = resp.get("aggregations", {}).get("speaker_uuids", {}).get("buckets", [])
        return {b["key"] for b in buckets}
    except Exception as e:
        logger.warning("Failed to query speaker UUIDs from %s: %s", index_name, e)
        return None


def _filter_unrepairable_speakers(missing_uuids: set[str]) -> set[str]:
    """Identify speakers whose segments are too short to ever extract embeddings.

    A speaker is unrepairable if, after merging adjacent segments and selecting
    the top ones, no segment meets the minimum duration threshold (0.5s).
    These speakers will always appear as "missing" but can never be fixed.
    """
    from collections import defaultdict

    from app.core.constants import SPEAKER_SHORT_SEGMENT_MIN_DURATION
    from app.services.audio_segment_utils import merge_adjacent_segments
    from app.services.audio_segment_utils import select_top_segments

    if not missing_uuids:
        return set()

    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    unrepairable: set[str] = set()

    with session_scope() as db:
        # Look up speaker IDs for missing UUIDs
        rows = db.query(Speaker.id, Speaker.uuid).filter(Speaker.uuid.in_(missing_uuids)).all()
        uuid_by_id = {r[0]: str(r[1]) for r in rows}
        speaker_ids = list(uuid_by_id.keys())

        if not speaker_ids:
            return set()

        # Batch-fetch all segments for all missing speakers in one query
        seg_rows = (
            db.query(
                TranscriptSegment.speaker_id,
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
            )
            .filter(TranscriptSegment.speaker_id.in_(speaker_ids))
            .all()
        )

        # Group segments by speaker_id
        segs_by_speaker: dict[int, list[dict]] = defaultdict(list)
        for speaker_id, start, end in seg_rows:
            segs_by_speaker[speaker_id].append({"start": float(start), "end": float(end)})

        for speaker_id, speaker_uuid in uuid_by_id.items():
            segs = segs_by_speaker.get(speaker_id)
            if not segs:
                unrepairable.add(speaker_uuid)
                continue

            merged = merge_adjacent_segments(segs)
            selected = select_top_segments(merged, min_duration=SPEAKER_SHORT_SEGMENT_MIN_DURATION)
            if not selected:
                unrepairable.add(speaker_uuid)

    return unrepairable


def _group_by_file(missing_uuids: set[str], uuid_to_file: dict[str, int]) -> dict[str, list[str]]:
    """Group missing speaker UUIDs by their media file UUID.

    Returns {file_uuid: [speaker_uuid, ...]} so the GPU pipeline can process
    per-file (it needs the audio source).
    """
    from app.models.media import MediaFile

    # Collect unique file IDs
    file_ids = {uuid_to_file[u] for u in missing_uuids if u in uuid_to_file}
    if not file_ids:
        return {}

    # Map file_id -> file_uuid
    with session_scope() as db:
        rows = db.query(MediaFile.id, MediaFile.uuid).filter(MediaFile.id.in_(file_ids)).all()
        fid_to_fuuid = {int(r[0]): str(r[1]) for r in rows}

    grouped: dict[str, list[str]] = {}
    for speaker_uuid in missing_uuids:
        fid = uuid_to_file.get(speaker_uuid)
        if fid is None:
            continue
        fuuid = fid_to_fuuid.get(fid)
        if fuuid is None:
            continue
        grouped.setdefault(fuuid, []).append(speaker_uuid)

    return grouped


# ---------------------------------------------------------------------------
# Status / counts helpers
# ---------------------------------------------------------------------------


def get_embedding_consistency_status() -> dict[str, Any]:
    """Running state + last run results (from Redis)."""
    r = get_redis()
    running = bool(r.exists(_REDIS_LOCK_KEY))

    progress_raw = r.get(_REDIS_PROGRESS_KEY)
    progress = json.loads(progress_raw) if progress_raw else None

    last_run_raw = r.get(_REDIS_LAST_RUN_KEY)
    last_run = json.loads(last_run_raw) if last_run_raw else None

    return {"running": running, "progress": progress, "last_run": last_run}


def _get_all_pg_speaker_uuids() -> set[str]:
    """Return all Speaker UUIDs in PostgreSQL (with or without segments)."""
    from app.models.media import Speaker

    with session_scope() as db:
        rows = db.query(Speaker.uuid).all()
        return {str(row[0]) for row in rows}


def get_embedding_consistency_counts() -> dict[str, Any]:
    """Dry-run: count missing speakers per index without fixing."""
    pg_speakers = _get_pg_speaker_uuids_with_segments()
    pg_uuids = set(pg_speakers.keys())

    # Also get ALL speaker UUIDs (for orphan detection)
    all_pg_uuids = _get_all_pg_speaker_uuids()

    # Query the concrete versioned indices (not the alias) for consistency checks
    v3_index = get_speaker_index_v3()
    v4_index = get_speaker_index_v4()

    from app.services.embedding_mode_service import EmbeddingModeService
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    current_mode = EmbeddingModeService.get_current_mode()

    # Query the active main index (alias resolves to correct versioned index)
    main_index = get_speaker_index()
    os_main = _get_opensearch_speaker_uuids(main_index)
    if os_main is None:
        return {"error": "opensearch_query_failed", "total_pg_speakers": len(pg_uuids)}
    missing_main = pg_uuids - os_main

    # Also query v3 concrete index directly if it exists and is different from alias target
    os_v3: set[str] = set()
    if client and client.indices.exists(index=v3_index):
        os_v3 = _get_opensearch_speaker_uuids(v3_index) or set()

    # Speakers in OS that are valid PG speakers but have no segments
    os_no_segments = (os_main & all_pg_uuids) - pg_uuids

    # True orphans: in OS but not in PG at all
    os_orphans = os_main - all_pg_uuids

    v4_active = current_mode == "v4"
    v4_exists = v4_active and bool(client and client.indices.exists(index=v4_index))

    os_v4: set[str] = set()
    missing_v4_set: set[str] = set()
    if v4_exists:
        _os_v4_result = _get_opensearch_speaker_uuids(v4_index)
        if _os_v4_result is None:
            logger.warning("Cannot query OpenSearch v4 index — reporting v3 data only")
        else:
            os_v4 = _os_v4_result
        missing_v4_set = pg_uuids - os_v4

    # Identify unrepairable speakers (segments too short for embedding extraction)
    # Filter both main (v3/v4 alias target) and v4 staging consistently
    all_missing = missing_main | missing_v4_set
    unrepairable = _filter_unrepairable_speakers(all_missing)

    return {
        "total_pg_speakers": len(pg_uuids),
        "mode": current_mode,
        "main_indexed": len(os_main & pg_uuids),
        "main_missing": len(missing_main - unrepairable),
        "v3_indexed": len(os_v3 & pg_uuids) if os_v3 else 0,
        "v4_indexed": len(os_v4 & pg_uuids),
        "unrepairable": len(unrepairable),
        "no_segments": len(os_no_segments),
        "orphans": len(os_orphans),
        "v4_exists": v4_exists,
        "v4_missing": len(missing_v4_set - unrepairable),
    }


def _select_voiceprints_to_remove(
    os_all: set[str], pg_with_segments: set[str], all_pg: set[str]
) -> tuple[set[str], set[str]]:
    """Which indexed voiceprints the consistency sweep may delete: ``(stale, orphans)``.

    ``stale`` are speakers that exist but have no segments; ``orphans`` are speakers
    Postgres does not hold at all. The same two refusals the index orphan sweep has
    (``opensearch_integrity_task``), plus its pre-delete re-check, because this runs
    every 10 minutes and voiceprints are biometric data that is NOT rebuildable from
    Postgres (re-extraction needs the audio and a GPU):

    * an EMPTY Postgres side deletes nothing — every voiceprint would be an "orphan",
      and an empty database looks exactly like a lost one;
    * an orphan Postgres holds by the time it is re-asked (a speaker created after the
      snapshot and already indexed) is kept;
    * a selection exceeding the ratio guard deletes nothing.
    """
    stale = (os_all - pg_with_segments) & all_pg
    orphans = os_all - all_pg
    if orphans and not all_pg:
        logger.error(
            "Refusing voiceprint orphan cleanup: Postgres holds NO speakers, which would "
            "make all %d indexed voiceprints orphans.",
            len(orphans),
        )
        orphans = set()
    elif orphans:
        orphans -= _speaker_uuids_present(orphans)
    if _exceeds_delete_ratio(len(os_all), len(stale) + len(orphans)):
        logger.error(
            "Refusing voiceprint cleanup: it would remove %d of %d indexed voiceprints. "
            "Verify Postgres is intact before removing them.",
            len(stale) + len(orphans),
            len(os_all),
        )
        return set(), set()
    return stale, orphans


# ---------------------------------------------------------------------------
# Orchestrator task (CPU queue)
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="speaker_embedding_consistency_check",
    priority=CPUPriority.MAINTENANCE,
)
def speaker_embedding_consistency_check_task(
    self, manual: bool = False, user_id: int = 1
) -> dict[str, Any]:
    """Detect speakers missing from OpenSearch and dispatch GPU repair batches.

    Args:
        manual: If True, triggered by admin UI (always runs even if recently ran).
        user_id: Admin user ID for WebSocket notifications (default 1 for beat schedule).
    """
    r = get_redis()

    # Guard against concurrent runs
    if not r.set(_REDIS_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL):
        logger.info("Embedding consistency check already running, skipping")
        return {"status": "already_running"}

    # Don't run if a migration, reindex, or related heavy task is in progress.
    # Check both the migration progress service AND Redis keys that indicate
    # active batch work (the progress service only tracks the orchestrator).
    from app.services.migration_progress_service import migration_progress

    if migration_progress.is_running():
        r.delete(_REDIS_LOCK_KEY)
        logger.info("Migration in progress, skipping consistency check")
        return {"status": "skipped", "reason": "migration_running"}

    # Check for active v4 embedding migration batches or reindex
    skip_patterns = ["task_progress:embedding_migration*", "reindex_lock:*"]
    for pattern in skip_patterns:
        for _key in r.scan_iter(match=pattern):
            r.delete(_REDIS_LOCK_KEY)
            reason = "embedding_migration_running" if "embedding" in pattern else "reindex_running"
            logger.info("%s in progress, skipping consistency check", reason)
            return {"status": "skipped", "reason": reason}

    start_time = time.time()
    try:
        # Phase 1: Detection (lightweight — set comparisons against concrete indices)
        pg_speakers = _get_pg_speaker_uuids_with_segments()
        pg_uuids = set(pg_speakers.keys())
        all_pg_uuids = _get_all_pg_speaker_uuids()

        from app.services.embedding_mode_service import EmbeddingModeService
        from app.services.opensearch_service import get_opensearch_client
        from app.services.opensearch_service import remove_speaker_embedding

        client = get_opensearch_client()
        current_mode = EmbeddingModeService.get_current_mode()

        # Query the main alias (resolves to the active versioned index)
        main_index = get_speaker_index()
        os_main = _get_opensearch_speaker_uuids(main_index)
        if os_main is None:
            r.delete(_REDIS_LOCK_KEY)
            logger.error("Cannot query main speaker index — skipping consistency check")
            return {"status": "skipped", "reason": "opensearch_query_failed"}
        missing_v3 = pg_uuids - os_main

        # Check v4 if the system is actively using v4 mode
        v4_index = get_speaker_index_v4()
        v4_active = current_mode == "v4"
        v4_exists = v4_active and bool(client and client.indices.exists(index=v4_index))
        missing_v4: set[str] = set()
        os_v4: set[str] = set()
        if v4_exists:
            _os_v4_result = _get_opensearch_speaker_uuids(v4_index)
            if _os_v4_result is None:
                r.delete(_REDIS_LOCK_KEY)
                logger.error("Cannot query OpenSearch v4 index — skipping consistency check")
                return {"status": "skipped", "reason": "opensearch_query_failed"}
            os_v4 = _os_v4_result
            missing_v4 = pg_uuids - os_v4

        # Phase 1b: Cleanup stale OS entries (speakers indexed but no longer
        # have segments, or don't exist in PG at all). This is CPU-only work.
        # Check both v3 and v4 indices. remove_speaker_embedding() deletes from both.
        os_all = os_main | os_v4
        stale_uuids, orphan_uuids = _select_voiceprints_to_remove(os_all, pg_uuids, all_pg_uuids)
        cleanup_uuids = stale_uuids | orphan_uuids

        cleaned = 0
        if cleanup_uuids:
            for uuid in cleanup_uuids:
                try:
                    remove_speaker_embedding(uuid)
                    cleaned += 1
                except Exception as e:
                    logger.warning("Failed to remove stale speaker %s: %s", uuid, e)

            logger.info(
                "Cleaned %d stale/orphan speaker embeddings from OS (%d no-segment, %d orphan)",
                cleaned,
                len(stale_uuids),
                len(orphan_uuids),
            )

        total_missing = len(missing_v3) + len(missing_v4)

        if total_missing == 0:
            # Healthy — no gaps
            r.delete(_REDIS_LOCK_KEY)
            duration = round(time.time() - start_time, 1)
            last_run = {
                "timestamp": time.time(),
                "status": "healthy",
                "v3_missing": 0,
                "v4_missing": 0,
                "cleaned": cleaned,
                "total_pg_speakers": len(pg_uuids),
                "duration_seconds": duration,
            }
            r.set(_REDIS_LAST_RUN_KEY, json.dumps(last_run), ex=86400 * 7)
            logger.info(
                "Embedding consistency check: healthy (%d speakers, %d cleaned, %.1fs)",
                len(pg_uuids),
                cleaned,
                duration,
            )
            return {
                "status": "healthy",
                "total_pg_speakers": len(pg_uuids),
                "cleaned": cleaned,
            }

        # Filter out unrepairable speakers (segments too short for embeddings)
        unrepairable = _filter_unrepairable_speakers(missing_v3 | missing_v4)
        missing_v3 -= unrepairable
        missing_v4 -= unrepairable
        total_missing = len(missing_v3) + len(missing_v4)

        if total_missing == 0:
            # All missing speakers are unrepairable — nothing to fix
            r.delete(_REDIS_LOCK_KEY)
            duration = round(time.time() - start_time, 1)
            last_run = {
                "timestamp": time.time(),
                "status": "healthy",
                "v3_missing": 0,
                "v4_missing": 0,
                "unrepairable": len(unrepairable),
                "total_pg_speakers": len(pg_uuids),
                "duration_seconds": duration,
            }
            r.set(_REDIS_LAST_RUN_KEY, json.dumps(last_run), ex=86400 * 7)
            logger.info(
                "Embedding consistency check: healthy (%d speakers, %d unrepairable, %.1fs)",
                len(pg_uuids),
                len(unrepairable),
                duration,
            )
            return {
                "status": "healthy",
                "total_pg_speakers": len(pg_uuids),
                "unrepairable": len(unrepairable),
            }

        logger.info(
            "Embedding consistency: %d missing from v3, %d missing from v4, %d unrepairable",
            len(missing_v3),
            len(missing_v4),
            len(unrepairable),
        )

        # Phase 2: Group by file and dispatch GPU batch tasks
        # Combine missing sets — the batch worker will write to whichever
        # indices each speaker is missing from
        all_missing = missing_v3 | missing_v4
        grouped = _group_by_file(all_missing, pg_speakers)

        if not grouped:
            r.delete(_REDIS_LOCK_KEY)
            return {"status": "no_repairable", "missing": total_missing}

        file_uuids = list(grouped.keys())
        total_files = len(file_uuids)

        # Initialize ProgressTracker for ETA + queue status integration
        from app.services.progress_tracker import ProgressTracker

        tracker = ProgressTracker(
            task_type="embedding_consistency",
            user_id=user_id,
            total=total_files,
        )
        tracker.start(message="Starting embedding consistency repair...")

        # Store progress (include user_id so batch workers can send notifications)
        progress_data = {
            "total_files": total_files,
            "processed_files": 0,
            "v3_missing": len(missing_v3),
            "v4_missing": len(missing_v4),
            "v4_exists": v4_exists,
            "unrepairable": len(unrepairable),
            "failed_files": [],
            "repaired": 0,
            "skipped": 0,
            "running": True,
            "start_time": time.time(),
            "user_id": user_id,
        }
        r.set(_REDIS_PROGRESS_KEY, json.dumps(progress_data), ex=_LOCK_TTL)

        # Send initial progress
        send_ws_event(user_id, NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_PROGRESS, progress_data)

        # Dispatch batches
        from app.tasks.embedding_consistency_repair import (
            speaker_embedding_consistency_repair_batch_task,
        )

        batches = [file_uuids[i : i + _BATCH_SIZE] for i in range(0, len(file_uuids), _BATCH_SIZE)]

        batch_task_ids = []
        for batch_idx, batch in enumerate(batches):
            result = speaker_embedding_consistency_repair_batch_task.apply_async(
                kwargs={
                    "file_uuids": batch,
                    "batch_index": batch_idx,
                    "total_batches": len(batches),
                    "total_files": total_files,
                    "missing_v3_uuids": list(missing_v3),
                    "missing_v4_uuids": list(missing_v4) if v4_exists else [],
                    "v4_exists": v4_exists,
                },
                priority=GPUPriority.ADMIN_MIGRATION,
            )
            batch_task_ids.append(result.id)

        # Store batch IDs for revocation
        r.set(_REDIS_BATCH_IDS_KEY, json.dumps(batch_task_ids), ex=86400)

        logger.info("Dispatched %d repair batches for %d files", len(batches), total_files)
        return {
            "status": "repairing",
            "total_files": total_files,
            "v3_missing": len(missing_v3),
            "v4_missing": len(missing_v4),
            "batches": len(batches),
        }

    except Exception as e:
        logger.error("Embedding consistency check failed: %s", e, exc_info=True)
        r.delete(_REDIS_LOCK_KEY)
        send_ws_event(
            user_id,
            NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_COMPLETE,
            {"status": "error", "error": "Embedding consistency check failed"},
        )
        return {"status": "error", "error": str(e)}


def stop_consistency_repair() -> dict[str, Any]:
    """Cancel a running consistency repair by revoking batch tasks."""
    from app.services.progress_tracker import ProgressTracker

    r = get_redis()

    if not r.exists(_REDIS_LOCK_KEY):
        return {"status": "not_running"}

    # Retrieve user_id from progress data (stored by orchestrator)
    raw = r.get(_REDIS_PROGRESS_KEY)
    notify_user_id = 1
    if raw:
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            notify_user_id = json.loads(raw).get("user_id", 1)

    # Revoke pending batch tasks
    batch_ids_raw = r.get(_REDIS_BATCH_IDS_KEY)
    revoked = 0
    if batch_ids_raw:
        batch_ids = json.loads(batch_ids_raw)
        for tid in batch_ids:
            celery_app.control.revoke(tid, terminate=True)
            revoked += 1
        r.delete(_REDIS_BATCH_IDS_KEY)

    # Clear ProgressTracker so queue status clears
    tracker = ProgressTracker(
        task_type="embedding_consistency",
        user_id=notify_user_id,
        total=0,
    )
    tracker.complete(message="Stopped")

    # Clear lock and progress
    r.delete(_REDIS_LOCK_KEY)
    r.delete(_REDIS_PROGRESS_KEY)

    send_ws_event(
        notify_user_id,
        NOTIFICATION_TYPE_EMBEDDING_CONSISTENCY_COMPLETE,
        {"status": "stopped"},
    )

    return {"status": "stopped", "revoked_tasks": revoked}
