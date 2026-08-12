"""OpenSearch data integrity tasks.

Includes:
- opensearch_orphan_cleanup: Detects and removes orphaned OpenSearch documents
  across all indices (speakers, speakers_v4, transcripts, transcript_chunks,
  transcript_summaries) for file IDs that no longer exist in PostgreSQL.
- get_integrity_status / get_integrity_counts / get_index_overview: Query helpers
  used by the admin API to display index health.
"""

import contextlib
import json
import logging
import time
from typing import Any

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import NOTIFICATION_TYPE_DATA_INTEGRITY_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_DATA_INTEGRITY_PROGRESS
from app.core.constants import CPUPriority
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v4
from app.core.redis import get_redis
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

_REDIS_LOCK_KEY = "data_integrity_running"
_REDIS_LAST_RUN_KEY = "data_integrity_last_run"

#: Fraction of an index's documents one sweep may delete without an explicit
#: ``force``. A sweep wanting more than this has almost certainly lost the
#: PostgreSQL side of the comparison rather than found that much real garbage —
#: which is exactly the June 2026 incident shape (Postgres restored EMPTY,
#: OpenSearch intact) and this task runs from beat four times a day.
_ORPHAN_DELETE_RATIO_LIMIT = 0.10

#: Deletions at or below this many documents skip the ratio guard: in a
#: three-document index a single genuine orphan is 33% of it, and a guard that
#: blocks routine cleanup forever would just be turned off.
_ORPHAN_DELETE_FLOOR_DOCS = 25


# ---------------------------------------------------------------------------
# Orphan cleanup helpers
# ---------------------------------------------------------------------------


def _get_all_file_ids_from_db() -> set[int]:
    """Return all MediaFile IDs that exist in PostgreSQL."""
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    with session_scope() as db:
        rows = db.query(MediaFile.id).all()
        return {int(row[0]) for row in rows}


def _get_all_file_uuids_from_db() -> set[str]:
    """Return all MediaFile UUIDs that exist in PostgreSQL."""
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    with session_scope() as db:
        rows = db.query(MediaFile.uuid).all()
        return {str(row[0]) for row in rows}


def _get_all_speaker_uuids_from_db() -> set[str]:
    """Return all Speaker UUIDs that exist in PostgreSQL.

    Used to detect orphaned speaker embeddings from speakers that were
    deleted via merges, reprocessing, or direct deletion without OpenSearch
    cleanup.
    """
    from app.db.session_utils import session_scope
    from app.models.media import Speaker

    with session_scope() as db:
        rows = db.query(Speaker.uuid).all()
        return {str(row[0]) for row in rows}


def _coerce_bucket_key(key: Any, key_type: type[int] | type[str]) -> int | str | None:
    """Coerce an aggregation bucket key to the index's own identifier type.

    Args:
        key: Raw bucket key as OpenSearch returned it.
        key_type: The type this index's identifier field holds, taken from the
            index config — never inferred from an arbitrary member of the valid
            set, which is unknowable when that set is empty.

    Returns:
        The coerced key, or **None** when it cannot be coerced. None means
        "cannot be compared", and an uncomparable document is never an orphan.
    """
    try:
        return key_type(key)
    except (TypeError, ValueError):
        return None


def _exceeds_delete_ratio(total_docs: int, orphaned_docs: int) -> bool:
    """True when this deletion is too large a share of the index to do unforced.

    Args:
        total_docs: Documents in the index carrying the identifier field.
        orphaned_docs: Documents this sweep considers orphaned.

    Returns:
        True when the sweep needs an explicit force. A total of 0 with orphans
        found means the count call did not answer, so the ratio is unknowable and
        the answer is True — fail closed.
    """
    if orphaned_docs <= _ORPHAN_DELETE_FLOOR_DOCS:
        return False
    if total_docs <= 0:
        return True
    return (orphaned_docs / total_docs) > _ORPHAN_DELETE_RATIO_LIMIT


