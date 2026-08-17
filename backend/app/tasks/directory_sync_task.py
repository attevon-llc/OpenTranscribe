"""Celery tasks for periodic directory reconciliation (LDAP groups, roles, deprovisioning).

Two tasks, following the same DB-driven scheduling pattern as ``backup_tasks``:

- ``directory.sync_check_schedule`` (beat, every 15 min, **cpu** queue): loads the
  DB-backed settings, checks whether the stored cron is due since
  ``directory_sync.last_run_at``, and dispatches the real sweep if so. Changing the
  schedule in the admin UI therefore takes effect with no beat restart.
- ``directory.sync_run`` (cpu queue): one reconciliation pass — probe every active
  LDAP account against the directory, disable the ones that are provably gone and
  revoke their sessions, and re-apply the configured ``group_mapping`` rows
  (groups + privilege) to the ones still present. Both halves come from the same
  probe, so adding group reconciliation cost no extra LDAP round-trips.

Both are cheap and network-bound; they must never land on the GPU queue. The sweep
runs under a Redis lock so a manual "Run now" landing on a scheduled window cannot
produce two concurrent passes racing on the same per-run cap.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime

from celery import shared_task

from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.services import directory_sync_service
from app.utils.task_lock import task_lock_manager

logger = logging.getLogger(__name__)

#: Overlap guard. A pass is one LDAP search per active account, so minutes at worst;
#: the generous timeout only has to outlive a slow directory.
DIRECTORY_SYNC_LOCK_KEY = "directory_sync_run"
DIRECTORY_SYNC_LOCK_TIMEOUT = 1800


@shared_task(name="directory.sync_check_schedule", priority=CPUPriority.MAINTENANCE)
def check_directory_sync_schedule() -> dict:
    """Beat-driven due-check. Dispatch ``directory.sync_run`` when the cron is due.

    Claims the window by stamping ``directory_sync.last_run_at`` before dispatch, so
    an overlapping tick can't double-fire the same window.
    """
    from app.services import backup_service

    with session_scope() as db:
        cfg = directory_sync_service.get_settings(db)
        if not cfg["enabled"]:
            return {"status": "disabled"}
        if not backup_service.is_due(cfg["schedule"], cfg["last_run_at"]):
            return {"status": "not_due", "schedule": cfg["schedule"]}
        prior_last_run = cfg["last_run_at"]
        directory_sync_service.update_settings_last_run(db, datetime.now(UTC).isoformat())

    try:
        run_directory_sync.apply_async(queue=CeleryQueues.CPU, priority=CPUPriority.MAINTENANCE)
    except Exception as e:
        logger.error("Failed to dispatch directory.sync_run, reverting claimed window: %s", e)
        with session_scope() as db:
            directory_sync_service.update_settings_last_run(db, prior_last_run)
        raise
    logger.info("Directory reconciliation is due — dispatched directory.sync_run")
    return {"status": "dispatched", "schedule": cfg["schedule"]}


@shared_task(name="directory.sync_run", priority=CPUPriority.MAINTENANCE)
def run_directory_sync(dry_run: bool | None = None) -> dict:
    """Execute one reconciliation pass and return its report.

    ``dry_run`` overrides the stored setting for an admin "Preview" action; the beat
    always passes ``None`` so the configured (safe-by-default) value wins.
    """
    with task_lock_manager.acquire_lock(
        DIRECTORY_SYNC_LOCK_KEY, timeout=DIRECTORY_SYNC_LOCK_TIMEOUT
    ) as acquired:
        if not acquired:
            logger.info("Directory reconciliation already running — skipping this dispatch")
            return {"status": "skipped", "reason": "directory sync already running"}
        logger.info("Starting directory reconciliation pass (dry_run=%s)", dry_run)
        return directory_sync_service.run_scheduled_sweep(dry_run=dry_run)
