"""Relay the governance audit outbox to the audit sink (v459, issue #1329).

Beat runs it every minute (``audit-outbox-relay``). Each run delivers due pending rows in passes
of ``AUDIT_OUTBOX_BATCH_SIZE`` until none is left or the sink fails (the failing row backs off;
see ``services/audit_outbox.relay``), deletes ``sent`` rows past their retention, and reports the
stuck count: at ERROR when it is above zero, so an operator watching logs sees what the
``audit_outbox_stuck_events`` gauge shows.
"""

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.constants import AUDIT_OUTBOX_BATCH_SIZE
from app.core.queues import UtilityPriority

logger = logging.getLogger(__name__)

TASK_NAME = "audit.outbox_relay"


@celery_app.task(bind=True, name=TASK_NAME, priority=UtilityPriority.OPERATIONAL)
def audit_outbox_relay(self: Any) -> dict:
    """Deliver pending governance audit events, prune sent ones, report stuck ones."""
    from app.db.session_utils import session_scope
    from app.services import audit_outbox

    sent = already = retrying = rejected = 0
    for _ in range(audit_outbox.MAX_PASSES_PER_RUN):
        result = audit_outbox.relay()
        sent += result.sent
        already += result.already_in_sink
        retrying += result.retrying
        rejected += result.rejected
        if result.claimed < AUDIT_OUTBOX_BATCH_SIZE or result.retrying:
            break

    with session_scope() as db:
        pruned = audit_outbox.prune_sent(db)
    stuck = audit_outbox.update_stuck_gauge()
    if stuck:
        logger.error(
            "audit outbox: %d governance audit event(s) committed but not in the audit sink "
            "(pending past the stuck threshold, or rejected); see audit_outbox.last_error",
            stuck,
        )
    if sent or retrying or rejected:
        logger.info(
            "audit outbox relay: sent=%d (already in sink=%d) retrying=%d rejected=%d pruned=%d",
            sent,
            already,
            retrying,
            rejected,
            pruned,
        )
    return {
        "sent": sent,
        "already_in_sink": already,
        "retrying": retrying,
        "rejected": rejected,
        "pruned": pruned,
        "stuck": stuck,
    }