def _cleanup_index_by_field(
    client: Any,
    index_name: str,
    field_name: str,
    valid_values: set,
    key_type: type[int] | type[str],
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Remove documents from an OpenSearch index where field_name is not in valid_values.

    Two refusals stand between this function and a full index wipe, because the
    "valid" set comes from PostgreSQL and the deletion is unrecoverable:

    1. **Empty valid set.** If the query returned nothing, every document in the
       index is an orphan by this rule. A database restored empty and an install
       with no files are indistinguishable from here, so the sweep refuses and
       says so rather than acting on the guess.
    2. **Ratio guard.** Deleting more than :data:`_ORPHAN_DELETE_RATIO_LIMIT` of
       the index needs ``force=True`` — the same "name it, then confirm it"
       double opt-in as ``DELETE /tags/cleanup?scope=all_users&confirm=true``.
       Deletions of at most :data:`_ORPHAN_DELETE_FLOOR_DOCS` documents are
       exempt so ordinary cleanup on a small index still works.

    Args:
        client: OpenSearch client.
        index_name: Index to scan.
        field_name: Document field containing the file identifier.
        valid_values: Set of valid values (file IDs or UUIDs that exist in DB).
        key_type: ``int`` or ``str`` — the identifier type this index uses, from
            the caller's index config.
        dry_run: If True, count orphans without deleting. The refusals are still
            evaluated and reported, so a dry run shows what a real run would do.
        force: Override the ratio guard. Never overrides the empty-set refusal.

    Returns:
        Dict with total_docs, orphaned_docs, deleted_docs, and ``refused`` —
        None, ``"empty_valid_set"`` or ``"ratio_guard"``.
    """
    result: dict[str, Any] = {
        "total_docs": 0,
        "orphaned_docs": 0,
        "deleted_docs": 0,
        "refused": None,
    }

    if not client.indices.exists(index=index_name):
        return result

    # Count only docs that have the field we're checking (excludes profiles/clusters
    # from speaker index counts, giving accurate per-type totals)
    with contextlib.suppress(Exception):
        count_resp = client.count(
            index=index_name,
            body={"query": {"exists": {"field": field_name}}},
        )
        result["total_docs"] = count_resp.get("count", 0)

    # Refusal 1: nothing to compare against. Every document would be an orphan.
    if not valid_values:
        result["refused"] = "empty_valid_set"
        logger.error(
            f"Refusing to sweep '{index_name}.{field_name}': the PostgreSQL side of the "
            f"comparison is EMPTY, which would make all {result['total_docs']} document(s) "
            "orphans. An empty database and a lost database look identical from here."
        )
        return result

    # Use terms aggregation to find all unique file identifiers in the index
    try:
        agg_resp = client.search(
            index=index_name,
            body={
                "size": 0,
                "aggs": {
                    "file_ids": {
                        "terms": {"field": field_name, "size": 50000},
                    }
                },
            },
        )
        buckets = agg_resp.get("aggregations", {}).get("file_ids", {}).get("buckets", [])
    except Exception as e:
        logger.warning(f"Aggregation on {index_name}.{field_name} failed: {e}")
        return result

    orphan_values: list[Any] = []
    uncomparable = 0
    for bucket in buckets:
        key = bucket["key"]
        # Coerce to the identifier type THIS INDEX uses. Reading the type off an
        # arbitrary member of valid_values used to make an empty set mean "int",
        # which wiped the int-keyed summary index and raised ValueError out of
        # this loop for the UUID-keyed ones — aborting the run mid-way, after
        # earlier indices had already been deleted from.
        comparable_key = _coerce_bucket_key(key, key_type)
        if comparable_key is None:
            uncomparable += 1
            continue
        if comparable_key not in valid_values:
            orphan_values.append(key)
            result["orphaned_docs"] += bucket["doc_count"]

    if uncomparable:
        logger.error(
            f"{index_name}.{field_name}: {uncomparable} distinct key(s) are not "
            f"{key_type.__name__} and were left alone — the index may hold documents "
            "written under a different schema."
        )

    if not orphan_values:
        return result

    # Refusal 2: too large a share of the index to delete without being asked twice.
    if _exceeds_delete_ratio(result["total_docs"], result["orphaned_docs"]) and not force:
        result["refused"] = "ratio_guard"
        logger.error(
            f"Refusing to delete {result['orphaned_docs']} of {result['total_docs']} "
            f"document(s) from '{index_name}': that exceeds the "
            f"{_ORPHAN_DELETE_RATIO_LIMIT:.0%} single-sweep limit. Verify PostgreSQL is "
            "intact, then re-run with force=True if the orphans are real."
        )
        return result

    if dry_run:
        return result

    # Delete orphaned documents in batches
    for i in range(0, len(orphan_values), 100):
        batch = orphan_values[i : i + 100]
        try:
            del_resp = client.delete_by_query(
                index=index_name,
                body={"query": {"terms": {field_name: batch}}},
                refresh=True,
            )
            result["deleted_docs"] += del_resp.get("deleted", 0)
        except Exception as e:
            logger.warning(f"delete_by_query on {index_name} failed for batch: {e}")

    return result


def _notify(user_id: int | None, event_type: str, payload: dict[str, Any]) -> None:
    """Send a data-integrity event to the admin who asked for this run.

    Args:
        user_id: The requesting admin, or None for the beat-scheduled sweep that
            nobody is watching (it is logged instead).
        event_type: Notification type constant.
        payload: Event body.

    The recipient used to be a hardcoded ``1``, so every progress and completion
    event went to whichever account happens to hold id 1 — not necessarily an
    admin, and never the admin who started the run, who therefore waited forever.
    """
    if user_id is None:
        logger.debug(f"{event_type}: no requesting user for this run, logging only")
        return
    send_ws_event(user_id, event_type, payload)


def run_orphan_cleanup(
    dry_run: bool = False,
    force: bool = False,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Scan all OpenSearch indices and remove orphaned documents.

    Speaker indices are checked at the speaker UUID level (not file level)
    so that embeddings from merged or deleted speakers are caught even when
    the parent file still exists.

    Args:
        dry_run: If True, count orphans without deleting them.
        force: Override the per-index ratio guard (see
            :func:`_cleanup_index_by_field`). The empty-set refusal is not
            overridable at all.
        user_id: Admin to send progress notifications to, or None to log only.

    Returns:
        Per-index stats: ``{index_name: {total_docs, orphaned_docs, deleted_docs,
        refused}}`` plus a ``summary`` carrying the refusals.
    """
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return {"error": "OpenSearch not available"}

    valid_file_ids = _get_all_file_ids_from_db()
    valid_file_uuids = _get_all_file_uuids_from_db()
    valid_speaker_uuids = _get_all_speaker_uuids_from_db()

    # Index configs: (index_name, field_name, valid_set, key_type)
    # Speaker indices use speaker_uuid (not media_file_id) so that orphans from
    # speaker merges and reprocessing (where the file still exists but the specific
    # speaker was deleted) are also caught — not just orphans from deleted files.
    # The key type is declared here, per index, because it is a property of the
    # index mapping — not something to infer from whatever the DB query returned.
    index_configs: list[tuple[str, str, set, type[int] | type[str]]] = [
        (get_speaker_index(), "speaker_uuid", valid_speaker_uuids, str),
        (get_speaker_index_v4(), "speaker_uuid", valid_speaker_uuids, str),
        (settings.OPENSEARCH_TRANSCRIPT_INDEX, "file_uuid", valid_file_uuids, str),
        (settings.OPENSEARCH_CHUNKS_INDEX, "file_uuid", valid_file_uuids, str),
        (settings.OPENSEARCH_SUMMARY_INDEX, "file_id", valid_file_ids, int),
    ]

    results: dict[str, Any] = {}
    total_orphans = 0
    total_deleted = 0
    refusals: dict[str, str] = {}

    for idx, (index_name, field_name, valid_set, key_type) in enumerate(index_configs):
        logger.info(f"Scanning index {index_name} for orphans...")
        stats = _cleanup_index_by_field(
            client, index_name, field_name, valid_set, key_type, dry_run, force
        )
        results[index_name] = stats
        total_orphans += stats["orphaned_docs"]
        total_deleted += stats["deleted_docs"]
        if stats["refused"]:
            refusals[index_name] = stats["refused"]

        # Send progress notification
        _notify(
            user_id,
            NOTIFICATION_TYPE_DATA_INTEGRITY_PROGRESS,
            {
                "current_index": index_name,
                "processed_indices": idx + 1,
                "total_indices": len(index_configs),
                "progress": round((idx + 1) / len(index_configs) * 100),
                "running": True,
            },
        )

    results["summary"] = {
        "total_orphans_found": total_orphans,
        "total_deleted": total_deleted,
        "dry_run": dry_run,
        "force": force,
        "refused_indices": refusals,
    }
    if refusals:
        logger.error(f"Orphan cleanup refused deletion in {len(refusals)} index/indices")

    action = "Found" if dry_run else "Cleaned"
    logger.info(
        f"Orphan cleanup complete: {action} {total_orphans} orphaned documents "
        f"across {len(index_configs)} indices"
    )

    return results


@celery_app.task(name="opensearch_orphan_cleanup", priority=CPUPriority.MAINTENANCE)
def opensearch_orphan_cleanup_task(
    user_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Celery task: scan all OpenSearch indices and remove orphaned documents.

    Guarded by a Redis lock to prevent concurrent runs.

    Args:
        user_id: Admin who requested the run; receives the progress and
            completion notifications. None (the beat schedule) logs instead.
        force: Override the per-index deletion ratio guard.

    Returns:
        Per-index cleanup stats.
    """
    r = get_redis()

    # Guard against concurrent runs
    if not r.set(_REDIS_LOCK_KEY, "1", nx=True, ex=900):
        logger.info("Orphan cleanup already running, skipping")
        return {"status": "already_running"}

    start_time = time.time()
    try:
        results = run_orphan_cleanup(dry_run=False, force=force, user_id=user_id)

        # Store last run results
        last_run_data = {
            "timestamp": time.time(),
            "results": results,
            "duration_seconds": round(time.time() - start_time, 1),
        }
        r.set(_REDIS_LAST_RUN_KEY, json.dumps(last_run_data), ex=86400 * 7)  # Keep 7 days

        # Send completion notification
        _notify(
            user_id,
            NOTIFICATION_TYPE_DATA_INTEGRITY_COMPLETE,
            {
                "status": "completed",
                "results": results,
                "duration_seconds": last_run_data["duration_seconds"],
            },
        )

        return results

    except Exception as e:
        logger.error(f"Orphan cleanup task failed: {e}", exc_info=True)
        _notify(
            user_id,
            NOTIFICATION_TYPE_DATA_INTEGRITY_COMPLETE,
            {"status": "error", "error": str(e)},
        )
        return {"error": str(e)}
    finally:
        r.delete(_REDIS_LOCK_KEY)


def get_integrity_status() -> dict[str, Any]:
    """Get the current data integrity status (running? last results?).

    Returns:
        Dict with running, last_run timestamp, and last_run results.
    """
    r = get_redis()

    running = bool(r.exists(_REDIS_LOCK_KEY))

    last_run_raw = r.get(_REDIS_LAST_RUN_KEY)
    last_run = json.loads(last_run_raw) if last_run_raw else None

    return {
        "running": running,
        "last_run": last_run,
    }


def get_integrity_counts() -> dict[str, Any]:
    """Quick count-only scan (dry_run) to show orphan counts without deletion."""
    return run_orphan_cleanup(dry_run=True)


def get_index_overview() -> dict[str, Any]:
    """Return document count breakdowns for all OpenSearch indices.

    For speaker indices, breaks down by document type (speakers, profiles, clusters).
    For other indices, returns total count with a description.

    Returns:
        Dict with per-index stats including type breakdowns.
    """
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if not client:
        return {"error": "OpenSearch not available", "indices": []}

    speaker_index = get_speaker_index()
    v4_index = get_speaker_index_v4()

    # Speaker indices with type breakdowns
    speaker_configs = [
        (speaker_index, "Speaker Embeddings (V3)"),
        (v4_index, "Speaker Embeddings (V4)"),
    ]
    # Transcript indices grouped into one card
    transcript_indices = [
        (settings.OPENSEARCH_TRANSCRIPT_INDEX, "metadata"),
        (settings.OPENSEARCH_CHUNKS_INDEX, "chunks"),
    ]

    indices: list[dict[str, Any]] = []

    # --- Speaker indices ---
    for index_name, label in speaker_configs:
        entry: dict[str, Any] = {"name": index_name, "label": label, "exists": False}
        try:
            if not client.indices.exists(index=index_name):
                indices.append(entry)
                continue
        except Exception:
            indices.append(entry)
            continue

        entry["exists"] = True
        try:
            total = client.count(index=index_name)["count"]
            entry["total"] = total
            speakers = client.count(
                index=index_name,
                body={"query": {"bool": {"must_not": {"exists": {"field": "document_type"}}}}},
            )["count"]
            profiles = client.count(
                index=index_name,
                body={"query": {"term": {"document_type": "profile"}}},
            )["count"]
            clusters = client.count(
                index=index_name,
                body={"query": {"term": {"document_type": "cluster"}}},
            )["count"]
            entry["breakdown"] = {
                "speakers": speakers,
                "profiles": profiles,
                "clusters": clusters,
            }
        except Exception as e:
            entry["total"] = 0
            logger.warning(f"Failed to get counts for {index_name}: {e}")
        indices.append(entry)

    # --- Transcripts (metadata + chunks combined) ---
    tx_entry: dict[str, Any] = {
        "name": "transcripts",
        "label": "Transcripts",
        "exists": False,
        "breakdown": {},
    }
    any_tx_exists = False
    for idx_name, sub_label in transcript_indices:
        try:
            if client.indices.exists(index=idx_name):
                any_tx_exists = True
                count = client.count(index=idx_name)["count"]
                tx_entry["breakdown"][sub_label] = count
            else:
                tx_entry["breakdown"][sub_label] = 0
        except Exception:
            tx_entry["breakdown"][sub_label] = 0
    tx_entry["exists"] = any_tx_exists
    tx_entry["total"] = sum(tx_entry["breakdown"].values())
    indices.append(tx_entry)

    # --- Summaries ---
    sum_entry: dict[str, Any] = {
        "name": settings.OPENSEARCH_SUMMARY_INDEX,
        "label": "Summaries",
        "exists": False,
    }
    try:
        if client.indices.exists(index=settings.OPENSEARCH_SUMMARY_INDEX):
            sum_entry["exists"] = True
            sum_entry["total"] = client.count(index=settings.OPENSEARCH_SUMMARY_INDEX)["count"]
        else:
            sum_entry["total"] = 0
    except Exception:
        sum_entry["total"] = 0
    indices.append(sum_entry)

    # Also get PG counts for context
    pg_stats: dict[str, int] = {}
    try:
        from app.db.session_utils import session_scope
        from app.models.media import FileStatus
        from app.models.media import MediaFile
        from app.models.media import Speaker

        with session_scope() as db:
            pg_stats["completed_files"] = (
                db.query(MediaFile.id).filter(MediaFile.status == FileStatus.COMPLETED).count()
            )
            # "Active" = files with actual media (not failed downloads or cancelled)
            excluded = {
                FileStatus.ERROR,
                FileStatus.CANCELLED,
                FileStatus.QUEUED,
                FileStatus.ORPHANED,
            }
            pg_stats["active_files"] = (
                db.query(MediaFile.id).filter(MediaFile.status.notin_(excluded)).count()
            )
            pg_stats["speakers"] = db.query(Speaker.id).count()
    except Exception as e:
        logger.warning(f"Failed to get PG counts: {e}")

    return {"indices": indices, "pg_stats": pg_stats}
