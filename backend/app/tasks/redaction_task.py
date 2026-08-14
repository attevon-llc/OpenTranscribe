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
    """Detect + cache redaction spans for every segment of a file."""
    from app.core.enums import FileStatus
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.services.redaction.service import RedactionService

    task_id = self.request.id
    logger.info("Redaction detection task %s started for file %s", task_id, file_id)
    total_start = time.time()
    benchmark_timing.mark(pipeline_task_id, "redaction_start")

    try:
        with session_scope() as db:
            media = db.query(MediaFile).filter(MediaFile.id == file_id).first()
            if media is None:
                return {"status": "skipped", "reason": "file_not_found"}
            owner_id = int(media.user_id)
            # Guard: don't run while the file is being reprocessed. Enum
            # comparison — str(FileStatus.X) is "FileStatus.X", so the old
            # string form never matched (issue #272).
            if (
                media.status in (FileStatus.PROCESSING, FileStatus.CANCELLING)
                and not media.transcript_segments
            ):
                return {"status": "skipped", "reason": "no_segments"}

            result = RedactionService.detect_and_store(db, file_id)

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
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc
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


@celery_app.task(
    bind=True,
    name="redaction.detect_document",
    priority=RedactionPriority.PIPELINE_AUTO,
    max_retries=2,
    default_retry_delay=30,
)
def redaction_detect_document_task(
    self,
    document_id: int,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Detect + cache redaction spans for every chunk of a document. Mirrors
    ``redaction_detect_task`` — see its docstring for the dispatch-gating rationale,
    identical here (opt-out by default, gated on the owner's effective config).
    """
    from app.db.session_utils import session_scope
    from app.models.document import Document
    from app.services.redaction.service import RedactionService

    task_id = self.request.id
    logger.info(
        "Document redaction detection task %s started for document %s", task_id, document_id
    )
    total_start = time.time()

    try:
        with session_scope() as db:
            doc = db.query(Document).filter(Document.id == document_id).first()
            if doc is None:
                return {"status": "skipped", "reason": "file_not_found"}
            owner_id = int(doc.user_id)
            result = RedactionService.detect_and_store_document(db, document_id)

        _notify_document(user_id or owner_id, document_id, result)
        logger.info(
            "Document redaction detection done for document %s: %s (%.0f ms)",
            document_id,
            result.get("status"),
            (time.time() - total_start) * 1000,
        )
        return {"document_id": document_id, **result}

    except Exception as exc:  # noqa: BLE001
        logger.error("Document redaction detection failed for document %s: %s", document_id, exc)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (2**self.request.retries)) from exc
        return {"status": "failed", "document_id": document_id, "error": str(exc)}


def _notify_document(user_id: int, document_id: int, result: dict) -> None:
    try:
        from app.services.notification_service import send_task_notification

        send_task_notification(
            user_id,
            NOTIFICATION_TYPE_REDACTION_STATUS,
            status=result.get("status", ""),
            file_id=document_id,
            extra={
                "redacted_chunks": result.get("chunks", 0),
                "pii_entities_found": result.get("pii_entities_found", 0),
                "language": result.get("language"),
                "skipped_detectors": result.get("skipped_detectors", []),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to send document redaction notification: %s", exc)


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
