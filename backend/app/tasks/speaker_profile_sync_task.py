"""Repair drift between PostgreSQL speaker profiles and their OpenSearch documents.

Runs as a Celery task, dispatched once per boot window from the API startup, instead of
inline on the startup path: at 580,000 speakers the old inline loop took 15 minutes, kept
the API unhealthy for all of it and held one database transaction open across every
OpenSearch call until the server's idle-in-transaction timeout killed it (#1337).
"""

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.queues import UtilityPriority
from app.db.base import SessionLocal
from app.utils.task_lock import with_task_lock

logger = logging.getLogger(__name__)

#: Matches the registered Celery task name, like the other ``with_task_lock`` tasks.
SPEAKER_PROFILE_SYNC_LOCK_KEY = "speaker_profile_opensearch_sync"


@celery_app.task(name="speaker_profile_opensearch_sync", priority=UtilityPriority.BACKGROUND)
@with_task_lock(SPEAKER_PROFILE_SYNC_LOCK_KEY, timeout=3600)
def speaker_profile_opensearch_sync_task() -> dict[str, Any]:
    """Bulk-sync speaker profile_id, display_name and verified into OpenSearch.

    The sync is chunked and holds no transaction across a network call (see
    ``sync_speaker_profiles_to_opensearch``), so it converges on its own over repeated
    runs and a restart simply starts it again.

    Returns:
        The ``updated`` / ``skipped`` / ``errors`` counts, or ``with_task_lock``'s
        ``{"skipped": True, ...}`` when another pass holds the lock.
    """
    from app.services.opensearch_service import sync_speaker_profiles_to_opensearch

    db = SessionLocal()
    try:
        result = sync_speaker_profiles_to_opensearch(db)
    finally:
        db.close()
    if result["updated"] > 0:
        logger.info(f"Speaker profile sync: {result}")
    return result
