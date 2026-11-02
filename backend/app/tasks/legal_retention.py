"""Legal acceptance record retention (migration v451).

A deleted user's acceptances are kept, pseudonymised, as evidence of which terms applied, until
``retain_until`` (``subject_erased_at`` + ``legal.acceptance_retention_days``, set by the database
trigger at the moment of deletion). This task deletes the rows whose time has come.

The database refuses a DELETE on ``legal_acceptance`` unless the transaction has set
``opentranscribe.legal_retention_sweep = 'on'`` AND the row's ``retain_until`` has passed, so this
is the only code path that can remove one. Live rows (``retain_until IS NULL``) are never
selected. Modelled on ``chat_retention``: bounded per tick, UTILITY queue, daily beat.
"""

import logging

from sqlalchemy import text

from app.core.celery import celery_app
from app.core.constants import UtilityPriority

logger = logging.getLogger(__name__)

TASK_NAME = "legal.acceptance_retention_sweep"

#: Bound the work per tick; the next tick picks up the rest.
MAX_DELETIONS_PER_RUN = 500

#: Ordered oldest-due first so a backlog drains in deadline order.
_DELETE_DUE_SQL = text(
    """
    DELETE FROM legal_acceptance WHERE id IN (
        SELECT id FROM legal_acceptance
         WHERE retain_until IS NOT NULL AND retain_until <= now()
         ORDER BY retain_until, id
         LIMIT :limit
    )
    """
)


def delete_due_acceptances(db, limit: int = MAX_DELETIONS_PER_RUN) -> int:
    """Delete up to ``limit`` records past ``retain_until`` in the caller's transaction.

    ``SET LOCAL`` scopes the sweep flag to this transaction, so it cannot leak to other work on
    a pooled connection. Returns the number deleted; the caller commits.
    """
    db.execute(text("SET LOCAL opentranscribe.legal_retention_sweep = 'on'"))
    result = db.execute(_DELETE_DUE_SQL, {"limit": int(limit)})
    return int(result.rowcount or 0)


@celery_app.task(bind=True, name=TASK_NAME, priority=UtilityPriority.BACKGROUND)
def legal_acceptance_retention_sweep(self) -> dict:  # noqa: ARG001 — bind=True signature
    """Delete pseudonymised acceptance records whose retention period has ended.

    Returns:
        ``{"status": "ok", "deleted": n, "truncated": bool}``.
    """
    from app.auth.audit import AuditEventType
    from app.auth.audit import AuditOutcome
    from app.auth.audit import audit_logger
    from app.db.session_utils import session_scope

    with session_scope() as db:
        deleted = delete_due_acceptances(db)

    if deleted:
        logger.info("Legal retention: deleted %d acceptance record(s) past retain_until", deleted)
        audit_logger.log(
            event_type=AuditEventType.LEGAL_RECORDS_EXPIRED,
            outcome=AuditOutcome.SUCCESS,
            details={"records_deleted": deleted},  # not "deleted": a bool elsewhere
        )
    return {"status": "ok", "deleted": deleted, "truncated": deleted >= MAX_DELETIONS_PER_RUN}
