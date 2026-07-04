"""Backup collectors, refreshed at Prometheus scrape time from the database.

The backup executes inside a Celery worker whose registry is never scraped (the
worker-process trap documented in ``app.core.metrics``), so ``backup.run`` persists
its outcome to SystemSettings (``backup.last_result``, ``backup.last_success_at``,
cumulative run counts) and this module projects that DB state onto the API process's
collectors at scrape time — the same sample-at-scrape pattern as
``celery_metrics.update_queue_depths``.

``backup_runs_total`` stays a true Counter: each scrape increments it by the delta
between the DB-persisted cumulative count and the current sample, which also makes it
survive API restarts (values re-sync from the DB on the next scrape; a shrinking DB
value is ignored, preserving monotonicity).

The whole refresh degrades gracefully: any DB error leaves the collectors untouched.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime

from prometheus_client import REGISTRY
from sqlalchemy.orm import Session

from app.core.metrics import backup_last_status
from app.core.metrics import backup_last_success_timestamp_seconds
from app.core.metrics import backup_runs_total

logger = logging.getLogger(__name__)


def _sync_run_counters(vals: dict[str, str | None]) -> None:
    """Increment ``backup_runs_total`` up to the DB-persisted cumulative counts."""
    from app.services import backup_service

    for result, key in (
        ("success", backup_service.KEY_RUNS_SUCCESS),
        ("failure", backup_service.KEY_RUNS_FAILURE),
    ):
        try:
            target = int(vals.get(key) or 0)
        except (TypeError, ValueError):
            continue
        current = REGISTRY.get_sample_value("backup_runs_total", {"result": result}) or 0.0
        delta = target - current
        if delta > 0:
            backup_runs_total.labels(result=result).inc(delta)


def update_backup_metrics(db: Session | None = None) -> None:
    """Refresh backup gauges/counters from persisted run state (best-effort).

    ``db`` lets tests pass their savepoint session; production scrapes open a
    short-lived session.
    """
    try:
        from app.services import backup_service
        from app.services import system_settings_service as sss

        keys = [
            backup_service.KEY_LAST_RESULT,
            backup_service.KEY_LAST_SUCCESS_AT,
            backup_service.KEY_RUNS_SUCCESS,
            backup_service.KEY_RUNS_FAILURE,
        ]
        if db is not None:
            vals = sss.get_settings_map(db, keys)
        else:
            from app.db.base import SessionLocal

            own = SessionLocal()
            try:
                vals = sss.get_settings_map(own, keys)
            finally:
                own.close()

        last_success = vals.get(backup_service.KEY_LAST_SUCCESS_AT)
        if last_success:
            with contextlib.suppress(ValueError, TypeError):
                backup_last_success_timestamp_seconds.set(
                    datetime.fromisoformat(last_success).timestamp()
                )
        raw_result = vals.get(backup_service.KEY_LAST_RESULT)
        if raw_result:
            with contextlib.suppress(ValueError, TypeError):
                backup_last_status.set(1 if json.loads(raw_result).get("ok") else 0)
        _sync_run_counters(vals)
    except Exception as exc:  # noqa: BLE001 — scrape must never fail on DB issues
        logger.debug("Backup metric sampling skipped: %s", exc)
