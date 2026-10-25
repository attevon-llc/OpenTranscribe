"""``POST /files/{uuid}/export/document``: queue a DOCX / PDF transcript export (issue #1278).

The same redaction rule governs every export surface, but the ROUTE cannot be the synchronous
``GET /files/{uuid}/export``: a four-hour Arabic PDF is about a minute of CPU, against a 60 s
client timeout and a GIL the API process shares with every other request. So this endpoint
validates, checks the gates that can be answered fast, and queues
``download.prepare_transcript_document`` on the ``download`` worker; the browser opens the
existing signed-job stream (``GET /files/bulk-export-stream?job=``) for the presigned URL.

The worker re-applies every gate at run time (``services/transcript_document/build.py``). What
this route decides is only what can be refused immediately with a useful status:

- 404: not visible, no transcript, or the ``exports.documents`` capability is off
- 400: transcription not finished
- 409: the reader's policy masks and the redaction scan has not produced spans yet
- 503: the redaction policy cannot be resolved (fail closed, never "unmasked")
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.core.capabilities import require_capability
from app.core.constants import DOCUMENT_EXPORT_GUARD_TTL_S
from app.db.base import get_db
from app.models.media import TranscriptSegment
from app.models.user import User
from app.schemas.transcript_export import DocumentExportAccepted
from app.schemas.transcript_export import TranscriptDocumentExportRequest
from app.services.download_events import document_export_guard_key
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

from .crud import _redaction_pending
from .subtitles import new_bulk_job_id

router = APIRouter()
logger = logging.getLogger(__name__)


def _options_sha(body: TranscriptDocumentExportRequest) -> str:
    """Digest of every option, so only an identical request collapses onto a running job."""
    return hashlib.sha256(body.model_dump_json().encode("utf-8")).hexdigest()[:16]


def _claim_or_existing_job(user_id: int, file_id: int, body: TranscriptDocumentExportRequest):
    """``(job_id, guard_key, is_new)``: a double click gets the SAME job, never a second render."""
    from app.core.redis import get_redis

    job_id = new_bulk_job_id(user_id)
    guard_key = document_export_guard_key(user_id, file_id, _options_sha(body))
    try:
        redis = get_redis()
        if redis.set(guard_key, job_id, nx=True, ex=DOCUMENT_EXPORT_GUARD_TTL_S):
            return job_id, guard_key, True
        existing = redis.get(guard_key)
        if existing:
            value = existing.decode() if isinstance(existing, bytes) else str(existing)
            return value, guard_key, False
    except Exception as e:
        # The guard only saves duplicate work; failing to take it must not refuse the export.
        logger.warning("Document export guard unavailable: %s", type(e).__name__)
    return job_id, None, True


@router.post(
    "/{file_uuid}/export/document",
    response_model=DocumentExportAccepted,
    status_code=202,
    dependencies=[
        Depends(refuse_under_support_grant),
        Depends(require_capability("exports.documents")),
    ],
)
def request_document_export(
    file_uuid: str,
    body: TranscriptDocumentExportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> DocumentExportAccepted:
    """Queue a DOCX/PDF export of one transcript and return the job id.

    Redaction is applied on the worker with **this** user's effective policy, resolved at run
    time. ``reveal_original`` is a request: the owner gets the original only when the admin
    floor permits, and a non-owner admin never gets it from this route.
    """
    from app.services.redaction.config import resolve_effective_config

    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
    )
    if media_file.status != "completed":
        raise HTTPException(status_code=400, detail="Transcription not completed yet")

    try:
        cfg = resolve_effective_config(db, current_user.id, organization_id=ctx.org_id)
    except Exception as e:
        # FAIL CLOSED, exactly like the synchronous export: an unreadable policy is never
        # read as "mask nothing".
        logger.exception("Failed to resolve redaction config; refusing the document export")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; export withheld.",
        ) from e

    if _redaction_pending(db, cfg, media_file):
        raise HTTPException(
            status_code=409,
            detail=(
                "Content redaction has not finished for this file, so an export "
                "could not be masked. Try again once detection completes."
            ),
        )

    has_transcript = (
        db.query(TranscriptSegment.id)
        .filter(TranscriptSegment.media_file_id == media_file.id)
        .first()
    )
    if has_transcript is None:
        raise HTTPException(status_code=404, detail="No transcript available for this file")

    job_id, guard_key, is_new = _claim_or_existing_job(current_user.id, media_file.id, body)
    if is_new:
        from app.tasks.transcript_document import prepare_transcript_document_task

        prepare_transcript_document_task.delay(
            file_id=media_file.id,
            user_id=current_user.id,
            organization_id=ctx.org_id,
            request=body.model_dump(),
            job_id=job_id,
            guard_key=guard_key,
        )
    return DocumentExportAccepted(status="processing", job_id=job_id)
