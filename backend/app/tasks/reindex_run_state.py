"""Per-run Redis coordination state, cancellation seam and progress events for reindex."""

import logging
from typing import Any

from app.core.constants import NOTIFICATION_TYPE_REINDEX_PROGRESS
from app.core.redis import get_redis
from app.services.search.reindex_cancel import cancel_requested
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def _is_cancellation_requested(user_id: int, run_id: str | None = None) -> bool:
    """Check if a reindex cancellation has been requested for THIS run.

    A thin seam over ``services/search/reindex_cancel``, which owns the flag's
    key shape and its run-naming contract (#691). Kept as a named function
    because it is the point the batch worker's tests substitute.

    Args:
        user_id: The user whose reindex to check.
        run_id: The dispatching coordinator's task id. A flag naming a different
            run is a finished run's residue and must not stop this one.

    Returns:
        True if cancellation was requested for this run.
    """
    return cancel_requested(user_id, run_id)


_REINDEX_STATE_KEY = "reindex_state:{user_id}"
_REINDEX_UUIDS_KEY = "reindex_uuids:{user_id}"
_REINDEX_LOCK_KEY = "reindex_lock:{user_id}"

#: TTL on a run's coordination keys. A crashed coordinator or a batch worker that
#: never reaches completion leaves them behind; run-scoping means each such run
#: leaks its OWN pair rather than overwriting one shared pair, so the expiry is
#: what bounds the leak.
_REINDEX_STATE_TTL_SECONDS = 86400


def _reindex_keys(user_id: int, run_id: str | None) -> tuple[str, str]:
    """Return the ``(state_key, uuids_key)`` pair belonging to ONE reindex run.

    The coordination state used to be keyed on ``user_id`` alone, so two
    overlapping coordinators for the same user addressed the same hash and the
    same uuid set. That is reachable whenever a run outlives
    ``reindex_lock:{user_id}``'s one-hour expiry: the lock lapses, the next
    ``search_index_maintenance`` tick sees no lock and dispatches a second
    coordinator, and that coordinator resets ``worker_count`` under the first
    run's still-running batch workers. Completion then fires early holding a
    fraction of the uuids the first run had indexed — and the orphan sweep reads
    that fraction as "everything else is orphaned".

    Appending the coordinator's task id makes the two runs address disjoint keys,
    so a late coordinator cannot write into an earlier run's state at all.

    Args:
        user_id: Owner of the reindex.
        run_id: The coordinator's task id. ``None``/empty selects the **legacy**
            user-only key shape, which exists for one reason: batch messages
            queued by a pre-upgrade coordinator arrive with no run id, and the
            state they must coordinate against is the user-only pair that
            coordinator wrote.

    Returns:
        ``(state_key, uuids_key)``.
    """
    state_key = _REINDEX_STATE_KEY.format(user_id=user_id)
    uuids_key = _REINDEX_UUIDS_KEY.format(user_id=user_id)
    if not run_id:
        return state_key, uuids_key
    return f"{state_key}:{run_id}", f"{uuids_key}:{run_id}"


def _release_reindex_lock(redis_client: Any, user_id: int, run_id: str | None) -> bool:
    """Release ``reindex_lock:{user_id}``, but only if ``run_id`` still holds it.

    The lock stays **user-scoped** on purpose — it is what makes runs mutually
    exclusive per user, and run-scoping it would permit the concurrency instead
    of preventing it. The consequence is that a run which outlived its own lock
    must not delete the lock a *later* run now owns; doing so releases that run
    early and lets a third coordinator in, which is the same failure one step
    further along.

    Args:
        redis_client: Redis client.
        user_id: Owner of the reindex.
        run_id: Coordinator task id to check ownership against. ``None`` (a
            legacy batch message) deletes unconditionally, as before.

    Returns:
        True if the lock was released or was already gone.
    """
    lock_key = _REINDEX_LOCK_KEY.format(user_id=user_id)
    if run_id:
        holder = redis_client.get(lock_key)
        if isinstance(holder, bytes):
            holder = holder.decode()
        if holder is not None and holder != run_id:
            logger.warning(
                f"Reindex run {run_id} (user {user_id}) finished but the lock is now held "
                f"by run {holder}; leaving it alone. Deleting it would release a reindex "
                f"that is still running."
            )
            return False
    redis_client.delete(lock_key)
    return True


def _clear_stale_progress(user_id: int) -> None:
    """Clear the stale reindex **progress tracker** key from Redis.

    Called at the start of a new reindex so a previous run's ETA/percentage does
    not contaminate this one. ``task_progress:reindex:{user_id}`` is still keyed
    on the user alone — the SPA reads it by user — so it is still the one key
    here that can be stale.

    It deliberately no longer touches ``reindex_state``/``reindex_uuids``. Those
    are now run-scoped (see :func:`_reindex_keys`), so a fresh run's pair cannot
    pre-exist and there is nothing stale to clear — while deleting the
    *user-scoped* pair is exactly the thing that wiped a live run's coordination
    state out from under it. A run's own pair is deleted by its completion
    handler, and the TTL bounds the crash case.
    """
    try:
        get_redis().delete(f"task_progress:reindex:{user_id}")
    except Exception as e:
        logger.warning(f"Could not clear stale reindex progress: {e}")


def _send_reindex_progress(
    redis_client: Any, user_id: int, state_key: str, tracker: Any | None = None
) -> None:
    """Read this run's progress from Redis and send a WebSocket notification.

    Uses ProgressTracker for EWMA-smoothed ETA when available.

    Args:
        redis_client: Redis client.
        user_id: Owner of the reindex, used for the websocket route.
        state_key: This run's state hash. Passed in rather than derived from
            ``user_id`` so a batch worker reports its OWN run's counters.
        tracker: Optional progress tracker for EWMA ETA.
    """
    state = redis_client.hgetall(state_key)
    if not state:
        return
    indexed = int(state.get(b"indexed", state.get("indexed", 0)))
    total = int(state.get(b"total", state.get("total", 0)))
    failed = int(state.get(b"failed", state.get("failed", 0)))

    # Use ProgressTracker for EWMA ETA if available
    eta_seconds = None
    message = f"Indexed {indexed} of {total} files"
    if tracker is not None:
        from app.services.progress_tracker import emit_progress_notification

        emit_progress_notification(
            tracker,
            processed=indexed,
            user_id=user_id,
            notification_type=NOTIFICATION_TYPE_REINDEX_PROGRESS,
            message=message,
            extra_data={
                "indexed_files": indexed,
                "total_files": total,
                "failed_files": failed,
            },
        )
        return

    # Fallback: send without ETA
    progress_frac = round(indexed / total, 4) if total > 0 else 0
    send_ws_event(
        user_id,
        NOTIFICATION_TYPE_REINDEX_PROGRESS,
        {
            "indexed_files": indexed,
            "total_files": total,
            "failed_files": failed,
            "progress": progress_frac,
            "eta_seconds": eta_seconds,
            "message": message,
        },
    )
