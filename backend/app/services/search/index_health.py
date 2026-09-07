"""Health and repair for the chunk plane's vector index (issue #540).

Why this lives in ``services/search`` and not beside
``opensearch_service.check_and_repair_indices``
-----------------------------------------------------------------------------
Repairing ``transcript_chunks`` needs ``ensure_chunks_index_exists``, the read
alias name, and the reindex coordinator — all of which live in this package.
``opensearch_service`` is the layer *below* this one: ``indexing_service``
imports it, and every other module here does too.

Putting the repair in ``opensearch_service/repair.py`` therefore created a
package **cycle** (``opensearch_service.repair`` -> ``search.indexing_service``
-> ``opensearch_service``). It works at runtime because the import is inside a
function, and it is still wrong: mypy resolves a cycle by degrading the imported
module to ``Any``, which silently deleted type checking for **66 call sites**
across ``indexing_service`` and ``reindex_task`` — including every
``opensearch_client.indices.…`` access. A guard that stops guarding is worse
than no guard.

So the dependency runs one way only. ``opensearch_service`` owns *detection*
(``probe_knn_health``, which needs nothing but the client); this module owns
*repair*, and the callers that already reach for both — the API startup hook and
the periodic health-check task — invoke them in sequence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.config import settings
from app.services.opensearch_service import probe_knn_health
from app.services.opensearch_service import reset_knn_health_cache
from app.services.search.indexing_service import CHUNKS_ALIAS_NAME
from app.services.search.indexing_service import ensure_chunks_index_exists

if TYPE_CHECKING:
    from app.services.opensearch_service.client import KnnProbeResult

logger = logging.getLogger(__name__)

#: WebSocket event type for search-index health alerts, consumed by the frontend
#: notification store alongside ``backup_status`` / ``media_mirror_status``.
SEARCH_HEALTH_EVENT_TYPE = "search_index_health"

#: How long one holder may own the structure lock while rebuilding.
STRUCTURE_LOCK_TIMEOUT_SECONDS = 60


def corruption_notice_key(index_name: str) -> str:
    """SystemSettings key recording that admins were told about *index_name*."""
    return f"search.index_corruption_notice_sent.{index_name}"


def repair_is_supervised(index_name: str) -> bool:
    """Whether repairing *index_name* needs a human rather than a beat tick.

    Repairing the chunk plane deletes it and re-embeds every owner's corpus. On a
    relaxed deployment that is the desired self-heal — the alternative is chat
    silently answering ungrounded on every turn. On a hardened one it is a
    capacity event an operator should choose, so the fault is surfaced instead.

    Gated on ``settings.is_hardened``, never on ``ENVIRONMENT == "production"``
    (repo-wide rule): every relaxed environment name must behave the same way.

    Args:
        index_name: Index whose repair is being considered.

    Returns:
        True when the repair must be surfaced rather than performed.
    """
    if index_name != settings.OPENSEARCH_CHUNKS_INDEX:
        return False
    return bool(settings.is_hardened)


def notify_corruption(index_name: str, probe: KnnProbeResult) -> None:
    """Tell every admin the vector plane is broken — at most once per outage.

    Best-effort: an alerting failure must never mask the health check itself.

    Args:
        index_name: The corrupted index.
        probe: The verdict that produced the alarm.
    """
    from app.db.base import SessionLocal
    from app.services import system_settings_service as sss
    from app.services.backup_alerts import _notify_admins

    key = corruption_notice_key(index_name)
    try:
        with SessionLocal() as db:
            if sss.get_setting_bool(db, key, False):
                logger.warning(
                    f"Vector plane of {index_name} still corrupt; admins already notified"
                )
                return
            _notify_admins(
                db,
                status="failed",
                message=(
                    f"Search index '{index_name}' has a corrupted vector plane: every "
                    f"semantic/hybrid query is failing while keyword search still works. "
                    f"AI chat will answer without retrieved context until this is repaired. "
                    f"Rebuild it from Settings → Search, or POST /api/search/reindex. "
                    f"({probe.detail})"
                ),
                event_type=SEARCH_HEALTH_EVENT_TYPE,
            )
            sss.set_setting(
                db,
                key,
                True,
                f"Admins were notified that '{index_name}' has a corrupted vector plane",
            )
    except Exception as e:  # noqa: BLE001 - alerting must never break the health check
        logger.error(f"Could not notify admins about {index_name} corruption: {e}")


def clear_corruption_notice(index_name: str) -> None:
    """Re-arm the corruption alert once *index_name* answers again.

    Without this the one-shot flag latches forever: the first outage is reported,
    it is fixed by hand, and every subsequent genuine outage is swallowed in
    silence. That is the same "state assumed to be truth" failure the probe
    itself exists to close, so the clear runs on **every** healthy tick,
    regardless of whether this deployment would have alerted.

    Args:
        index_name: Index that has just probed serviceable.
    """
    from app.db.base import SessionLocal
    from app.services import system_settings_service as sss

    key = corruption_notice_key(index_name)
    try:
        with SessionLocal() as db:
            if not sss.get_setting_bool(db, key, False):
                return
            sss.set_setting(db, key, False, f"'{index_name}' vector plane recovered")
            logger.info(f"Vector plane of {index_name} recovered; corruption alert re-armed")
    except Exception as e:  # noqa: BLE001 - never break the health check
        logger.error(f"Could not clear the corruption notice for {index_name}: {e}")


def rebuild_chunks_index() -> bool:
    """Delete and recreate ``transcript_chunks``, then re-embed every owner's corpus.

    The chunk plane has no per-chunk mirror in Postgres to copy back the way
    ``rebuild_speaker_index`` does — its source of truth is ``transcript_segment``,
    re-chunked and re-embedded through the neural ingest pipeline. That is exactly
    what the reindex coordinator does, so repair here is delete + recreate + fan
    the coordinator out.

    Returns:
        True if the index was recreated and reindexing was dispatched.
    """
    from app.db.base import SessionLocal
    from app.services.backup_alerts import _admin_user_ids
    from app.services.opensearch_service import opensearch_client
    from app.services.search.model_switch import dispatch_reindex_for_every_owner
    from app.utils.task_lock import task_lock_manager

    if not opensearch_client:
        return False

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    lock_key = f"opensearch_index_structure_lock:{index_name}"

    # Non-blocking: if a reindex coordinator already holds the structure lock it
    # is already rebuilding this index. Waiting would only queue a second delete
    # behind the first one's create.
    with task_lock_manager.acquire_lock(
        lock_key, timeout=STRUCTURE_LOCK_TIMEOUT_SECONDS, blocking_timeout=0
    ) as acquired:
        if not acquired:
            logger.warning(
                f"Index structure lock busy for {index_name}; another rebuild is in flight. "
                "Skipping this repair pass."
            )
            return False

        try:
            if opensearch_client.indices.exists_alias(name=CHUNKS_ALIAS_NAME):
                opensearch_client.indices.delete_alias(index=index_name, name=CHUNKS_ALIAS_NAME)
                logger.info(f"Removed alias '{CHUNKS_ALIAS_NAME}' ahead of rebuild")
        except Exception as e:  # noqa: BLE001 - a missing alias must not block the rebuild
            logger.info(f"No alias '{CHUNKS_ALIAS_NAME}' to remove before rebuild ({e})")

        opensearch_client.indices.delete(index=index_name, ignore=[404])
        logger.warning(f"Deleted corrupted index {index_name}; recreating from mapping")

        # ensure_chunks_index_exists, NOT recreate_index_for_dimension: the latter
        # short-circuits when the declared dimension already matches, which is
        # exactly the corrupt-but-correctly-sized case being repaired.
        if not ensure_chunks_index_exists():
            logger.error(f"Failed to recreate {index_name} after deletion")
            return False

        with SessionLocal() as db:
            admin_ids = _admin_user_ids(db)
        if not admin_ids:
            logger.error("No admin account to attribute the repair reindex to; not dispatching")
            return False

        result = dispatch_reindex_for_every_owner(triggered_by=admin_ids[0])
        # `reindex_users`, not `dispatched` — the helper has never returned a
        # `dispatched` key, so `.get(..., 0)` silently reported "0 owners" on
        # every repair, however many it actually fanned out to (issue #692).
        # A direct subscript would have raised the first time this ran.
        logger.warning(
            f"Dispatched repair reindex for {index_name}: {result.get('reindex_users', 0)} owners"
        )

    # The index is legitimately EMPTY until the coordinators finish, so there is
    # nothing to verify here — probing now would report "empty", and re-entering
    # the corruption check is how a delete loop starts.
    reset_knn_health_cache()
    return True


def check_and_repair_chunks_index() -> list[str]:
    """Probe the chunk plane's vector index and act on the verdict.

    ``opensearch_service.check_and_repair_indices`` deliberately does not cover
    this index: repairing it belongs to this package (see the module docstring),
    and its probe must be a real kNN query rather than the ``match_all`` that
    cannot see vector-segment corruption at all.

    Returns:
        ``[index_name]`` if it was repaired, else ``[]``.
    """
    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    probe = probe_knn_health(index_name)

    if probe.is_serviceable:
        logger.info(f"kNN health check passed: {index_name} ({probe.status})")
        clear_corruption_notice(index_name)
        return []

    if not probe.is_corrupt:
        logger.warning(
            f"kNN health check inconclusive for {index_name}: {probe.status} ({probe.detail})"
        )
        return []

    logger.error(f"Index {index_name} has a corrupted vector plane: {probe.detail}")
    if repair_is_supervised(index_name):
        notify_corruption(index_name, probe)
        return []

    if rebuild_chunks_index():
        clear_corruption_notice(index_name)
        return [index_name]

    logger.error(f"Index {index_name} could not be repaired automatically")
    return []
