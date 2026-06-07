"""Celery tasks for scheduled database backups (Feature C).

Two tasks:
- ``backup.check_schedule`` (beat, every 5 min, utility queue): loads DB-backed settings,
  checks whether the cron schedule is due since ``backup.last_run_at``, and if so dispatches
  the real backup. This makes scheduling fully DB-driven — changing the cron in the admin UI
  takes effect with no beat restart.
- ``backup.run`` (utility queue): runs ``pg_dump`` from the worker, writes to the mounted
  destination, optionally encrypts, prunes old backups by GFS policy, records the result.
"""

from __future__ import annotations

import logging

from celery import shared_task

from app.core.constants import UtilityPriority
from app.db.session_utils import session_scope
from app.services import backup_service

logger = logging.getLogger(__name__)


@shared_task(name="backup.check_schedule", priority=UtilityPriority.ROUTINE)
def check_backup_schedule() -> dict:
    """Beat-driven due-check. Dispatch ``backup.run`` when the cron schedule is due.

    Stamps ``backup.last_run_at`` to the current dispatch time immediately so the next
    5-minute tick won't re-fire the same window (the run task records the final result).
    """
    from datetime import datetime
    from datetime import timezone

    with session_scope() as db:
        cfg = backup_service.get_settings(db)
        if not cfg["enabled"]:
            return {"status": "disabled"}
        if not backup_service.is_due(cfg["schedule"], cfg["last_run_at"]):
            return {"status": "not_due", "schedule": cfg["schedule"]}
        # Claim this window before dispatching so overlapping ticks don't double-fire.
        now_iso = datetime.now(timezone.utc).isoformat()
        backup_service.update_settings_last_run(db, now_iso)

    run_backup.apply_async(queue="utility", priority=UtilityPriority.ROUTINE)
    logger.info("Scheduled backup is due — dispatched backup.run")
    return {"status": "dispatched", "schedule": cfg["schedule"]}


@shared_task(name="backup.run", priority=UtilityPriority.ROUTINE)
def run_backup() -> dict:
    """Execute one database backup end-to-end and return the result dict."""
    logger.info("Starting database backup run")
    result = backup_service.perform_backup()
    return result
