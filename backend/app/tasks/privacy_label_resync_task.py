"""Relabel the search index after a voiceprint switch changed (issue #1280).

Dispatched by ``services/privacy/label_reindex.request_label_resync`` once the switch change has
committed, and by ``search_index_maintenance`` as a safety net while the pending record is not
empty. The work is ``label_reindex.run_label_resync``: it reads the pending record, queues the
debounced per-file re-index for every file a voiceprint can name in each pending scope, audits
what it measured, and removes a scope only when every file was queued. Running it twice, or
concurrently with the maintenance arm, is safe: a file already queued counts as done. There is
deliberately no task lock: a request that arrives while a pass runs must not be skipped.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.services.privacy.label_reindex import run_label_resync

logger = logging.getLogger(__name__)

TASK_NAME = "privacy.label_resync"


@celery_app.task(name=TASK_NAME, priority=CPUPriority.MAINTENANCE)
def privacy_label_resync_task() -> dict[str, Any]:
    """Finish every pending voiceprint-switch relabelling. Returns the measured counts."""
    with session_scope() as db:
        outcomes = run_label_resync(db)
    return {
        "scopes": len(outcomes),
        "finished": sum(1 for o in outcomes if o.finished),
        "files_selected": sum(o.files_selected for o in outcomes),
        "files_queued": sum(o.files_queued for o in outcomes),
        "files_already_pending": sum(o.files_already_pending for o in outcomes),
        "files_failed": sum(o.files_failed for o in outcomes),
    }
