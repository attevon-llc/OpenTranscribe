"""Shared clean-up for the admin "stop migration" endpoints.

Revoking in-flight batch tasks and clearing the queue tracker are best effort: the
migration's own progress flag is already stopped by then, so a failure here must not turn
the stop into a 500. It must also not be invisible, so the revoke result carries a flag
the endpoint reports to the caller.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kombu.exceptions import KombuError
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Redis (the id list), the Celery broker (revoke), and a malformed stored id list.
_REVOKE_ERRORS = (RedisError, KombuError, OSError, ValueError)

BATCH_REVOCATION_FAILED = "batch_task_revocation_failed"


def revoke_batch_tasks(redis_client: Any, ids_key: str) -> tuple[int, bool]:
    """Revoke the batch task ids stored under ``ids_key`` and delete the key.

    Returns:
        ``(revoked_count, failed)``. ``failed`` is True when Redis or the broker could not
        be reached, so tasks may still be running; "nothing to revoke" is ``(0, False)``.
    """
    revoked = 0
    try:
        raw = redis_client.get(ids_key)
        if raw:
            from app.core.celery import celery_app

            for task_id in json.loads(raw):
                celery_app.control.revoke(task_id, terminate=True)
                revoked += 1
            redis_client.delete(ids_key)
    except _REVOKE_ERRORS:
        logger.warning("Failed to revoke batch tasks stored under %s", ids_key, exc_info=True)
        return revoked, True
    return revoked, False


def clear_stop_tracker(task_type: str, user_id: int) -> None:
    """Mark the queue-status tracker of a stopped migration complete (best effort)."""
    try:
        from app.services.progress_tracker import ProgressTracker

        ProgressTracker(task_type=task_type, user_id=user_id, total=0).complete(
            message="Stopped by user"
        )
    except (RedisError, OSError):
        logger.warning("Failed to clear progress tracker for %s", task_type, exc_info=True)
