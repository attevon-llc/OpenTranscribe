"""Retention for the prunable Postgres audit tables (migration v453).

``AUDIT_LOG_RETENTION_DAYS`` is ``0`` (keep forever) by default and never below
``AUDIT_RETENTION_FLOOR_DAYS``. The sweep calls the ``audit_prune`` SQL function, which is the
only way the application role can delete an audit row: it refuses any table outside its
hardcoded list (``auth_config_audit``, ``support_access_use``) and clamps the age to the floor
itself, so a wrong value here cannot delete a younger row. The erasure ledger, the takedown
tables and the support-access grants are evidence and are never pruned; ``legal_acceptance``
keeps its own sweep (``tasks/legal_retention.py``).
"""

import logging

from sqlalchemy import text

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import AUDIT_RETENTION_FLOOR_DAYS
from app.core.constants import UtilityPriority
from app.db.audit_protection import prunable_tables

logger = logging.getLogger(__name__)

TASK_NAME = "audit.retention_sweep"


def prune_audit_tables(db, keep_days: int) -> dict[str, int]:
    """Prune every prunable table older than ``keep_days`` in the caller's transaction.

    Returns the number of rows removed per table. ``keep_days`` below the floor is clamped by
    the SQL function, not trusted here.
    """
    removed: dict[str, int] = {}
    for table in prunable_tables():
        removed[table.name] = int(
            db.execute(
                text("SELECT audit_prune(CAST(:t AS regclass), :d)"),
                {"t": f"public.{table.name}", "d": int(keep_days)},
            ).scalar()
            or 0
        )
    return removed


@celery_app.task(bind=True, name=TASK_NAME, priority=UtilityPriority.BACKGROUND)
def audit_retention_sweep(self) -> dict:  # noqa: ARG001 — bind=True signature
    """Delete audit rows older than ``AUDIT_LOG_RETENTION_DAYS`` (no-op when it is 0)."""
    from app.auth.audit import AuditEventType
    from app.auth.audit import AuditOutcome
    from app.auth.audit import audit_logger
    from app.db.session_utils import session_scope

    days = settings.AUDIT_LOG_RETENTION_DAYS
    if days <= 0:
        return {"status": "disabled", "removed": {}}
    days = max(days, AUDIT_RETENTION_FLOOR_DAYS)
    with session_scope() as db:
        removed = prune_audit_tables(db, days)
    total = sum(removed.values())
    if total:
        logger.info("Audit retention: removed %d row(s) older than %d days", total, days)
        audit_logger.log(
            event_type=AuditEventType.AUDIT_RECORDS_PRUNED,
            outcome=AuditOutcome.SUCCESS,
            details={"retention_days": days, "rows_by_table": removed},
        )
    return {"status": "ok", "removed": removed}
