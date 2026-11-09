"""Celery task that deletes expired refresh-token rows.

``refresh_token`` rows are the session record, and an expired row can never be exchanged
again, so it only costs table and index size. Mirrors ``session_cap.py``'s shape: one task,
a fixed daily beat entry (``expired-refresh-token-cleanup``), a Redis lock so an overlapping
beat tick cannot run two deletes at once. The rule lives in
``TokenService.cleanup_expired_tokens``; this module is only the scheduling shell.
"""

from __future__ import annotations

import logging

from app.auth.token_service import token_service
from app.core.celery import celery_app
from app.core.queues import UtilityPriority
from app.db.session_utils import session_scope
from app.utils.task_lock import task_lock_manager

logger = logging.getLogger(__name__)

SESSION_ROW_CLEANUP_LOCK = "expired_session_row_cleanup"
#: One indexed range DELETE; the timeout only has to outlive a slow database.
SESSION_ROW_CLEANUP_LOCK_TIMEOUT = 1800


@celery_app.task(name="auth.cleanup_expired_refresh_tokens", priority=UtilityPriority.BACKGROUND)
def cleanup_expired_refresh_tokens_task() -> dict:
    """Delete refresh tokens whose ``expires_at`` has passed and report how many."""
    with task_lock_manager.acquire_lock(
        SESSION_ROW_CLEANUP_LOCK, timeout=SESSION_ROW_CLEANUP_LOCK_TIMEOUT
    ) as acquired:
        if not acquired:
            logger.info("Expired refresh token cleanup already running, skipping this dispatch")
            return {"status": "skipped", "reason": "token cleanup already running"}
        with session_scope() as db:
            deleted = token_service.cleanup_expired_tokens(db)
        return {"status": "ok", "deleted": deleted}
