"""Async DOCX / PDF transcript export (issue #1278).

Runs on the ``download`` worker, reusing the bulk-export machinery: the document is uploaded to
the 1-day ``bulk/`` prefix, delivered as a presigned URL over the existing signed-job SSE stream
(``GET /files/bulk-export-stream``), and cached for a reconnecting client. The API never proxies
the bytes, and a long render (about a minute for four hours of Arabic) never holds a request
thread.

Every gate is re-applied here, at run time, by ``prepare_document`` (quarantine, a pending
redaction scan, the reader's policy, ``export_locked``): a dispatch-time check does not survive
a takedown or an admin tightening the floor between the click and the render.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime

from app.core.celery import celery_app
from app.core.constants import DOCUMENT_EXPORT_SOFT_TIME_LIMIT_S
from app.core.constants import DownloadPriority
from app.db.session_utils import session_scope
from app.schemas.transcript_export import TranscriptDocumentExportRequest
from app.services.download_events import DOCUMENT_EXPORT_ERRORS
from app.services.download_events import publish_bulk_event
from app.services.download_events import release_document_export_guard
from app.services.minio_service import MinIOService
from app.services.video_processing_service import VideoProcessingService

logger = logging.getLogger(__name__)

RESULT_TTL_S = 900


def _fail(job_id: str, reason: str) -> dict:
    """Publish a fixed-sentence error and cache it, so a client that connects late still sees it."""
    from app.core.redis import get_redis

    message = DOCUMENT_EXPORT_ERRORS[reason]
    try:
        get_redis().setex(
            f"bulk_export_result:{job_id}",
            RESULT_TTL_S,
            json.dumps({"status": "error", "reason": reason, "message": message}),
        )
    except Exception as e:  # pragma: no cover - the live event below still goes out
        logger.warning("Could not cache a document export error: %s", type(e).__name__)
    publish_bulk_event(job_id, status="error", message=message, reason=reason)
    return {"status": "error", "reason": reason}


@celery_app.task(
    name="download.prepare_transcript_document",
    bind=True,
    priority=DownloadPriority.SINGLE_URL,
    max_retries=0,
    soft_time_limit=DOCUMENT_EXPORT_SOFT_TIME_LIMIT_S,
)
def prepare_transcript_document_task(
    self,
    file_id: int,
    user_id: int | None,
    organization_id: int | None,
    request: dict,
    job_id: str,
    guard_key: str | None = None,
) -> dict:
    """Render one transcript as DOCX/PDF and push a presigned URL over SSE.

    Args:
        file_id: ``MediaFile`` id, already permission-filtered by the dispatching endpoint.
        user_id: Whose redaction policy masks the document (the requester). ``None`` refuses.
        organization_id: The requester's tenant scope, so a per-org floor is consulted.
        request: A dumped ``TranscriptDocumentExportRequest``. Never a policy or a config.
        job_id: Signed job id keying the SSE channel and the reconnect cache.
        guard_key: The dispatcher's Redis dedupe key, released when the job ends.
    """
    from app.core.redis import get_redis
    from app.services.minio_service import get_presigned_download_url
    from app.services.transcript_document.build import DocumentExportRefusedError
    from app.services.transcript_document.build import prepare_document
    from app.services.transcript_document.model import DocumentTooLongError
    from app.services.transcript_document.model import FontsUnavailableError
    from app.services.transcript_document.render import CONTENT_TYPES
    from app.services.transcript_document.render import render_document

    try:
        parsed = TranscriptDocumentExportRequest.model_validate(request)
        publish_bulk_event(job_id, status="processing", message="Building the document...")
        # Read phase: short session, plain data out. ``session_scope`` commits on exit, which
        # is safe only because ``masked_segments`` expunges before masking.
        with session_scope() as db:
            prepared = prepare_document(
                db,
                file_id=file_id,
                user_id=user_id,
                organization_id=organization_id,
                request=parsed,
                now=datetime.now(UTC),
            )
        # Render phase: no database session is held.
        data = render_document(parsed.format, prepared)

        key = f"bulk/{job_id}.{parsed.format}"
        service = VideoProcessingService(MinIOService())
        content_type = CONTENT_TYPES[parsed.format]
        service.minio_service.upload_bytes(service.cache_bucket, key, data, content_type)
        filename = f"{prepared.base_filename}.{parsed.format}"
        url = get_presigned_download_url(
            key,
            bucket_name=service.cache_bucket,
            download_filename=filename,
            content_type=content_type,
        )
        get_redis().setex(
            f"bulk_export_result:{job_id}",
            RESULT_TTL_S,
            json.dumps({"url": url, "filename": filename, "exported": 1, "skipped": 0}),
        )
        publish_bulk_event(
            job_id,
            status="completed",
            message="Document ready.",
            url=url,
            filename=filename,
            exported=1,
        )
        return {"status": "success"}
    except DocumentExportRefusedError as e:
        return _fail(job_id, e.reason)
    except DocumentTooLongError:
        return _fail(job_id, "too_long")
    except FontsUnavailableError:
        logger.error("Document export: the document fonts are not installed")
        return _fail(job_id, "fonts_unavailable")
    except Exception as e:
        # Type only: the message of a rendering error can quote transcript text (issue #1022).
        logger.error("Document export failed (job %s): %s", job_id, type(e).__name__)
        return _fail(job_id, "failed")
    finally:
        release_document_export_guard(guard_key)
