"""Periodic sweep that re-dispatches completed files missing from search (issue #1182).

``index_transcript_search_task`` retries for hours, but a long outage can still spend its
budget, and a worker lost at the wrong moment can leave no row at all. This beat task is the
durable backstop: it finds completed transcripts whose latest ``search_indexing`` row is
``failed`` (or that never got one) and hands each back to the indexing task at retry priority.

Safe to run often: the batch is bounded, each file is claimed in Redis for a cooldown before it
is dispatched (so a file still waiting in the queue is not dispatched again every tick), and the
dispatch itself is debounced per file. It does nothing when ``OPENSEARCH_ENABLED=false``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import EmbeddingPriority
from app.core.constants import UtilityPriority
from app.utils.task_lock import with_task_lock

logger = logging.getLogger(__name__)

SWEEP_LOCK_KEY = "search_index_sweep"
_COOLDOWN_KEY_TEMPLATE = "search_index_sweep:{file_uuid}"


def run_search_index_sweep(db: Session) -> dict[str, Any]:
    """One sweep pass over ``db``. Returns ``{"status", "found", "dispatched"}``."""
    if not settings.OPENSEARCH_ENABLED:
        return {"status": "skipped", "reason": "opensearch_disabled"}

    from app.core.redis import get_redis
    from app.services.search.index_retry import find_files_awaiting_reindex
    from app.services.search.reindex_dispatch import dispatch_transcript_reindex

    candidates = find_files_awaiting_reindex(db, limit=settings.SEARCH_INDEX_SWEEP_BATCH_SIZE)
    dispatched = 0
    redis = get_redis()
    for file_id, file_uuid, user_id in candidates:
        key = _COOLDOWN_KEY_TEMPLATE.format(file_uuid=file_uuid)
        if not redis.set(key, "1", nx=True, ex=settings.SEARCH_INDEX_SWEEP_COOLDOWN_S):
            continue
        if dispatch_transcript_reindex(
            file_id=file_id,
            file_uuid=file_uuid,
            user_id=user_id,
            priority=EmbeddingPriority.PIPELINE_RETRY,
        ):
            dispatched += 1

    if candidates:
        logger.info(
            "Search index sweep: %d file(s) not indexed, %d re-dispatched",
            len(candidates),
            dispatched,
        )
    return {"status": "ok", "found": len(candidates), "dispatched": dispatched}


@celery_app.task(name="search_index_sweep", priority=UtilityPriority.ROUTINE)
@with_task_lock(SWEEP_LOCK_KEY, timeout=300)
def search_index_sweep_task() -> dict[str, Any]:
    """Beat entry point: run one sweep in its own short-lived session."""
    from app.db.session_utils import session_scope

    with session_scope() as db:
        return run_search_index_sweep(db)
