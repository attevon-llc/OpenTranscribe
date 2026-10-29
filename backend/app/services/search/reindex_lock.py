"""Read the per-owner reindex lock, telling "no run" apart from "cannot tell"."""

import logging

logger = logging.getLogger(__name__)


class ReindexLockUnavailableError(Exception):
    """The reindex lock could not be read, so "running or not" is unknown."""


def running_reindex_run_id(user_id: int) -> str | None:
    """The coordinator task id currently holding ``reindex_lock:{user_id}``.

    The lock's *value* is the run id (``reindex_task`` sets it NX with a 1-hour
    TTL and releases it only if it still owns it), so one sub-millisecond GET
    answers both "is a reindex running for this owner" and "which run is it" — no
    Celery broadcast needed.

    Args:
        user_id: The owner to check.

    Returns:
        The run id, or ``None`` when no reindex is running for that owner.

    Raises:
        ReindexLockUnavailableError: Redis could not be read. Distinct from ``None``:
            an outage must not read as "no reindex running".
    """
    try:
        from app.core.redis import get_redis

        value = get_redis().get(f"reindex_lock:{user_id}")
    except Exception as e:
        logger.warning(f"Could not check reindex lock: {type(e).__name__}")
        raise ReindexLockUnavailableError(str(user_id)) from e
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)
