"""Periodic search index maintenance task.

Detects unindexed files and dispatches reindex tasks to ensure
all completed transcripts are searchable.
"""

import contextlib
import logging
from typing import Any

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import CPUPriority
from app.core.redis import get_redis
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.utils.task_lock import with_task_lock

logger = logging.getLogger(__name__)

#: Cap on how many files get a ``file_facts`` backfill dispatch per maintenance tick.
#: Bounded so a large backlog (every file completed before v390 shipped) drains over
#: several 6-hourly runs instead of flooding the nlp queue in one shot; unbounded
#: dispatch is also how a beat tick becomes an outage, the same reasoning
#: `_report_embedding_provenance` already documents for not auto-reindexing.
FACTS_BACKFILL_BATCH_SIZE = 200


def _get_indexed_uuids() -> set[str] | None:
    """Query OpenSearch to get all file UUIDs currently in the chunks index.

    Returns:
        Set of file UUID strings already indexed, or None if OpenSearch
        is unreachable / query failed (callers must handle None to avoid
        treating a query failure as "nothing is indexed").
    """
    from app.services.opensearch_service import opensearch_client

    if not opensearch_client:
        return None

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        if not opensearch_client.indices.exists(index=index_name):
            return set()

        with contextlib.suppress(Exception):
            opensearch_client.indices.refresh(index=index_name)

        response = opensearch_client.search(
            index=index_name,
            body={
                "size": 0,
                # Addendum G4: "is this file indexed?" must mean "does it have
                # CHUNKS?". Counting any document per file_uuid makes a
                # digest-only file — what a partially failed rebuild leaves —
                # look indexed, so auto-repair never fires for it.
                "query": {"bool": {"filter": [chunk_plane_clause()]}},
                "aggs": {
                    "file_uuids": {
                        "terms": {
                            "field": "file_uuid",
                            "size": 50000,
                        }
                    }
                },
            },
        )
        buckets = response.get("aggregations", {}).get("file_uuids", {}).get("buckets", [])
        return {b["key"] for b in buckets}
    except Exception as e:
        logger.warning(f"Could not check indexed files: {e}")
        return None


def _report_embedding_provenance(stats: dict[str, int | bool | str]) -> None:
    """Log when the index holds vectors from more than one embedding model (#437).

    This is the only **automatic** detector of a mixed vector space, and it is
    here because this task is the only thing that periodically asks whether the
    index is what it should be. It cannot be the ordinary write path:
    ``ensure_chunks_index_exists`` runs on every single indexing call.

    It deliberately does not act. Reindexing is the cure, but the cure is a full
    re-embed of every user's corpus, and dispatching that from a beat tick on the
    strength of one aggregation is how a health check becomes an outage. The
    remedy is ``PUT /search/models/neural/active`` or ``POST /search/models``,
    which an operator chooses to run.

    Args:
        stats: The maintenance stats dict, annotated in place.
    """
    from app.services.search.embedding_provenance import survey_embedding_models

    survey = survey_embedding_models()
    stats["embedding_provenance"] = survey.verdict
    if survey.mixed:
        logger.error(survey.describe())
    elif survey.verdict == "partially_unattributed":
        logger.info(survey.describe())


def _find_unindexed_by_user(
    completed_files: list[Any], indexed_uuids: set[str]
) -> dict[int, list[str]]:
    """Group unindexed files by user ID.

    Args:
        completed_files: List of MediaFile ORM objects with status COMPLETED.
        indexed_uuids: Set of file UUIDs already in the search index.

    Returns:
        Dict mapping user_id to list of unindexed file UUID strings.
    """
    unindexed_by_user: dict[int, list[str]] = {}
    for f in completed_files:
        file_uuid = str(f.uuid)
        if file_uuid not in indexed_uuids:
            user_id = int(f.user_id)
            if user_id not in unindexed_by_user:
                unindexed_by_user[user_id] = []
            unindexed_by_user[user_id].append(file_uuid)
    return unindexed_by_user


def _dispatch_reindex_tasks(unindexed_by_user: dict[int, list[str]]) -> None:
    """Dispatch reindex Celery tasks for each user with unindexed files.

    Args:
        unindexed_by_user: Dict mapping user_id to list of file UUIDs.
    """
    from app.tasks.reindex_task import reindex_transcripts_task

    for user_id, file_uuids in unindexed_by_user.items():
        try:
            reindex_transcripts_task.delay(
                user_id=user_id,
                file_uuids=file_uuids,
            )
            logger.info(f"Dispatched reindex for user {user_id}: {len(file_uuids)} files")
        except Exception as e:
            logger.error(f"Failed to dispatch reindex for user {user_id}: {e}")


#: Lock key for this task. Matches the registered Celery task name, the same
#: convention ``recovery.periodic_health_check_task`` follows.
MAINTENANCE_LOCK_KEY = "search_index_maintenance"


@celery_app.task(name="search_index_maintenance", priority=CPUPriority.MAINTENANCE)
@with_task_lock(MAINTENANCE_LOCK_KEY, timeout=120)
def search_index_maintenance_task() -> dict[str, Any]:
    """
    Check for completed files missing from the search index and trigger re-indexing.

    This task runs periodically via Celery Beat and on startup to ensure
    all completed transcripts are searchable. This handles:
    - First-time setup: existing files before search feature was added
    - Failed indexing: files where chunk indexing failed during transcription
    - Index recovery: after OpenSearch data loss or index recreation

    Concurrency is held off by ``with_task_lock`` rather than a hand-rolled
    ``SET NX``: the manual version let a ``redis.RedisError`` propagate out of
    the task, where ``TaskLockManager`` fails open and still does the work.

    Returns:
        Dict with maintenance stats, or ``with_task_lock``'s ``{"skipped": True,
        ...}`` when another pass already holds the lock.
    """
    return _run_search_maintenance()


