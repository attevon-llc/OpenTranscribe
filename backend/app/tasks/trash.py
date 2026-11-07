"""Trash purge tasks (issue #1101); the work is ``services/trash_purge``.

* ``files.purge_trash`` - beat, hourly at :45 (offset from ``cleanup_expired_files`` at :00 and
  the 04:10-05:25 daily slots). Destroys trashed files whose ``purge_after`` has passed, at most
  ``TRASH_PURGE_BATCH_SIZE`` per tick. This tick is the guarantee.
* ``files.purge_trash_now`` - dispatched by "delete forever" and "empty trash" after they made
  the files due; it only shortens the wait for the next tick.

Both are idempotent: a file purged by one run is simply absent for the next, and a restored or
newly held file no longer matches the purge predicate. Not replayed on worker loss
(``core/task_replay``): the next beat tick re-selects whatever is still due.
"""

from typing import Any

from app.core.celery import celery_app
from app.core.queues import UtilityPriority

PURGE_TRASH_TASK = "files.purge_trash"
PURGE_TRASH_NOW_TASK = "files.purge_trash_now"


@celery_app.task(bind=True, name=PURGE_TRASH_TASK, priority=UtilityPriority.BACKGROUND)
def purge_trash(self: Any) -> dict[str, Any]:  # noqa: ARG001 - bind=True signature
    """Purge every due trashed file (bounded per tick)."""
    from app.services.trash_purge import purge_due

    return purge_due().as_dict()


@celery_app.task(bind=True, name=PURGE_TRASH_NOW_TASK, priority=UtilityPriority.ROUTINE)
def purge_trash_now(self: Any, file_ids: list[int]) -> dict[str, Any]:  # noqa: ARG001 - bind=True
    """Purge ``file_ids`` now, if they are still due (a restore or a hold may have intervened)."""
    from app.services.trash_purge import purge_due

    return purge_due(file_ids=file_ids, limit=max(1, len(file_ids))).as_dict()
