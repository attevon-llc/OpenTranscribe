"""Celery task: content-redaction detection (dedicated CPU service).

**Dispatch is gated, not unconditional.** Redaction is opt-out by default
(``DEFAULT_REDACTION_ENABLED = False``) because the scan delays transcript display,
so the pipeline only dispatches this task when the owner has redaction enabled or an
admin forces it — see ``tasks/transcription/postprocess.py::_dispatch_redaction``,
which returns early unless ``resolve_effective_config(db, user_id).enabled``. A user
who enables redaction later gets detection dispatched **lazily, the first time they
open the file**, so an existing transcript with no spans is expected rather than a bug.

Once it does run, it runs **once per transcript**: detection spans are cached on the
segments, and enabling/disabling, categories, and style are all applied at read time,
so this never reruns when settings change. Only segment text edits or an admin model
upgrade re-trigger detection.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.celery import celery_app
from app.core.constants import NOTIFICATION_TYPE_REDACTION_STATUS
from app.core.constants import REDACTION_MODEL_VERSION
from app.core.constants import RedactionPriority
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)


def _mark_redaction_task(
    task_id: str,
    status: str,
    *,
    progress: float | None = None,
    error_message: str | None = None,
) -> None:
    """Record a status/progress transition, in its own short session.

    Best-effort and must never mask the caller's real outcome — a bookkeeping
    failure here logs and is swallowed, matching every other best-effort side
    path in this module (the WebSocket notification below).
    """
    from app.db.session_utils import session_scope
    from app.utils.task_utils import update_task_status

    try:
        with session_scope() as db:
            update_task_status(
                db,
                task_id,
                status,
                progress=progress,
                error_message=error_message,
                completed=status in ("completed", "failed", "skipped"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not update redaction task record %s to %s: %s", task_id, status, exc)


@celery_app.task(
    bind=True,
    name="redaction.detect",
    priority=RedactionPriority.PIPELINE_AUTO,
    max_retries=2,
    default_retry_delay=30,
)
def redaction_detect_task(
    self,
    file_id: int,
    user_id: int | None = None,
    pipeline_task_id: str | None = None,
) -> dict[str, Any]:
    """Detect + cache redaction spans for every segment of a file.

    Task tracking (issue #622): the row is created only after the file is
    confirmed to exist — ``Task.media_file_id`` is a foreign key, so a row
    can't target a file that isn't there. ``task_id`` is ``self.request.id``,
    which Celery keeps stable across ``self.retry()`` calls (a retry re-runs
    the *same* task id after its countdown, it does not dispatch a new one),
    so ``create_task_record``'s existing ``IntegrityError``-and-reuse path is
    what keeps a retried detection from producing duplicate Task rows —
    matching how ``topic_extraction.py``/``summarization.py`` behave under
    their own ``defer_for_redaction`` retries.
    """
    from app.core.enums import FileStatus
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.services.redaction.service import RedactionService
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status

    task_id = self.request.id
    logger.info("Redaction detection task %s started for file %s", task_id, file_id)
    total_start = time.time()
    benchmark_timing.mark(pipeline_task_id, "redaction_start")

    try:
        # Phase 0 — resolve the file and create/start the Task row in its OWN
        # transaction, committed before the real (possibly-failing) work starts.
        # Sharing one `session_scope` with `detect_and_store` below would mean a
        # detection failure rolls back the tracking row along with it — the
        # exact invisibility bug issue #622 exists to fix, just relocated onto
        # the failure path instead of closed.
        with session_scope() as db:
            media = db.query(MediaFile).filter(MediaFile.id == file_id).first()
            if media is None:
                return {"status": "skipped", "reason": "file_not_found"}
            owner_id = int(media.user_id)
            effective_user_id = user_id or owner_id

            create_task_record(db, task_id, effective_user_id, file_id, "redaction_detection")
            update_task_status(db, task_id, "in_progress", progress=0.1)

            # Guard: don't run while the file is being reprocessed. Enum
            # comparison — str(FileStatus.X) is "FileStatus.X", so the old
            # string form never matched (issue #272).
            needs_no_segments_skip = (
                media.status in (FileStatus.PROCESSING, FileStatus.CANCELLING)
                and not media.transcript_segments
            )

        if needs_no_segments_skip:
            _mark_redaction_task(task_id, "skipped", progress=1.0)
            return {"status": "skipped", "reason": "no_segments"}

        # Phase 1 — the actual (CPU-bound, locally-modeled) detection work.
        # `RedactionService.detect_and_store` owns its own session lifetime —
        # it writes an intermediate `redaction_status` before scanning — so it
        # still needs a session passed in; that pre-existing shape is not
        # something this task-tracking change restructures.
        with session_scope() as db:
            result = RedactionService.detect_and_store(db, file_id)
        _mark_redaction_task(task_id, "completed", progress=1.0)

        benchmark_timing.mark(pipeline_task_id, "redaction_end")
        benchmark_timing.set_context(
            pipeline_task_id,
            {
                "redaction_detectors": result.get("detectors", ""),
                "pii_entities_found": result.get("pii_entities_found", 0),
            },
        )

        _notify(user_id or owner_id, file_id, result)
        logger.info(
            "Redaction detection done for file %s: %s (%.0f ms)",
            file_id,
            result.get("status"),
            (time.time() - total_start) * 1000,
        )
        return {"file_id": file_id, **result}

    except Exception as exc:  # noqa: BLE001
        benchmark_timing.mark(pipeline_task_id, "redaction_end")
        logger.error("Redaction detection failed for file %s: %s", file_id, exc)
        if self.request.retries < self.max_retries:
            # Same task_id retries after the countdown — leave status as
            # in_progress rather than marking it failed for a run that is
            # about to happen again.
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc
        _mark_redaction_task(task_id, "failed", error_message=str(exc))
        return {"status": "failed", "file_id": file_id, "error": str(exc)}


@celery_app.task(
    bind=True,
    name="redaction.reindex_all",
    priority=RedactionPriority.ADMIN_BACKFILL,
)
def redaction_reindex_all_task(self, only_stale: bool = True, limit: int | None = None) -> dict:
    """Backfill/re-index redaction spans across completed files.

    Used on first rollout and after a detector model upgrade. ``only_stale`` skips files
    already detected at the current REDACTION_MODEL_VERSION. Chunked dispatch — each file
    is processed by its own ``redaction.detect`` for isolation/retry.
    """
    from app.core.enums import FileStatus
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    dispatched = 0
    with session_scope() as db:
        q = db.query(MediaFile.id, MediaFile.user_id).filter(
            MediaFile.status == FileStatus.COMPLETED
        )
        if only_stale:
            q = q.filter(
                (MediaFile.redaction_model_version != REDACTION_MODEL_VERSION)
                | (MediaFile.redaction_model_version.is_(None))
            )
        if limit:
            q = q.limit(limit)
        rows = q.all()

    for file_id, owner_id in rows:
        redaction_detect_task.delay(file_id=file_id, user_id=owner_id)
        dispatched += 1

    logger.info("Redaction reindex dispatched %d files (only_stale=%s)", dispatched, only_stale)
    return {"status": "dispatched", "files": dispatched}


def _notify(user_id: int, file_id: int, result: dict) -> None:
    try:
        from app.services.notification_service import send_task_notification

        send_task_notification(
            user_id,
            NOTIFICATION_TYPE_REDACTION_STATUS,
            status=result.get("status", ""),
            file_id=file_id,
            extra={
                "redacted_segments": result.get("segments", 0),
                "pii_entities_found": result.get("pii_entities_found", 0),
                "language": result.get("language"),
                # Detectors skipped because the transcript language isn't supported.
                "skipped_detectors": result.get("skipped_detectors", []),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to send redaction notification: %s", exc)
