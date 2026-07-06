"""Celery task for embedded SQLite search reindexing."""
from __future__ import annotations

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import CPUPriority

logger = logging.getLogger(__name__)


def run_sqlite_search_reindex(
    user_id: int,
    file_uuids: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run the embedded SQLite search coordinator."""
    from app.db.session_utils import session_scope
    from app.services.sqlite_search.orchestrator import ReindexSlice, SQLiteSearchOrchestrator
    from app.services.sqlite_search.store import SQLiteSearchStore

    coordinator = SQLiteSearchOrchestrator(SQLiteSearchStore(settings.SQLITE_SEARCH_PATH))
    index_slice = ReindexSlice(user_id=user_id, limit=limit, file_uuids=file_uuids)
    with session_scope() as db:
        return coordinator.run_reindex(db, index_slice)


@celery_app.task(
    name="sqlite_search_reindex",
    priority=CPUPriority.USER_TRIGGERED,
    max_retries=2,
    default_retry_delay=10,
)
def sqlite_search_reindex_task(
    user_id: int,
    file_uuids: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Run a bounded SQLite search reindex job."""
    logger.info("SQLite search reindex started for user %s", user_id)
    result = run_sqlite_search_reindex(user_id, file_uuids=file_uuids, limit=limit)
    logger.info("SQLite search reindex finished for user %s: %s", user_id, result.get("status"))
    return result
