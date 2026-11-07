"""The beat half of the opt-in anonymous usage report (issue #1019).

Runs hourly; :func:`app.services.telemetry.send_if_due` decides whether anything goes out
(off unless an administrator turned it on, then about once a day). A failed send never
raises and is never retried: it is logged at DEBUG and waits for the next day, so the report
can never hold a worker slot or delay other work.
"""

import logging

from sqlalchemy.exc import SQLAlchemyError

from app.core.celery import celery_app
from app.core.queues import UtilityPriority

logger = logging.getLogger(__name__)

TASK_NAME = "telemetry.usage_report"


@celery_app.task(
    name=TASK_NAME,
    priority=UtilityPriority.BACKGROUND,
    ignore_result=True,
    max_retries=0,
    soft_time_limit=30,
    time_limit=45,
)
def usage_report_task() -> dict[str, str]:
    """Send the usage report if it is on, allowed and due. Never raises."""
    from app.db.session_utils import session_scope
    from app.services import telemetry

    try:
        with session_scope() as db:
            return {"status": telemetry.send_if_due(db)}
    except SQLAlchemyError as exc:
        # The network half already swallows its own failures (services/telemetry._post);
        # a database hiccup must not surface as an ERROR for an optional report either.
        logger.debug("Usage report skipped: %s", type(exc).__name__)
        return {"status": "error"}
