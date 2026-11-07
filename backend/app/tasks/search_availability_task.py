"""``search.availability_reconcile``: make the index's trash and takedown state converge.

Issues #1101 (trash) and #1253 (quarantine).

The work is ``services/search/availability_stamp.reconcile``. Two ways in:

* **Dispatched with ``file_ids``** by a trash, restore, quarantine or release whose synchronous
  stamp failed or left documents unstamped. Retried with backoff while OpenSearch is unreachable; each run reads the
  rows' CURRENT state, so a restore that lands between retries is what gets stamped.
* **Beat**, every 15 minutes, and **once per boot** (``startup/maintenance``), with no
  arguments: every trashed and quarantined file, and every file the index still reads as
  unavailable whose row is available again. The boot run is what stamps a takedown that
  predates the field. This is the guarantee for a
  stamp that failed AND whose dispatch failed (broker down at the same moment), and for a
  document a concurrent writer landed after the stamp ran.

Replayable after worker loss (``core/task_replay``): every run recomputes its target from
Postgres and assigns the value whole, so a duplicate run reaches the same end state.
Meanwhile the Postgres post-filter (``file_availability.unavailable_among``) keeps a trashed or
quarantined file's hits out of every result list; only counts, facets and suggestions can lag.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.queues import UtilityPriority
from app.services.search.availability_stamp import AVAILABILITY_RECONCILE_TASK

logger = logging.getLogger(__name__)


#: Retries of a scoped run, at 30 s doubling up to the cap; past them the beat tick takes over.
MAX_SCOPED_RETRIES = 6
_MAX_COUNTDOWN_SECONDS = 900


def _countdown(retries: int) -> int:
    return int(min(30 * (2**retries), _MAX_COUNTDOWN_SECONDS))


@celery_app.task(
    bind=True,
    name=AVAILABILITY_RECONCILE_TASK,
    priority=UtilityPriority.ROUTINE,
    max_retries=MAX_SCOPED_RETRIES,
)
def availability_reconcile(self: Any, file_ids: list[int] | None = None) -> dict[str, Any]:
    """Stamp ``availability`` from Postgres: for ``file_ids``, or for every unavailable file."""
    from app.services.search.availability_stamp import reconcile

    try:
        outcome = reconcile(file_ids)
    except Exception as exc:
        if file_ids is None:
            raise
        logger.warning(
            "availability reconcile for %d file(s) failed; retrying", len(file_ids), exc_info=True
        )
        raise self.retry(exc=exc, countdown=_countdown(self.request.retries)) from exc
    if file_ids is not None and outcome.incomplete:
        raise self.retry(countdown=_countdown(self.request.retries))
    return outcome.as_dict()
