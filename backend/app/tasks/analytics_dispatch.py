"""Debounced dispatch of ``analytics.analyze_transcript`` from request handlers.

Request paths must not compute or write analytics (it is CPU plus several statements); they
queue the CPU-queue task instead. A Redis ``SET NX EX`` key per file coalesces bursts, the same
mechanism the search sweep uses (``SWEEP_COOLDOWN_KEY_TEMPLATE``). The task is queued with a
``countdown`` equal to the debounce window, so it runs after the burst and reads the final
state; a request that loses the SET NX is covered by the task already queued.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ANALYTICS_DISPATCH_KEY_TEMPLATE = "analytics_dispatch:{file_uuid}"
ANALYTICS_DEBOUNCE_SECONDS = 5


def dispatch_analytics(file_uuid: str) -> bool:
    """Queue analytics for a file unless one is already queued within the window.

    Returns True when a task was dispatched. Redis trouble fails open to dispatching, since a
    duplicate analytics run is harmless and a dropped one leaves stale numbers.
    """
    from app.core.redis import get_request_redis
    from app.tasks.analytics import analyze_transcript_task

    key = ANALYTICS_DISPATCH_KEY_TEMPLATE.format(file_uuid=file_uuid)
    try:
        # Key outlives the countdown so the queued task, not a second one, owns the window.
        if not get_request_redis().set(key, "1", nx=True, ex=ANALYTICS_DEBOUNCE_SECONDS * 2):
            return False
    except Exception:
        logger.warning("analytics debounce unavailable; dispatching anyway", exc_info=True)
    analyze_transcript_task.apply_async(
        kwargs={"file_uuid": file_uuid}, countdown=ANALYTICS_DEBOUNCE_SECONDS
    )
    return True