def _dispatch_facts_backfill(
    db: Any, stats: dict[str, int | bool | str], *, batch_size: int = FACTS_BACKFILL_BATCH_SIZE
) -> None:
    """Dispatch artifact generation for completed files with no ``file_facts`` row.

    Files that finished transcription before ``file_facts`` (v390, #383 Phase 2) existed
    permanently lack it: nothing else ever revisits a COMPLETED file, so without this arm
    the coverage map's INNER JOIN (now an outer join, see
    ``services/chat/mapreduce.scope_digest_hits``) would keep dropping them on every
    upgraded deployment forever, and a later "we covered every file in scope" guarantee
    would be false on any library that predates this table.

    Covers **all users** — this is server-side maintenance over the whole installation,
    not a per-owner operation like the reindex arm above. Ordered newest-first
    (``MediaFile.id`` descending, ids are time-ordered UUIDv7-adjacent auto-increment) so
    a large backlog backfills the most recently completed — and most likely to be
    actively viewed — files first.

    Args:
        db: Open session (short-lived; this function only reads and dispatches).
        stats: The maintenance stats dict, annotated in place with
            ``missing_facts_files`` (files found missing a row, before the batch cap) and
            ``facts_backfill_dispatched`` (how many were actually dispatched this tick).
        batch_size: Cap on dispatches this tick. Parameterized so tests can exercise the
            cap without monkeypatching the module constant.
    """
    from sqlalchemy import exists
    from sqlalchemy import select

    from app.models.file_facts import FileFacts
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.tasks.ingest_artifacts_task import dispatch_file_facts

    has_segments = exists(
        select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
    )
    has_facts = exists(select(FileFacts.id).where(FileFacts.media_file_id == MediaFile.id))

    base_query = db.query(MediaFile.id).filter(
        MediaFile.status == FileStatus.COMPLETED, has_segments, ~has_facts
    )
    # order_by(None): a COUNT does not need the ORDER BY the id list below uses —
    # same reasoning as `utils/pagination.paginate()`.
    stats["missing_facts_files"] = base_query.order_by(None).count()
    stats["facts_backfill_dispatched"] = 0
    if not stats["missing_facts_files"]:
        return

    file_ids = [
        int(row[0]) for row in base_query.order_by(MediaFile.id.desc()).limit(batch_size).all()
    ]

    for file_id in file_ids:
        # dispatch_file_facts already contains its own try/except — a broker hiccup on
        # one file must not stop the rest of the batch from being dispatched.
        dispatch_file_facts(file_id)
    stats["facts_backfill_dispatched"] = len(file_ids)
    logger.info(
        "file_facts backfill: dispatched artifact generation for %d completed file(s) "
        "with no file_facts row (batch capped at %d)",
        len(file_ids),
        batch_size,
    )


def _is_reindex_running() -> bool:
    """Check if a reindex is already in progress (any user)."""
    r = get_redis()
    for _key in r.scan_iter(match="reindex_lock:*"):
        return True
    return False


def _run_search_maintenance() -> dict[str, Any]:
    """Inner implementation of search index maintenance.

    Guards against redundant work:
    - Skips dispatch if a reindex is already running for any user.
    """
    from sqlalchemy import exists
    from sqlalchemy import select

    from app.db.session_utils import session_scope
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    stats: dict[str, int | bool | str] = {
        "total_completed_files": 0,
        "indexed_files": 0,
        "unindexed_files": 0,
        "reindex_triggered": False,
        "missing_facts_files": 0,
        "facts_backfill_dispatched": 0,
    }

    try:
        # file_facts backfill runs independent of the reindex-in-progress guard below:
        # it dispatches to the nlp queue, never cpu, so it cannot collide with a chunk
        # reindex, and skipping it whenever a reindex happens to be in flight would mean
        # an upgraded library only gets backfilled on a lucky tick.
        with session_scope() as db:
            _dispatch_facts_backfill(db, stats)

        # Don't dispatch reindex if one is already running
        if _is_reindex_running():
            logger.info("Reindex already in progress, skipping maintenance dispatch")
            stats["reindex_triggered"] = False
            return stats

        with session_scope() as db:
            # Only consider completed files that have transcript segments
            has_segments = exists(
                select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
            )
            completed_files = (
                db.query(MediaFile.uuid, MediaFile.user_id)
                .filter(MediaFile.status == FileStatus.COMPLETED, has_segments)
                .all()
            )

            if not completed_files:
                logger.info("No completed files found, nothing to maintain")
                return stats

            stats["total_completed_files"] = len(completed_files)

            indexed_uuids = _get_indexed_uuids()
            if indexed_uuids is None:
                logger.error("Cannot query OpenSearch — skipping search maintenance")
                stats["error"] = "opensearch_query_failed"
                return stats
            stats["indexed_files"] = len(indexed_uuids)
            _report_embedding_provenance(stats)

            unindexed_by_user = _find_unindexed_by_user(completed_files, indexed_uuids)
            total_unindexed = sum(len(uuids) for uuids in unindexed_by_user.values())
            stats["unindexed_files"] = total_unindexed

            if total_unindexed == 0:
                logger.info(f"All {stats['total_completed_files']} completed files are indexed")
                return stats

            logger.info(
                f"Found {total_unindexed} unindexed files across "
                f"{len(unindexed_by_user)} users. Dispatching reindex tasks."
            )

            _dispatch_reindex_tasks(unindexed_by_user)
            stats["reindex_triggered"] = True

    except Exception as e:
        logger.error(f"Search maintenance task failed: {e}")
        stats["error"] = str(e)

    return stats
