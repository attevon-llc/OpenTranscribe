"""Celery tasks for scheduled database backups (Feature C) + the media mirror (#242).

Four tasks:
- ``backup.check_schedule`` (beat, every 5 min, utility queue): loads DB-backed settings,
  checks whether the cron schedule is due since ``backup.last_run_at``, and if so dispatches
  the real backup. This makes scheduling fully DB-driven — changing the cron in the admin UI
  takes effect with no beat restart.
- ``backup.run`` (utility queue): runs ``pg_dump`` from the worker, writes to the mounted
  destination, optionally encrypts, prunes old backups by GFS policy, records the result.
- ``backup.mirror_check_schedule`` (beat, every 5 min, utility queue): same due-check
  pattern against the ``backup.mirror_*`` settings; dispatches the mirror run.
- ``backup.mirror_run`` (**download queue** — bulk object I/O belongs with the download
  worker, never the GPU queue): incremental media-bucket mirror under a Redis lock so
  runs never overlap (a first full mirror can outlast the next scheduled window).
"""

from __future__ import annotations

import logging
from datetime import UTC

from celery import shared_task

from app.core.constants import CeleryQueues
from app.core.constants import DownloadPriority
from app.core.constants import UtilityPriority
from app.db.session_utils import session_scope
from app.services import backup_service
from app.services import media_mirror_service
from app.utils.task_lock import task_lock_manager

logger = logging.getLogger(__name__)


@shared_task(name="backup.check_schedule", priority=UtilityPriority.ROUTINE)
def check_backup_schedule() -> dict:
    """Beat-driven due-check. Dispatch ``backup.run`` when the cron schedule is due.

    Stamps ``backup.last_run_at`` to the current dispatch time immediately so the next
    5-minute tick won't re-fire the same window (the run task records the final result).
    """
    from datetime import datetime

    with session_scope() as db:
        cfg = backup_service.get_settings(db)
        if not cfg["enabled"]:
            return {"status": "disabled"}
        if not backup_service.is_due(cfg["schedule"], cfg["last_run_at"]):
            return {"status": "not_due", "schedule": cfg["schedule"]}
        # Claim this window before dispatching so overlapping ticks don't double-fire.
        now_iso = datetime.now(UTC).isoformat()
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


@shared_task(name="backup.mirror_check_schedule", priority=UtilityPriority.ROUTINE)
def check_mirror_schedule() -> dict:
    """Beat-driven due-check. Dispatch ``backup.mirror_run`` when the cron is due.

    Same window-claiming pattern as ``backup.check_schedule``: stamps
    ``backup.mirror_last_run_at`` before dispatch so the next 5-minute tick won't
    re-fire the same window (the run task records the final result).
    """
    from datetime import datetime

    with session_scope() as db:
        cfg = media_mirror_service.get_settings(db)
        if not cfg["enabled"]:
            return {"status": "disabled"}
        if not backup_service.is_due(cfg["schedule"], cfg["last_run_at"]):
            return {"status": "not_due", "schedule": cfg["schedule"]}
        now_iso = datetime.now(UTC).isoformat()
        media_mirror_service.update_settings_last_run(db, now_iso)

    run_media_mirror.apply_async(queue=CeleryQueues.DOWNLOAD, priority=DownloadPriority.PLAYLIST)
    logger.info("Scheduled media mirror is due — dispatched backup.mirror_run")
    return {"status": "dispatched", "schedule": cfg["schedule"]}


@shared_task(name="backup.mirror_run", priority=DownloadPriority.PLAYLIST)
def run_media_mirror(max_objects: int | None = None) -> dict:
    """Execute one incremental media mirror run under the overlap-preventing lock.

    ``max_objects`` bounds how many source objects the run examines (None = all);
    manual Run Now and tests use it — the beat always dispatches unbounded.
    """
    with task_lock_manager.acquire_lock(
        media_mirror_service.MIRROR_LOCK_KEY, timeout=media_mirror_service.MIRROR_LOCK_TIMEOUT
    ) as acquired:
        if not acquired:
            logger.info("Media mirror already running — skipping this dispatch")
            return {"status": "skipped", "reason": "mirror already running"}
        logger.info("Starting media mirror run (max_objects=%s)", max_objects)
        from app.services import media_mirror_engine

        return media_mirror_engine.perform_mirror(max_objects=max_objects)
