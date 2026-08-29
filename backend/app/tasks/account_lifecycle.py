"""Celery task for FedRAMP AC-2 account-inactivity expiration.

One task, on a fixed daily beat entry (``account-inactivity-sweep``,
``app/core/celery.py``) — unlike ``directory_sync_task``'s admin-configurable
schedule, this sweep has no interval to make configurable; it is either on
(``ACCOUNT_EXPIRATION_ENABLED``) or off, and ``account_lifecycle_service.run_inactivity_sweep``
itself no-ops immediately when it's off. A locked single task is simpler than a
due-check/dispatch pair and there is nothing here that needs the extra split.

Cheap and DB-only; must never land on the GPU queue. Runs under a Redis lock so an
overlapping beat tick (a slow pass still running when the next one is due) cannot
produce two concurrent passes racing on the same deactivation set. There is no
admin "Run now" trigger for this task today — the lock is still worth having for
that reason alone.
"""

from __future__ import annotations

import logging

from app.core.celery import celery_app
from app.core.constants import UtilityPriority
from app.db.session_utils import session_scope
from app.services import account_lifecycle_service
from app.utils.task_lock import task_lock_manager

logger = logging.getLogger(__name__)

#: Overlap guard. One indexed query plus at most a handful of deactivations —
#: minutes at worst; the generous timeout only has to outlive a slow DB.
ACCOUNT_INACTIVITY_LOCK_KEY = "account_inactivity_sweep"
ACCOUNT_INACTIVITY_LOCK_TIMEOUT = 1800


@celery_app.task(name="account.inactivity_sweep", priority=UtilityPriority.ROUTINE)
def run_account_inactivity_sweep() -> dict:
    """Execute one AC-2 inactivity-expiration pass and return its report."""
    with task_lock_manager.acquire_lock(
        ACCOUNT_INACTIVITY_LOCK_KEY, timeout=ACCOUNT_INACTIVITY_LOCK_TIMEOUT
    ) as acquired:
        if not acquired:
            logger.info("Account inactivity sweep already running — skipping this dispatch")
            return {"status": "skipped", "reason": "account inactivity sweep already running"}
        with session_scope() as db:
            return account_lifecycle_service.run_inactivity_sweep(db)
