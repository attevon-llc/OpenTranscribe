"""Operator-triggered re-embed of files stranded text-only by a neural-search degraded
window (issue #626, the follow-up to #625's neural-search-availability retry/backoff).

A degraded window means the neural ingest pipeline was unavailable at index time, so
``index_transcript_chunks`` wrote chunk-plane documents with ``embedding_model = None`` —
text-only, no vector, invisible to kNN forever unless something reindexes them.
``embedding_provenance.survey_degraded_files`` finds them; this task is the thin dispatch
wrapper around the existing reindex coordinator, mirroring
``search_indexing_task.backfill_speaker_id_fields_task``'s shape: enumerate, group by
owner, dispatch one ``reindex_transcripts_task`` per owner, never write OpenSearch
directly.

Reports **aggregate dispatch counts only** — per-file completion already streams through
the existing reindex-progress WebSocket/UI once ``reindex_transcripts_task`` is dispatched,
so this task does not duplicate that tracking.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.utils.task_lock import with_task_lock
from app.utils.task_utils import TASK_STATUS_COMPLETED
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import TASK_STATUS_SKIPPED
from app.utils.task_utils import create_task_record
from app.utils.task_utils import update_task_status

logger = logging.getLogger(__name__)

#: Redis lock key guarding this task — shared by the endpoint, which checks lock state
#: before dispatching rather than dispatching into a lock it knows is already held.
REEMBED_LOCK_KEY = "search_reembed_degraded"


def _reembed_degraded_files(self, triggered_by: int, limit: int = 500) -> dict[str, Any]:
    """Enumerate text-only chunk-plane documents and dispatch a reindex per owner.

    Undecorated so tests can call it directly with a stand-in ``self`` — see
    ``reembed_degraded_files_task`` below, which is the actual Celery entry point
    (locked, task-registered).

    Args:
        triggered_by: The admin user id who requested this run (recorded on the Task row;
            this is a corpus-wide operation with no single owning file, hence the
            ``media_file_id=None`` widening in ``task_utils.create_task_record``).
        limit: Maximum number of distinct files to survey/dispatch in one call — a
            maintenance knob bounding queue impact, not a target to hit.

    Returns:
        Dict with ``status`` (``skipped`` / ``dispatched``) and, when files were found,
        ``dispatched_files`` / ``dispatched_users`` / ``dispatch_failures`` / ``truncated``.
    """
    with session_scope() as db:
        task = create_task_record(db, self.request.id, triggered_by, None, "search_reembed")
        update_task_status(db, task.id, TASK_STATUS_IN_PROGRESS, progress=0.1)

    # No DB session open across the OpenSearch survey — session-lifetime rule
    # (app/tasks/CLAUDE.md): a slow non-DB call must never run inside an open
    # transaction.
    from app.services.search.embedding_provenance import survey_degraded_files

    files, truncated = survey_degraded_files(limit=limit)

    if not files:
        with session_scope() as db:
            update_task_status(db, task.id, TASK_STATUS_SKIPPED, completed=True)
        return {"status": "skipped", "reason": "no_degraded_files"}

    by_user: dict[int, set[str]] = {}
    for f in files:
        by_user.setdefault(f.user_id, set()).add(f.file_uuid)

    from app.tasks.reindex_task import reindex_transcripts_task

    dispatched_files = 0
    dispatched_users = 0
    dispatch_failures: list[dict[str, Any]] = []

    for user_id, file_uuids in by_user.items():
        try:
            reindex_transcripts_task.apply_async(
                args=[user_id, sorted(file_uuids)],
                priority=CPUPriority.MAINTENANCE,
            )
            dispatched_files += len(file_uuids)
            dispatched_users += 1
        except Exception as e:
            # Per-owner isolation: one broker failure must not stop dispatch to
            # every other owner.
            logger.error(f"search_reembed: dispatch failed for user {user_id}: {e}")
            dispatch_failures.append({"user_id": user_id, "error": str(e)})

    with session_scope() as db:
        update_task_status(db, task.id, TASK_STATUS_COMPLETED, progress=1.0, completed=True)

    logger.info(
        f"search_reembed: dispatched reindex for {dispatched_files} file(s) across "
        f"{dispatched_users} user(s), {len(dispatch_failures)} dispatch failure(s), "
        f"truncated={truncated}"
    )
    return {
        "status": "dispatched",
        "dispatched_files": dispatched_files,
        "dispatched_users": dispatched_users,
        "dispatch_failures": dispatch_failures,
        "truncated": truncated,
    }


@celery_app.task(bind=True, name="search.reembed_degraded", priority=CPUPriority.MAINTENANCE)
@with_task_lock(REEMBED_LOCK_KEY, timeout=300)
def reembed_degraded_files_task(self, triggered_by: int, limit: int = 500) -> dict[str, Any]:
    """Celery entry point: ``_reembed_degraded_files`` under the redis dispatch lock."""
    return _reembed_degraded_files(self, triggered_by, limit=limit)
