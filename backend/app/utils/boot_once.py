"""Run a startup step exactly once across all replicas (issue #284 A1.3 / A1.15).

Several startup steps were written for a single-instance deployment and are actively
harmful when more than one API replica boots:

* ``_clear_stale_task_state()`` deletes **all** ``task_progress:*`` keys and every
  coordination lock. With N replicas, the second one to start wipes progress and locks
  belonging to work the first is actively coordinating.
* the neural-search model deploy and ``check_and_repair_indices`` each run on every
  replica, doing the same expensive work N times against one OpenSearch cluster.

This is an election, not a lock: the winner does the work and the marker **stays** for
the rest of the boot window so replicas starting slightly later skip it. A lock would be
released on exit and let the next replica redo the work.

The TTL is what makes a genuine full-stack restart still work — the marker from the
previous boot has long expired, so the first process up runs the step again. It only
suppresses the other replicas of the *same* rollout.

Fails OPEN: if Redis is unreachable we run the step. A single-instance deployment with no
Redis must still clean up after itself, and duplicated work is a smaller failure than
skipped work.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: How long one boot "generation" lasts. Long enough to cover a rolling restart of every
#: replica, short enough that the next real restart is not suppressed.
DEFAULT_BOOT_WINDOW_SECONDS = 300


def run_once_per_boot(step: str, ttl: int = DEFAULT_BOOT_WINDOW_SECONDS) -> bool:
    """Claim *step* for this boot window.

    Args:
        step: Stable identifier for the startup step.
        ttl: Seconds the claim persists — the width of one boot generation.

    Returns:
        True if this process should perform the step, False if another replica
        already claimed it during this window.
    """
    key = f"boot_once:{step}"
    try:
        from app.core.redis import get_redis

        claimed = get_redis().set(key, "1", nx=True, ex=ttl)
    except Exception as exc:  # noqa: BLE001 - Redis availability is a deployment concern
        logger.warning(
            "Boot election unavailable for %s (%s); running the step. Duplicated work is "
            "safer here than skipped work.",
            step,
            exc,
        )
        return True

    if claimed:
        logger.info("Claimed startup step %r for this boot window", step)
        return True

    logger.info("Startup step %r already claimed by another replica; skipping", step)
    return False
