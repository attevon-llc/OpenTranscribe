"""Presigned-upload completion endpoint (Phase 2 PR #2 of the timing plan).

Flow:

    1. Browser POSTs /files/prepare with use_presigned=true and gets back
       ``{file_id, task_id, upload_url, storage_path}``.
    2. Browser PUTs the raw bytes directly to MinIO via ``upload_url``.
    3. Browser POSTs /files/complete with the ``file_id`` + ``task_id`` and
       any client-side timing markers.
    4. This endpoint verifies the object exists, computes imohash, dispatches
       the transcription pipeline, and returns the file record.

See ``docs/PIPELINE_TIMING.md`` for the marker reference.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.utils import benchmark_timing

logger = logging.getLogger(__name__)

router = APIRouter()


class CompleteUploadRequest(BaseModel):
    """Payload for POST /files/complete.

    ``task_id`` is the application task_id we handed out from /prepare so the
    client-side timing markers land in the same benchmark hash. All
    ``*_ms`` fields are epoch-millisecond timestamps measured client-side;
    they are optional but strongly recommended so the timing report can
    show the full client → server → done wall-clock.
    """

    file_id: str = Field(..., description="UUID of the MediaFile from /prepare")
    task_id: str | None = Field(None, description="Application task_id from /prepare response")
    file_hash: str | None = Field(None, description="Client-computed SHA-256")
    file_size: int | None = Field(None, description="Client-observed size in bytes")
    # Optional client-side timing markers (epoch-ms)
    client_hash_start_ms: int | None = None
    client_hash_end_ms: int | None = None
    client_put_start_ms: int | None = None
    client_put_end_ms: int | None = None
    # Per-file pipeline overrides (same semantics as the legacy upload path)
    min_speakers: int | None = None
    max_speakers: int | None = None
    num_speakers: int | None = None
    skip_summary: bool | None = False
    whisper_model: str | None = None


def _record_client_markers(task_id: str | None, req: CompleteUploadRequest) -> None:
    """Convert client-side epoch-ms markers into float-second hash entries.

    Keeps the Redis hash schema uniform: every marker is float seconds.
    """
    if not task_id:
        return
    for name, val in (
        ("client_hash_start", req.client_hash_start_ms),
        ("client_hash_end", req.client_hash_end_ms),
        ("client_put_start", req.client_put_start_ms),
        ("client_put_end", req.client_put_end_ms),
    ):
        if val is not None and val > 0:
            benchmark_timing.mark(task_id, name, val / 1000.0)


@router.post("/complete", response_model=dict[str, Any])
def complete_upload(
    request: CompleteUploadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Finalize a presigned upload and dispatch the transcription pipeline."""
    from app.api.endpoints.files.upload import _update_file_hash
    from app.api.endpoints.files.upload import dispatch_upload_pipeline
    from app.services.imohash_service import compute_from_minio
    from app.services.minio_service import object_exists_and_size

    benchmark_timing.mark(request.task_id, "http_request_received")
    _record_client_markers(request.task_id, request)

    # Locate the prepared MediaFile — must belong to the caller.
    db_file = (
        db.query(MediaFile)
        .filter(MediaFile.uuid == request.file_id, MediaFile.user_id == current_user.id)
        .first()
    )
    if not db_file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MediaFile {request.file_id} not found for user",
        )
    if not db_file.storage_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MediaFile has no storage_path (was /prepare called with use_presigned=true?)",
        )

    # Verify the object actually landed in MinIO — trust but verify.
    minio_size = object_exists_and_size(str(db_file.storage_path))
    if minio_size is None:
        # The presigned PUT never completed, so the prepared row is an orphan.
        # Drop it (parity with the legacy path's failure cleanup) so it doesn't
        # linger in the gallery as a stuck PENDING upload.
        db.delete(db_file)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"No MinIO object at {db_file.storage_path} — presigned PUT "
                "did not complete successfully"
            ),
        )
    if request.file_size and abs(minio_size - int(request.file_size)) > 0:
        logger.warning(
            f"size mismatch for {request.file_id}: client={request.file_size} server={minio_size}"
        )

    # Enforce the upload ceiling against the size MinIO ACTUALLY observed, not the size
    # the client declared at /prepare. The declared value only gates minting the
    # presigned URL; the object is written browser→MinIO directly, so this is the first
    # authoritative number and the only place an oversized upload can be caught
    # (issue #284 A0.12). Delete the object and the row rather than leaving either behind.
    from app.api.endpoints.files.upload import validate_file_size_for_tenant

    try:
        validate_file_size_for_tenant(minio_size, db_file.organization_id)
    except HTTPException:
        logger.warning(
            "Rejecting oversized upload %s: %d bytes exceeds the ceiling",
            request.file_id,
            minio_size,
        )
        from app.services.minio_service import delete_file

        with contextlib.suppress(Exception):
            delete_file(str(db_file.storage_path))
        db.delete(db_file)
        db.commit()
        raise

    # Magic-byte validation — parity with the legacy path. The bytes went
    # browser→MinIO directly, so verify the object's real signature matches
    # its declared content_type before dispatching it to the GPU pipeline.
    # We range-read only a small header (never the whole object). Fail closed
    # on a CONFIRMED mismatch (delete object + row, 400); on a transient read
    # error, log and proceed — the object verified-exists, so we don't reject
    # a legitimate upload over a MinIO hiccup.
    header_bytes: bytes | None = None
    try:
        from app.services.minio_service import range_read

        header_bytes = range_read(str(db_file.storage_path), 0, 64)
    except Exception as read_err:
        logger.warning(
            f"header read for validation of {request.file_id} failed "
            f"(non-fatal, proceeding): {read_err}"
        )
    if header_bytes is not None and db_file.content_type:
        from app.utils.file_validation import validate_uploaded_file

        is_valid, validation_detail = validate_uploaded_file(
            header_bytes, str(db_file.content_type), str(db_file.filename)
        )
        benchmark_timing.mark(request.task_id, "http_validation_end")
        if not is_valid:
            from app.services.minio_service import delete_file

            logger.warning(f"Rejecting presigned upload {request.file_id}: {validation_detail}")
            try:
                delete_file(str(db_file.storage_path))
            except Exception as del_err:
                logger.warning(f"cleanup of rejected object failed (non-fatal): {del_err}")
            db.delete(db_file)
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File content does not match its declared type: {validation_detail}",
            )

    # Update client-supplied hash and compute server-side imohash fingerprint
    _update_file_hash(db_file, request.file_hash, str(db_file.filename))
    try:
        with benchmark_timing.stage(request.task_id, "imohash"):
            fingerprint = compute_from_minio(str(db_file.storage_path), size=minio_size)
        if fingerprint:
            db_file.imohash = fingerprint  # type: ignore[assignment]
    except Exception as e:
        logger.debug(f"imohash for {request.file_id} failed (non-fatal): {e}")

    # Update the row to reflect the landed file. The pending attributes
    # we just set (file_size, status, summary_status, imohash) stay on the
    # session-managed instance after commit because we don't need any
    # server-assigned columns — skipping db.refresh() saves one SELECT
    # round-trip per presigned upload.
    db_file.file_size = minio_size  # type: ignore[assignment]
    if request.skip_summary:
        db_file.summary_status = "disabled"  # type: ignore[assignment]
    db_file.status = FileStatus.PENDING  # type: ignore[assignment]
    # Snapshot the per-file model BEFORE commit so resolving it doesn't trigger
    # an expire-on-commit refetch (we deliberately skip db.refresh() here).
    whisper_model: str | None = request.whisper_model
    if not whisper_model and db_file.requested_whisper_model:
        whisper_model = str(db_file.requested_whisper_model)
    db.commit()

    # Fire the thumbnail + transcription pipeline via the shared dispatch tail
    # (same call the legacy path uses). The thumbnail runs concurrently so the
    # gallery shows it via the live file_updated refresh during processing, and
    # the pre-minted task_id keeps every downstream marker in one benchmark hash.
    dispatch_upload_pipeline(
        db_file,
        user_id=current_user.id,
        whisper_model=whisper_model,
        min_speakers=request.min_speakers,
        max_speakers=request.max_speakers,
        num_speakers=request.num_speakers,
        task_id=request.task_id,
    )

    # Product metric: file accepted via direct upload (API process). The
    # watch-source path dispatches in a worker, whose registry is never scraped.
    from app.core.metrics import files_uploaded_total

    files_uploaded_total.labels(source="upload").inc()

    benchmark_timing.mark(request.task_id, "http_response_end")

    # Invalidate caches so gallery picks up the new file
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_user_files(current_user.id)
    except Exception as cache_err:
        logger.debug(f"Cache invalidation failed (non-critical): {cache_err}")

    return {
        "file_id": str(db_file.uuid),
        "task_id": request.task_id,
        "status": db_file.status.value if db_file.status else "pending",
        "file_size": minio_size,
        "imohash": db_file.imohash,
    }
