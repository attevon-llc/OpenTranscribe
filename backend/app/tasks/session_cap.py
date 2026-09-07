"""Celery task for FedRAMP AC-10 concurrent-session-ceiling enforcement (issue #632).

Mirrors ``account_lifecycle.py``'s shape exactly: one task, a fixed daily beat
entry, a Redis lock so an overlapping beat tick cannot run two passes concurrently.
Policy and mechanism live in ``services/session_cap_service.py``; this module is
only the scheduling shell.
"""

from __future__ import annotations

import logging

from app.core.celery import celery_app
from app.core.constants import UtilityPriority
from app.db.session_utils import session_scope
from app.services import session_cap_service
from app.utils.task_lock import task_lock_manager

logger = logging.getLogger(__name__)

#: Overlap guard. One grouped query plus at most a handful of per-user UPDATEs —
#: minutes at worst; the generous timeout only has to outlive a slow DB.
SESSION_CAP_SWEEP_LOCK_KEY = "session_cap_sweep"
SESSION_CAP_SWEEP_LOCK_TIMEOUT = 1800


@celery_app.task(name="session.cap_sweep", priority=UtilityPriority.BACKGROUND)
def run_session_cap_sweep_task() -> dict:
    """Execute one AC-10 concurrent-session-ceiling pass and return its report."""
    with task_lock_manager.acquire_lock(
        SESSION_CAP_SWEEP_LOCK_KEY, timeout=SESSION_CAP_SWEEP_LOCK_TIMEOUT
    ) as acquired:
        if not acquired:
            logger.info("Session cap sweep already running — skipping this dispatch")
            return {"status": "skipped", "reason": "session cap sweep already running"}
        with session_scope() as db:
            return session_cap_service.run_session_cap_sweep(db)
