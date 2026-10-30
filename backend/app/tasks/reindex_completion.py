"""Reindex completion handler and post-reindex orphan sweep."""

import contextlib
import logging
from typing import Any

from app.core.config import settings
from app.core.constants import NOTIFICATION_TYPE_REINDEX_COMPLETE
from app.core.constants import NOTIFICATION_TYPE_REINDEX_PROGRESS
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.search.reindex_cancel import clear_cancel
from app.tasks.reindex_index_ops import _force_merge_after_reindex
from app.tasks.reindex_index_ops import _refresh_index_and_clear_cache
from app.tasks.reindex_index_ops import _restore_normal_mode
from app.tasks.reindex_run_state import _release_reindex_lock
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

#: Post-reindex orphan sweep guard. A sweep that would remove more than this
#: fraction of the indexed files is refused: the input set comes from Redis
#: coordination state, and an incomplete set is indistinguishable from a corpus
#: that really is orphaned. Mirrors `opensearch_integrity_task`, deliberately —
#: two sweeps of the same shape should refuse on the same rule.
_ORPHAN_RATIO_LIMIT = 0.10

#: Files below which the ratio guard does not apply: in a three-file index one
#: genuine orphan is 33%, and a guard that blocks routine cleanup forever gets
#: turned off.
_ORPHAN_FLOOR_FILES = 5


def _handle_reindex_completion(
    redis_client: Any,
    user_id: int,
    state_key: str,
    uuids_key: str,
    run_id: str | None = None,
) -> None:
    """Final cleanup after all of one run's reindex workers complete.

    Args:
        redis_client: Redis client.
        user_id: Owner of the reindex.
        state_key: This run's state hash.
        uuids_key: This run's indexed-uuid set.
        run_id: This run's coordinator task id, used to check the per-user lock
            is still ours before releasing it. ``None`` is the legacy path.
    """
    _restore_normal_mode()
    _refresh_index_and_clear_cache()
    _force_merge_after_reindex()

    # Orphan cleanup — only safe for a FULL reindex that actually COMPLETED.
    #
    # It deletes every file in the index that is not in "the set of files this
    # run indexed", so a set that is merely *incomplete* reads as a corpus that
    # is mostly orphaned. Two ways that happens, both observed: a partial
    # reindex (specific file_uuids), and a run whose coordination state was
    # reset underneath it — the API process used to clear `reindex_state:*` on
    # every restart, which orphan-swept a 432-file corpus down to 252.
    #
    # Fail closed, the same way `opensearch_integrity_task._cleanup_index_by_field`
    # refuses an empty "valid" set: an incomplete tally cannot be distinguished
    # from a corpus that really is orphaned, so the sweep declines and says so.
    # Skipping it leaves stale documents, which the next full reindex removes.
    # Getting it wrong deletes the index, which nothing recovers.
    state = redis_client.hgetall(state_key)

    def _field(key: str, default: str = "") -> str:
        value = state.get(key.encode(), state.get(key, default))
        return value.decode() if isinstance(value, bytes) else str(value)

    if _field("partial", "0") == "1":
        logger.info(f"Skipping orphan cleanup for partial reindex (user {user_id})")
    else:
        indexed_uuids = {
            u.decode() if isinstance(u, bytes) else u for u in redis_client.smembers(uuids_key)
        }
        expected = int(_field("total", "0") or 0)
        accounted = len(indexed_uuids) + int(_field("failed", "0") or 0)
        if not indexed_uuids:
            logger.warning(
                f"Skipping orphan cleanup for user {user_id}: this run recorded NO indexed "
                f"files, so every document in the index would look orphaned."
            )
        elif not expected:
            # `total` is written once, by the coordinator, before any batch is
            # dispatched, and the coordinator returns early rather than
            # dispatching a run of zero files — so a completion that cannot read
            # it is reading a hash that expired or was reset, not a finished run.
            # Without this the tally check below is skipped (`expected` is 0) and
            # a truncated uuid set sweeps everything else away.
            logger.warning(
                f"Skipping orphan cleanup for user {user_id}: the coordination state carries "
                f"no file total, so the {len(indexed_uuids)} uuids recorded cannot be shown "
                f"to be the whole run."
            )
        elif accounted < expected:
            logger.warning(
                f"Skipping orphan cleanup for user {user_id}: only {len(indexed_uuids)} of "
                f"{expected} files are accounted for. The coordination state was reset or "
                f"a second reindex overlapped this one — sweeping now would delete the "
                f"{expected - accounted} files this run never got to."
            )
        else:
            orphan_cleaned = _cleanup_orphaned_chunks(user_id, indexed_uuids)
            if orphan_cleaned > 0:
                logger.info(f"Cleaned {orphan_cleaned} orphaned chunks for user {user_id}")

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

    # Cleanup Redis keys. The state/uuid pair belongs to this run, so it goes
    # unconditionally; the lock is user-scoped and only goes if we still hold it.
    redis_client.delete(state_key, uuids_key)
    _release_reindex_lock(redis_client, user_id, run_id)
    clear_cancel(user_id)

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
                # G4 on the detection side: a file is "present in the index" when
                # it has chunks. The DELETE below stays plane-agnostic on purpose —
                # once a file is judged orphaned, its digests must go with it.
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"user_id": user_id}},
                            chunk_plane_clause(),
                        ]
                    }
                },
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

        # Ratio guard — the backstop, and it is what the tally check in
        # `_handle_reindex_completion` cannot cover. That check compares the
        # accumulated uuid set against the coordinator's own `total`, so when a
        # SECOND coordinator resets the shared state mid-run, the truncated
        # tally and the truncated total agree with each other and it passes.
        # This one compares against the INDEX, which no reset can rewrite: a
        # "cleanup" removing most of the corpus is a coordination failure, not a
        # corpus that is mostly orphaned. Same constants and the same small-index
        # floor as `opensearch_integrity_task`, whose sweep has the same shape.
        if len(orphan_uuids) > _ORPHAN_FLOOR_FILES and (
            len(orphan_uuids) / max(len(indexed_in_os), 1) > _ORPHAN_RATIO_LIMIT
        ):
            logger.error(
                f"REFUSING post-reindex orphan cleanup for user {user_id}: it would delete "
                f"{len(orphan_uuids)} of {len(indexed_in_os)} indexed files. A reindex that "
                f"orphaned most of the corpus did not finish — run a full reindex instead. If "
                f"those files really were removed (a restore, out-of-band row deletion), "
                f"reconcile with GET /api/admin/data-integrity/counts, then "
                f"POST /api/admin/data-integrity?force=true&confirm=true."
            )
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
